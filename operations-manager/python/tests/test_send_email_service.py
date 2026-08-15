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

import aiohttp
import pytest
from opi.connectors.mail import MailAccount, MailConnector, MailRelayNotConfiguredError, create_mail_connector
from opi.core.cluster_config import get_mail_domain, get_mail_relay_host, get_mail_relay_namespace, get_mail_relay_port
from opi.manager.mail_manager import MailManager
from opi.services.catalog.approval import ApproverScope
from opi.services.catalog.base import ConfigLayer, DeploymentManifestContext
from opi.services.catalog.send_email import RELAY_POD_LABELS, SendEmailService
from opi.services.catalog.send_email.config_model import MAX_MESSAGES_PER_DAY, SendEmailConfig
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from opi.utils.secrets import SendEmailSecret
from pydantic import ValidationError
from ruamel.yaml import YAML

SERVICE = get_service(ServiceType.SEND_EMAIL)


def _approval(project: dict) -> dict:
    """Het opgeslagen goedkeuringsblok, uit het projectbestand zelf gelezen."""
    return project["services"][0]["config"]["approval"]


def _project(
    *,
    component_services: list[str] | None = None,
    config: dict | None = None,
    approval: str | None = "approved",
) -> dict:
    """A project with one deployment of one component, optionally using send-email.

    ``approval`` defaults to approved because that is the state in which the service does
    anything at all; the tests that care about the gate say so explicitly.
    """
    config = dict(config or {})
    if approval is not None:
        config["approval"] = {"status": approval, "history": []}
    return {
        "name": "myproject",
        "services": [{"name": ServiceType.SEND_EMAIL.value, "config": config}],
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

    def test_the_platform_written_fields_are_declared(self) -> None:
        """``approval`` too, and that one is not a nicety: a project that could set its own
        status to approved would make the approval no approval at all."""
        assert SERVICE.platform_managed_fields(ConfigLayer.PROJECT) == frozenset(
            {"accounts", "approval", "from-domain"}
        )

    def test_the_user_fields_are_not(self) -> None:
        managed = SERVICE.platform_managed_fields(ConfigLayer.PROJECT)
        assert "from-name" not in managed
        assert "from-local-part" not in managed
        assert "messages-per-day" not in managed

    def test_a_write_carrying_the_domain_is_refused(self) -> None:
        """Identity rule 2 of the plan: a project picks its display name, not its domain.

        Without the marking the field simply rode along in the generated PUT -- having no
        editable protects nothing, the API is the other door. And the approval does not
        cover it either: ``ensure_approval_requests`` stops as soon as an approval block
        exists, so after one verdict the domain could be changed without a second one.
        """
        from fastapi import HTTPException
        from opi.api.v2.router import _refuse_platform_managed

        managed = SERVICE.platform_managed_fields(ConfigLayer.PROJECT)
        with pytest.raises(HTTPException) as refused:
            _refuse_platform_managed(ServiceType.SEND_EMAIL.value, {"from-domain": "eigen.example"}, managed)
        assert refused.value.status_code == 422

        # ... while the display name a project DOES choose goes straight through.
        _refuse_platform_managed(ServiceType.SEND_EMAIL.value, {"from-name": "Algoritmeregister"}, managed)


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
        assert peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "rig-ron"
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
        """A domain gets there through the platform, never through the API (see
        ``TestTheAccountBlockIsPlatformData``); this is what the platform writing one does.

        SPF is checked against the ENVELOPE domain. Keeping the envelope on our own
        domain is exactly why a project domain costs one DKIM record instead of a full
        DNS set -- move the bounce along and that saving is gone."""
        sender, bounce = self._manager()._addresses("sandboxed-local", "myproject", {"from-domain": "eigen.example"})
        assert sender == "noreply@eigen.example"
        assert bounce == f"bounce+myproject@{get_mail_domain('sandboxed-local')}"


class TestHetOpgeschrevenAccountVeroudertNiet:
    """Het accountblok is het antwoord op "als wie verstuurt dit project".

    Het account wordt bij een tweede run bijgewerkt op de relay (``ensure_account`` is
    replay-veilig), dus als het projectbestand alleen bij de EERSTE run wordt geschreven,
    staat er daarna een afzenderadres in dat de relay niet meer afdwingt.
    """

    def _account(self, from_address: str = "noreply@mail.example") -> MailAccount:
        return MailAccount(
            username="myproject",
            from_address=from_address,
            bounce_address="bounce+myproject@mail.example",
            messages_per_day=500,
        )

    def test_an_unchanged_entry_makes_no_commit(self) -> None:
        entry = {
            "cluster": "sandboxed-local",
            "username": "myproject",
            "password": "AGE-VERSLEUTELD",
            "from-address": "noreply@mail.example",
            "bounce-address": "bounce+myproject@mail.example",
        }
        assert MailManager._entry_is_stale(entry, self._account()) is False

    def test_a_changed_sender_address_is_written_back(self) -> None:
        """Wat een project wel zelf kiest: het stuk voor de @."""
        entry = {
            "cluster": "sandboxed-local",
            "username": "myproject",
            "password": "AGE-VERSLEUTELD",
            "from-address": "noreply@mail.example",
            "bounce-address": "bounce+myproject@mail.example",
        }
        assert MailManager._entry_is_stale(entry, self._account("support@mail.example")) is True


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
    async def test_the_platform_account_is_skipped_without_a_relay(self, monkeypatch) -> None:
        """No relay yet simply means no platform mail yet; it must not stop the boot."""
        from opi.core.config import settings

        monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", "")
        assert await MailManager.ensure_platform_account() is None


class TestHetPlatformaccountIsEenGewoonAccount:
    """Aanvulling 4b: geen tweede soort account, en geen wachtwoord uit de bootstrap.

    Het wachtwoord bestaat pas nadat de relay draait, dus OPI maakt het zelf en bewaart het
    in een Secret in zijn EIGEN namespace. Wat hier vastligt:

    - een eerste opstart genereert, schrijft de Secret VOOR de relay-aanroep en maakt het
      account via dezelfde weg als een projectaccount;
    - een tweede opstart hergebruikt het bewaarde wachtwoord en vervangt niets stilzwijgend.
    """

    @pytest.fixture(autouse=True)
    def _relay(self, monkeypatch):
        from opi.core.config import settings

        monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", "http://relay")
        monkeypatch.setattr(settings, "MAIL_RELAY_ADMIN_PASSWORD", "plain:adminpw")
        monkeypatch.setattr(settings, "CLUSTER_MANAGER", "sandboxed-local")

    def _record_relay(self, monkeypatch) -> AsyncMock:
        """De ene weg waarlangs een account ontstaat, opgevangen zoals hij wordt gebruikt."""
        ensured = AsyncMock(
            return_value=MailAccount(
                username="zad-platform",
                from_address="noreply@mail.sandbox.rijksapp.dev",
                bounce_address="bounce+zad-platform@mail.sandbox.rijksapp.dev",
                messages_per_day=2000,
            )
        )
        monkeypatch.setattr(MailManager, "ensure_account", ensured)
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=object()))
        return ensured

    @pytest.mark.asyncio
    async def test_a_first_boot_generates_and_stores_before_it_calls_the_relay(self, monkeypatch) -> None:
        """De volgorde is de veiligheid: een wachtwoord dat alleen op de relay staat sluit
        ZAD buiten zijn eigen account, een dat alleen in de Secret staat repareert de
        volgende opstart zelf."""
        order: list[str] = []
        written: dict[str, str] = {}

        async def _read() -> dict[str, str] | None:
            return None

        async def _write(username: str, password: str, from_address: str) -> None:
            order.append("secret")
            written.update({"username": username, "password": password, "from-address": from_address})

        monkeypatch.setattr(MailManager, "_read_platform_secret", _read)
        monkeypatch.setattr(MailManager, "_write_platform_secret", _write)
        ensured = self._record_relay(monkeypatch)
        ensured.side_effect = lambda **kwargs: order.append("relay")

        await MailManager.ensure_platform_account()

        assert order == ["secret", "relay"]
        assert written["password"], "er moet een wachtwoord gegenereerd zijn"
        assert ensured.await_args.kwargs["password"] == written["password"]

    @pytest.mark.asyncio
    async def test_a_second_boot_reuses_the_stored_password_and_writes_nothing(self, monkeypatch) -> None:
        """Idempotent: geen tweede account, en geen nieuw wachtwoord dat nergens landt."""
        stored = {
            "username": "zad-platform",
            "password": "bewaard-wachtwoord",
            "from-address": "noreply@mail.sandbox.rijksapp.dev",
        }

        async def _read() -> dict[str, str] | None:
            return stored

        write = AsyncMock()
        monkeypatch.setattr(MailManager, "_read_platform_secret", _read)
        monkeypatch.setattr(MailManager, "_write_platform_secret", write)
        ensured = self._record_relay(monkeypatch)

        await MailManager.ensure_platform_account()

        write.assert_not_awaited()
        assert ensured.await_args.kwargs["password"] == "bewaard-wachtwoord"


