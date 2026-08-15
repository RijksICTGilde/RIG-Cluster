"""Tests for the ``send-email`` service and the mail manager (RC-114).

Four things are worth holding down, and they are the four the plan argues hardest for:

1. **One account path, two callers.** ``ensure_account`` is a staticmethod precisely so the
   platform caller needs no project. If someone ever gives the platform its own copy, the
   platform account becomes the one nobody looks at (``plans/mailrelay.md``, aanvulling 4).
2. **The NetworkPolicy comes from the service.** The relay lives in a namespace the tenant
   baseline does not open, so without this rule a pod resolves the relay and then hangs.
   The rule must also DISAPPEAR when the service does, which is what the prune keys on.
3. **The account is per project, not per deployment.** So removing the service from one
   deployment while another still has it must not delete the account.
4. **The account block is platform data.** The API may not clear or rewrite it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from opi.connectors.mail import MailConnector, MailRelayNotConfiguredError, create_mail_connector
from opi.core.cluster_config import get_mail_domain, get_mail_relay_host, get_mail_relay_namespace, get_mail_relay_port
from opi.manager.mail_manager import MailManager
from opi.services.catalog.base import ConfigLayer, DeploymentManifestContext
from opi.services.catalog.send_email import RELAY_POD_LABELS, SendEmailService
from opi.services.catalog.send_email.config_model import MAX_MESSAGES_PER_DAY, SendEmailConfig
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from pydantic import ValidationError
from ruamel.yaml import YAML

SERVICE = get_service(ServiceType.SEND_EMAIL)


def _project(*, component_services: list[str] | None = None, config: dict | None = None) -> dict:
    """A project with one deployment of one component, optionally using send-email."""
    return {
        "name": "myproject",
        "services": [{"name": ServiceType.SEND_EMAIL.value, "config": config or {}}],
        "components": [{"name": "web", "services": component_services or []}],
        "deployments": [
            {
                "name": "prod",
                "cluster": "sandboxed-local",
                "components": [{"reference": "web"}],
            }
        ],
    }


class TestTheConfigModel:
    """The rules that keep a bad value out of an address the relay enforces."""

    def test_an_empty_config_is_valid(self) -> None:
        """Everything is optional: switching the service on requires no decisions."""
        config = SendEmailConfig()
        assert config.from_local_part is None
        assert config.accounts == []

    def test_a_local_part_with_an_at_sign_is_refused(self) -> None:
        """It would land verbatim in an address, so it is refused here and not at send time."""
        with pytest.raises(ValidationError):
            SendEmailConfig(**{"from-local-part": "no@reply"})

    def test_a_local_part_with_a_space_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            SendEmailConfig(**{"from-local-part": "no reply"})

    def test_a_local_part_may_not_start_with_a_dot(self) -> None:
        with pytest.raises(ValidationError):
            SendEmailConfig(**{"from-local-part": ".noreply"})

    def test_a_plain_local_part_is_accepted(self) -> None:
        assert SendEmailConfig(**{"from-local-part": "noreply"}).from_local_part == "noreply"

    def test_a_budget_above_the_cap_is_refused(self) -> None:
        """The cap is the agreement with the mail team, so it is refused, never clamped."""
        with pytest.raises(ValidationError):
            SendEmailConfig(**{"messages-per-day": MAX_MESSAGES_PER_DAY + 1})

    def test_a_budget_of_zero_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            SendEmailConfig(**{"messages-per-day": 0})

    def test_an_unknown_key_is_refused(self) -> None:
        """extra='forbid': a typo in a key must not silently do nothing."""
        with pytest.raises(ValidationError):
            SendEmailConfig(**{"from-adres": "noreply"})


class TestTheAccountBlockIsPlatformData:
    """Aanvulling 5: declared from the start instead of repaired afterwards."""

    def test_the_accounts_field_is_platform_managed(self) -> None:
        assert SERVICE.platform_managed_fields(ConfigLayer.PROJECT) == frozenset({"accounts"})

    def test_the_user_fields_are_not(self) -> None:
        managed = SERVICE.platform_managed_fields(ConfigLayer.PROJECT)
        assert "from-name" not in managed
        assert "from-local-part" not in managed
        assert "messages-per-day" not in managed


class TestTheServiceDeclaration:
    """What the registry has to be able to read off the service."""

    def test_it_is_selectable_by_a_user(self) -> None:
        """Not hidden: a project switches this on itself."""
        assert SERVICE.definition.hidden is False

    def test_it_carries_config_on_the_project_layer_only(self) -> None:
        """An account belongs to the project; a component only decides whether it gets it."""
        assert SERVICE.config_layers() == [ConfigLayer.PROJECT]

    def test_it_does_not_enrol_itself(self) -> None:
        """Sending under the platform's name eats agreed volume: that is a project decision."""
        assert SERVICE.allows_implicit_project_selection is False


class TestTheNetworkPolicy:
    """Aanvulling 3: the rule comes from the service, because only it knows it is on."""

    def _ctx(self, project: dict) -> DeploymentManifestContext:
        return DeploymentManifestContext(
            project_name="myproject",
            project_data=project,
            deployment=project["deployments"][0],
            cluster="sandboxed-local",
            namespace="rig-myproject",
        )

    def test_a_component_using_the_service_gets_an_egress_rule(self) -> None:
        specs = SERVICE.contribute_deployment_manifests(
            self._ctx(_project(component_services=[ServiceType.SEND_EMAIL.value]))
        )
        assert len(specs) == 1
        egress = specs[0].values["egress"]
        assert egress[0]["peer"]["namespace"] == get_mail_relay_namespace("sandboxed-local")
        assert egress[0]["peer"]["pod_labels"] == RELAY_POD_LABELS
        assert egress[0]["ports"] == [get_mail_relay_port("sandboxed-local")]

    def test_it_opens_nothing_inbound(self) -> None:
        """One direction only: the relay never has to reach into a project namespace."""
        specs = SERVICE.contribute_deployment_manifests(
            self._ctx(_project(component_services=[ServiceType.SEND_EMAIL.value]))
        )
        assert specs[0].values["ingress"] == []

    def test_a_component_not_using_the_service_gets_nothing(self) -> None:
        """No file means the prune removes a stale one -- that is how switching off works."""
        assert SERVICE.contribute_deployment_manifests(self._ctx(_project(component_services=[]))) == []

    def test_the_filename_carries_the_prune_prefix(self) -> None:
        """``_prune_obsolete_service_manifests`` keys on '{deployment}-{service}-'; without
        this prefix the policy would stay behind after the service is switched off."""
        specs = SERVICE.contribute_deployment_manifests(
            self._ctx(_project(component_services=[ServiceType.SEND_EMAIL.value]))
        )
        assert specs[0].filename.startswith(f"prod-{ServiceType.SEND_EMAIL.value}-")

    def test_the_rendered_policy_selects_only_this_component(self) -> None:
        """A policy on the whole namespace would open the relay for every workload there."""
        from opi.generation.manifests import render_template

        specs = SERVICE.contribute_deployment_manifests(
            self._ctx(_project(component_services=[ServiceType.SEND_EMAIL.value]))
        )
        rendered = YAML().load(render_template(specs[0].template_path, specs[0].values))
        assert rendered["spec"]["podSelector"]["matchLabels"] == {"app": "prod-web"}
        assert rendered["spec"]["policyTypes"] == ["Egress"]
        peer = rendered["spec"]["egress"][0]["to"][0]
        assert peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "rig-operations-ron"
        assert peer["podSelector"]["matchLabels"] == {"app": "rig-mail-relay"}


class TestTheOneAccountPath:
    """Aanvulling 4: two callers, one implementation."""

    def _connector(self, existing: dict | None = None) -> MailConnector:
        connector = MailConnector("http://relay", "admin", "geheim")
        connector.get_principal = AsyncMock(return_value=existing)  # type: ignore[method-assign]
        connector.create_principal = AsyncMock()  # type: ignore[method-assign]
        connector.update_principal = AsyncMock()  # type: ignore[method-assign]
        return connector

    @pytest.mark.asyncio
    async def test_a_missing_account_is_created(self) -> None:
        connector = self._connector(existing=None)
        account = await MailManager.ensure_account(
            connector=connector,
            username="myproject",
            password="geheim",
            from_address="noreply@mail.example",
            bounce_address="bounce+myproject@mail.example",
            messages_per_day=500,
        )
        connector.create_principal.assert_awaited_once()
        assert account.username == "myproject"
        assert account.messages_per_day == 500

    @pytest.mark.asyncio
    async def test_an_existing_account_is_brought_in_line_not_refused(self) -> None:
        """Replay-safety: provisioning runs again on every process of the project."""
        connector = self._connector(existing={"name": "myproject"})
        await MailManager.ensure_account(
            connector=connector,
            username="myproject",
            password="geheim",
            from_address="noreply@mail.example",
            bounce_address="bounce+myproject@mail.example",
            messages_per_day=800,
        )
        connector.create_principal.assert_not_awaited()
        connector.update_principal.assert_awaited_once()
        assert connector.update_principal.await_args.kwargs["messages_per_day"] == 800

    def test_the_platform_caller_needs_no_project(self) -> None:
        """A staticmethod, so ZAD's account goes through the very same code without a
        project file. Make it an instance method again and the platform side needs a
        second implementation -- which is the failure mode the plan names."""
        assert isinstance(MailManager.__dict__["ensure_account"], staticmethod)