class TestEenOnleesbareSecretRoteertNiets:
    """Rework r3: "niet gevonden" en "niet kunnen kijken" zijn niet hetzelfde.

    ``get_secret`` antwoordt ``None`` op een ontbrekende Secret ÉN op elke mislukte
    kubectl-aanroep (geen rechten, API-server weg, timeout). De opstartweg maakt van een
    ``None`` een NIEUW wachtwoord, dus één onleesbaar moment zou de Secret overschrijven en
    ZAD uit zijn eigen mailaccount roteren. Daarom bevestigt de lezer de afwezigheid.
    """

    @pytest.fixture(autouse=True)
    def _relay(self, monkeypatch):
        from opi.core.config import settings

        monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", "http://relay")
        monkeypatch.setattr(settings, "CLUSTER_MANAGER", "sandboxed-local")

    @pytest.mark.asyncio
    async def test_an_unreadable_secret_refuses_instead_of_generating(self, monkeypatch) -> None:
        from opi.connectors.kubectl import KubectlConnector, KubectlExecutionError

        monkeypatch.setattr(KubectlConnector, "get_secret", AsyncMock(return_value=None))
        # Bestaan onbekend: kubectl gaf een fout die geen NotFound is.
        monkeypatch.setattr(KubectlConnector, "secret_exists", AsyncMock(return_value=None))
        write = AsyncMock()
        monkeypatch.setattr(MailManager, "_write_platform_secret", write)
        ensured = AsyncMock()
        monkeypatch.setattr(MailManager, "ensure_account", ensured)
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=object()))

        with pytest.raises(KubectlExecutionError):
            await MailManager.ensure_platform_account()

        write.assert_not_awaited()
        ensured.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_startup_task_survives_that_refusal(self, monkeypatch) -> None:
        """En het blijft non-critical: de taak vangt ``KubectlExecutionError`` al, dus de
        boot gaat door en de volgende opstart leest de Secret gewoon terug."""
        from opi.connectors.kubectl import KubectlConnector
        from opi.core import startup

        monkeypatch.setattr(KubectlConnector, "get_secret", AsyncMock(return_value=None))
        monkeypatch.setattr(KubectlConnector, "secret_exists", AsyncMock(return_value=None))

        assert await startup.ensure_platform_mail_account() is False

    @pytest.mark.asyncio
    async def test_a_confirmed_absence_still_generates(self, monkeypatch) -> None:
        """De eerste opstart moet gewoon blijven werken: NotFound is een echt antwoord."""
        from opi.connectors.kubectl import KubectlConnector

        monkeypatch.setattr(KubectlConnector, "get_secret", AsyncMock(return_value=None))
        monkeypatch.setattr(KubectlConnector, "secret_exists", AsyncMock(return_value=False))
        write = AsyncMock()
        monkeypatch.setattr(MailManager, "_write_platform_secret", write)
        monkeypatch.setattr(MailManager, "ensure_account", AsyncMock(return_value=None))
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=object()))

        await MailManager.ensure_platform_account()

        write.assert_awaited_once()


class TestDeStartuptaakTrektDeBootNietOm:
    """Fase 3b is non-critical, en dat moet HIER waargemaakt worden.

    ``server.py`` doet ``await run_startup_tasks(app)`` zonder ``try``, dus een uitzondering
    die uit deze taak ontsnapt haalt fase 4 (Keycloak) en 5 (OAuth) onderuit. Een ingestelde
    maar onbereikbare relay geeft geen ``MailRelayError`` maar de transportfout van aiohttp,
    en die is er eerder dan er een HTTP-antwoord is om er een van te maken.
    """

    @pytest.mark.asyncio
    async def test_an_unreachable_relay_is_logged_and_not_raised(self, monkeypatch) -> None:
        from opi.core import startup

        async def _boom() -> None:
            raise aiohttp.ClientConnectorError(
                connection_key=SimpleNamespace(ssl=None, host="relay", port=443, is_ssl=True),  # type: ignore[arg-type]
                os_error=OSError("Network is unreachable"),
            )

        monkeypatch.setattr(MailManager, "ensure_platform_account", _boom)
        assert await startup.ensure_platform_mail_account() is False

    @pytest.mark.asyncio
    async def test_a_relay_that_refuses_the_call_is_logged_and_not_raised(self, monkeypatch) -> None:
        from opi.connectors.mail import MailRelayError
        from opi.core import startup

        async def _boom() -> None:
            raise MailRelayError("POST /api/principal gaf 500")

        monkeypatch.setattr(MailManager, "ensure_platform_account", _boom)
        assert await startup.ensure_platform_mail_account() is False

    @pytest.mark.asyncio
    async def test_a_dns_failure_is_logged_and_not_raised(self, monkeypatch) -> None:
        """``socket.gaierror`` is an ``OSError``, and it is what an unknown relay hostname
        gives before aiohttp has anything of its own to raise."""
        from opi.core import startup

        async def _boom() -> None:
            raise OSError("Name or service not known")

        monkeypatch.setattr(MailManager, "ensure_platform_account", _boom)
        assert await startup.ensure_platform_mail_account() is False