class TestTheAddresses:
    """The envelope stays on our own domain, which is what makes SPF cheap."""

    def _manager(self) -> MailManager:
        return MailManager(project_manager=SimpleNamespace())  # type: ignore[arg-type]

    def test_the_default_local_part_is_noreply(self) -> None:
        sender, _ = self._manager()._addresses("sandboxed-local", "myproject", {})
        assert sender == f"noreply@{get_mail_domain('sandboxed-local')}"

    def test_the_project_chooses_the_local_part(self) -> None:
        sender, _ = self._manager()._addresses("sandboxed-local", "myproject", {"from-local-part": "support"})
        assert sender == f"support@{get_mail_domain('sandboxed-local')}"

    def test_the_bounce_address_carries_the_account_name(self) -> None:
        """So a returned message is traceable to one project without asking the relay."""
        _, bounce = self._manager()._addresses("sandboxed-local", "myproject", {})
        assert bounce == f"bounce+myproject@{get_mail_domain('sandboxed-local')}"

    def test_an_own_domain_moves_the_sender_but_not_the_bounce(self) -> None:
        """SPF is checked against the ENVELOPE domain. Keeping the envelope on our own
        domain is exactly why a project domain costs one DKIM record instead of a full
        DNS set -- move the bounce along and that saving is gone."""
        sender, bounce = self._manager()._addresses("sandboxed-local", "myproject", {"from-domain": "eigen.example"})
        assert sender == "noreply@eigen.example"
        assert bounce == f"bounce+myproject@{get_mail_domain('sandboxed-local')}"


class TestTheAccountIsSharedByTheProject:
    """One account per project, so one budget and one bounce address."""

    def _manager(self) -> MailManager:
        """With the REAL file handler: 'does this deployment use the service' is exactly
        the question that must not get a second, simpler answer here."""
        from opi.handlers.project_file_handler import ProjectFileHandler

        return MailManager(project_manager=SimpleNamespace(_project_file_handler=ProjectFileHandler()))  # type: ignore[arg-type]

    def test_another_deployment_still_using_it_blocks_removal(self) -> None:
        project = _project(component_services=[ServiceType.SEND_EMAIL.value])
        project["deployments"].append(
            {"name": "acceptatie", "cluster": "sandboxed-local", "components": [{"reference": "web"}]}
        )
        manager = self._manager()
        assert manager._project_still_uses_send_email(project, exclude_deployment="prod") is True

    def test_the_last_deployment_releases_the_account(self) -> None:
        project = _project(component_services=[ServiceType.SEND_EMAIL.value])
        manager = self._manager()
        assert manager._project_still_uses_send_email(project, exclude_deployment="prod") is False


class TestNoRelayConfigured:
    """A cluster without a relay must fail where someone is looking."""

    @pytest.mark.asyncio
    async def test_the_connector_refuses_instead_of_handing_out_dead_credentials(self, monkeypatch) -> None:
        from opi.core.config import settings

        monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", "")
        with pytest.raises(MailRelayNotConfiguredError):
            await create_mail_connector()

    @pytest.mark.asyncio
    async def test_the_platform_account_is_skipped_without_a_password(self, monkeypatch) -> None:
        """No relay yet simply means no platform mail yet; it must not stop the boot."""
        from opi.core.config import settings

        monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", "http://relay")
        monkeypatch.setattr(settings, "MAIL_PLATFORM_PASSWORD", "")
        assert await MailManager.ensure_platform_account() is None


class TestTheClusterConfig:
    """The relay is addressed per cluster, like every other shared service."""

    @pytest.mark.parametrize("cluster", ["local", "sandboxed-local", "odcn-production"])
    def test_every_cluster_knows_where_the_relay_is(self, cluster: str) -> None:
        assert get_mail_relay_host(cluster).endswith("svc.cluster.local")
        assert get_mail_relay_port(cluster) == 587
        assert get_mail_relay_namespace(cluster) == "rig-operations-ron"
        assert "@" not in get_mail_domain(cluster)

    def test_production_sends_from_the_platform_domain(self) -> None:
        """Let op het enkelvoud: rijksapps.nl is de zone van ODC-Noord zelf."""
        assert get_mail_domain("odcn-production") == "mail.rijksapp.nl"


class TestTheSecretHandedToTheApplication:
    """The five variables the plan names, and no sixth."""

    def test_the_variable_names_are_the_ones_an_smtp_library_expects(self) -> None:
        names = {variable.name for variable in SERVICE.definition.variables}
        assert names == {"SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM"}

    def test_the_secret_renders_those_keys(self) -> None:
        from opi.utils.secrets import SendEmailSecret

        data = SendEmailSecret(
            host="rig-mail-relay.rig-operations-ron.svc.cluster.local",
            port=587,
            username="myproject",
            password="geheim",
            from_address="noreply@mail.example",
        ).to_k8s_secret_data()
        assert data["SMTP_HOST"] == "rig-mail-relay.rig-operations-ron.svc.cluster.local"
        assert data["SMTP_PORT"] == "587"
        assert data["SMTP_FROM"] == "noreply@mail.example"


class TestTheServiceOnlySeesItsOwnComponents:
    """A second component that did not tick the service gets no rule."""

    def test_only_the_ticking_component_is_named(self) -> None:
        project = _project(component_services=[ServiceType.SEND_EMAIL.value])
        project["components"].append({"name": "worker", "services": []})
        project["deployments"][0]["components"].append({"reference": "worker"})
        ctx = DeploymentManifestContext(
            project_name="myproject",
            project_data=project,
            deployment=project["deployments"][0],
            cluster="sandboxed-local",
            namespace="rig-myproject",
        )
        specs = SendEmailService().contribute_deployment_manifests(ctx)
        assert [spec.filename for spec in specs] == [f"prod-{ServiceType.SEND_EMAIL.value}-web-network-policy"]