class TestTheClusterConfig:
    """The relay is addressed per cluster, like every other shared service."""

    @pytest.mark.parametrize(
        ("cluster", "namespace"),
        [("local", "rig-ron"), ("sandboxed-local", "rig-ron"), ("odcn-production", "rig-prd-ron")],
    )
    def test_every_cluster_knows_where_the_relay_is(self, cluster: str, namespace: str) -> None:
        """ODCN eist de clusterprefix op een namespace, dus daar heet hij rig-prd-ron.
        Dezelfde vorm als backup_namespace, en de host moet de namespace volgen -- een
        hostnaam die naar de andere namespace wijst resolvet niet."""
        assert get_mail_relay_namespace(cluster) == namespace
        assert get_mail_relay_host(cluster) == f"rig-mail-relay.{namespace}.svc.cluster.local"
        assert get_mail_relay_port(cluster) == 587
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
            host="rig-mail-relay.rig-ron.svc.cluster.local",
            port=587,
            username="myproject",
            password="geheim",
            from_address="noreply@mail.example",
        ).to_k8s_secret_data()
        assert data["SMTP_HOST"] == "rig-mail-relay.rig-ron.svc.cluster.local"
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


class TestNothingHappensWithoutApproval:
    """Aanvulling 6: geen aanvraag, in behandeling en afgewezen leveren alle drie NIETS op.

    Elk van de vier dingen wordt apart getoetst, want de hele reden dat er een enkele poort
    is, is dat ze niet uit elkaar kunnen lopen: een account zonder netwerkbeleid of
    andersom is de halve toestand die niemand kan uitleggen.
    """

    def _ctx(self, project: dict) -> DeploymentManifestContext:
        return DeploymentManifestContext(
            project_name="myproject",
            project_data=project,
            deployment=project["deployments"][0],
            cluster="sandboxed-local",
            namespace="rig-myproject",
        )

    @pytest.mark.parametrize("status", [None, "requested", "denied"])
    def test_geen_netwerkbeleid(self, status: str | None) -> None:
        project = _project(component_services=[ServiceType.SEND_EMAIL.value], approval=status)
        assert SERVICE.contribute_deployment_manifests(self._ctx(project)) == []

    @pytest.mark.parametrize("status", [None, "requested", "denied"])
    def test_geen_envfrom_en_geen_geheim(self, status: str | None) -> None:
        """Ook op de manifestweg, en niet alleen in de manager: die weg draait ook als het
        inrichten niets deed, dus zonder deze poort verwijst een deployment naar een geheim
        dat nooit geschreven is."""
        from opi.services.catalog.base import ManifestContext

        project = _project(component_services=[ServiceType.SEND_EMAIL.value], approval=status)
        ctx = ManifestContext(
            deployment_name="prod",
            project_data=project,
            unique_name="prod-web",
            cluster="sandboxed-local",
            get_secret=lambda *a, **k: SendEmailSecret(
                host="h", port=587, username="u", password="p", from_address="a@b"
            ),
            component_def=None,
        )
        contribution = SERVICE.contribute_manifest_context(ctx)
        assert contribution.env_from_secrets == []
        assert contribution.secret_files == []

    def test_goedgekeurd_zet_alles_wel_aan(self) -> None:
        """De tegenproef, zodat de drie tests hierboven niet groen zijn omdat de dienst
        uberhaupt niets doet."""
        from opi.services.catalog.base import ManifestContext

        project = _project(component_services=[ServiceType.SEND_EMAIL.value], approval="approved")
        assert SERVICE.contribute_deployment_manifests(self._ctx(project)) != []
        ctx = ManifestContext(
            deployment_name="prod",
            project_data=project,
            unique_name="prod-web",
            cluster="sandboxed-local",
            get_secret=lambda *a, **k: SendEmailSecret(
                host="h", port=587, username="u", password="p", from_address="a@b"
            ),
            component_def=None,
        )
        contribution = SERVICE.contribute_manifest_context(ctx)
        assert contribution.env_from_secrets == ["prod-send-email"]
        assert len(contribution.secret_files) == 1


class TestDeAanvraagLooptViaDeBestaandeWeg:
    """Geen eigen scherm en geen tweede mechanisme: dezelfde spec-weg als publish-on-web."""

    def test_de_dienst_declareert_een_goedkeuring_op_projectniveau(self) -> None:
        specs = SERVICE.config_approvals(ConfigLayer.PROJECT)
        assert [spec.key for spec in specs] == ["send-email"]
        assert specs[0].approver is ApproverScope.PLATFORM_ADMIN

    def test_de_dienst_staat_in_de_generieke_lijst_van_goedkeurders(self) -> None:
        """De poort op "geen tweede mechanisme": de beheerdersinterface loopt hierlangs."""
        from opi.services.registry import approval_services

        assert SERVICE in approval_services()

    def test_aanzetten_maakt_de_aanvraag(self) -> None:
        project = _project(approval=None)
        SERVICE.ensure_approval_requests(project)
        assert _approval(project) == {"status": "requested", "history": []}

    def test_nog_eens_aanzetten_verandert_niets(self) -> None:
        """Toestandsvormig en niet gebeurtenisvormig, dus elke schrijver mag hem aanroepen."""
        project = _project(approval="approved")
        SERVICE.ensure_approval_requests(project)
        assert _approval(project)["status"] == "approved"

    def test_zonder_de_dienst_geen_aanvraag(self) -> None:
        project = {"name": "myproject", "services": []}
        SERVICE.ensure_approval_requests(project)
        assert project["services"] == []

    def test_de_aanvraag_komt_in_de_generieke_lijst(self) -> None:
        from opi.services.approvals import collect_approval_items

        project = _project(approval="requested")
        items = [item for item in collect_approval_items(project) if item["type"] == "send-email"]
        assert len(items) == 1
        assert items[0]["name"] == "myproject"
        assert items[0]["current_status"] == "requested"

    def test_een_oordeel_landt_in_het_projectbestand(self) -> None:
        """Via apply_approval_verdicts, dus dezelfde weg die de beheerdersinterface loopt."""
        from opi.services.approvals import apply_approval_verdicts, collect_approval_items

        project = _project(approval="requested")
        items = collect_approval_items(project)
        for item in items:
            if item["type"] == "send-email":
                item["status"] = "approved"
        apply_approval_verdicts(project, items, admin_email="beheerder@example.nl")

        approval = _approval(project)
        assert approval["status"] == "approved"
        assert approval["history"][-1]["by"] == "beheerder@example.nl"


class TestDeWachtstandIsZichtbaar:
    """Een dienst die aanstaat en stil niets doet is de fout die bij de domeinaanvraag is
    weggehaald; hij mag hier niet terugkomen."""

    def _notices(self, project: dict):
        from opi.services.approvals import collect_deployment_approval_notices

        return [
            notice
            for notice in collect_deployment_approval_notices(project, project["deployments"][0])
            if notice["type"] == "send-email"
        ]

    @pytest.mark.parametrize(
        ("status", "kern"),
        [(None, "nog niet aangevraagd"), ("requested", "wacht op goedkeuring"), ("denied", "afgewezen")],
    )
    def test_elke_ongoedgekeurde_stand_meldt_zichzelf(self, status: str | None, kern: str) -> None:
        notices = self._notices(_project(component_services=[ServiceType.SEND_EMAIL.value], approval=status))
        assert len(notices) == 1
        assert kern in notices[0]["text"]

    def test_de_melding_zegt_ook_wat_het_betekent(self) -> None:
        """Alleen de status is geen melding: de gebruiker moet lezen dat er geen account,
        geen netwerktoegang en geen variabelen zijn."""
        notices = self._notices(_project(component_services=[ServiceType.SEND_EMAIL.value], approval="requested"))
        assert "geen SMTP-account" in notices[0]["text"]
        assert "SMTP_-variabelen" in notices[0]["text"]

    def test_goedgekeurd_meldt_niets(self) -> None:
        assert self._notices(_project(component_services=[ServiceType.SEND_EMAIL.value], approval="approved")) == []

    def test_zonder_de_dienst_meldt_niets(self) -> None:
        project = _project(component_services=[], approval=None)
        project["services"] = []
        assert self._notices(project) == []


class TestIntrekkenRuimtOp:
    """Het intrekken van een goedkeuring volgt hetzelfde opruimpad als een
    projectverwijdering, anders blijft er een weesaccount op de relay staan."""

    def _manager(self, saved: list) -> MailManager:
        from opi.handlers.project_file_handler import ProjectFileHandler

        async def get_name() -> str:
            return "myproject"

        async def save_and_commit_project(project_data, message, enforce_validation=True) -> None:
            saved.append(message)

        return MailManager(
            project_manager=SimpleNamespace(  # type: ignore[arg-type]
                _project_file_handler=ProjectFileHandler(),
                get_name=get_name,
                save_and_commit_project=save_and_commit_project,
                _add_secret_to_create=lambda *a, **k: None,
            )
        )

    @pytest.mark.asyncio
    async def test_een_ingetrokken_goedkeuring_verwijdert_het_account(self, monkeypatch) -> None:
        project = _project(component_services=[ServiceType.SEND_EMAIL.value], approval="denied")
        project["services"][0]["config"]["accounts"] = [
            {
                "cluster": "sandboxed-local",
                "username": "myproject",
                "password": "plain:geheim",
                "from-address": "noreply@mail.example",
                "bounce-address": "bounce+myproject@mail.example",
            }
        ]
        verwijderd: list[str] = []

        connector = MailConnector("http://relay", "admin", "geheim")
        connector.delete_principal = AsyncMock(side_effect=lambda naam: verwijderd.append(naam) or True)  # type: ignore[method-assign]
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=connector))

        saved: list[str] = []
        await self._manager(saved).create_resources_for_deployment(project, project["deployments"][0])

        assert verwijderd == ["myproject"]
        # En de vermelding gaat mee: laten staan toont een project een account dat het niet
        # heeft, en de volgende goedkeuring zou een wachtwoord hergebruiken dat de relay
        # niet meer kent.
        assert project["services"][0]["config"]["accounts"] == []
        assert saved, "het opruimen hoort te worden vastgelegd"

    @pytest.mark.asyncio
    async def test_zonder_account_valt_er_niets_op_te_ruimen(self, monkeypatch) -> None:
        """De status heeft geen geheugen van wat hij was, dus dit draait bij elke
        onverwerkte verwerking en moet dan niets doen."""
        project = _project(component_services=[ServiceType.SEND_EMAIL.value], approval="requested")
        connector_gemaakt = AsyncMock()
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", connector_gemaakt)

        saved: list[str] = []
        await self._manager(saved).create_resources_for_deployment(project, project["deployments"][0])

        connector_gemaakt.assert_not_awaited()
        assert saved == []


class TestDeAanvraagStaatGoedInDeBestaandeInterface:
    """Geen eigen scherm betekent dat het bestaande scherm hem moet KUNNEN tonen.

    Dat was niet vanzelf zo: beide goedkeuringssjablonen kozen hun opschrift met een
    ``if type == 'subdomain' else 'Domein'``, dus een derde soort aanvraag werd als
    "Domein" aangekondigd. Het opschrift komt nu van de spec (``label``).
    """

    def _item(self) -> dict:
        from opi.services.approvals import collect_approval_items

        project = _project(approval="requested")
        return next(item for item in collect_approval_items(project) if item["type"] == "send-email")

    def test_het_item_draagt_het_opschrift_van_de_spec(self) -> None:
        assert self._item()["label"] == "E-mail versturen"

    @pytest.mark.parametrize(
        "sjabloon",
        ["admin/approvals/_aanvragen.html.j2", "wizard/partials/approval_items.html.j2"],
    )
    def test_beide_schermen_noemen_de_aanvraag_bij_naam(self, sjabloon: str) -> None:
        from opi.core.templates_lotc import templates_lotc

        item = self._item()
        html = templates_lotc.env.get_template(sjabloon).render(
            projects_data=[{"project_name": "myproject", "approval_items": [item]}],
            _approval_items=[item],
        )
        assert "E-mail versturen" in html
