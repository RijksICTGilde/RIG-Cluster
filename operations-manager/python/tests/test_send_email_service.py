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

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestServer
from opi.connectors.mail import (
    MAIL_LIMITER_PREFIX,
    MAIL_SENDER_ADDRESS_PREFIX,
    MAIL_SENDER_NAME_PREFIX,
    MAIL_SENDER_SCRIPT_NAME,
    MAIL_SENDER_TABLE_KEY,
    MailAccount,
    MailConnector,
    MailRelayError,
    MailRelayNotConfiguredError,
    MailSenderNameError,
    create_mail_connector,
    render_sender_table,
)
from opi.core.cluster_config import (
    get_mail_from_address,
    get_mail_relay_host,
    get_mail_relay_namespace,
    get_mail_relay_port,
)
from opi.core.config import settings
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler
from opi.manager.mail_manager import MailAccountNameError, MailManager
from opi.services.catalog.approval import ApproverScope
from opi.services.catalog.base import ConfigLayer, DeploymentManifestContext
from opi.services.catalog.send_email import RELAY_POD_LABELS, RELAY_POD_PORT, SendEmailService
from opi.services.catalog.send_email.config_model import MAX_MESSAGES_PER_DAY, SendEmailConfig
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from opi.utils.naming import (
    MAIL_LOCAL_PART_MAX_LENGTH,
    MAIL_PROJECT_ACCOUNT_PREFIX,
    MAIL_SENDER_DOMAIN_ALLOWLIST,
    generate_mail_account_name,
)
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
        assert config.accounts == []

    def test_er_is_geen_veld_voor_het_afzenderadres(self) -> None:
        """Het adres draagt sinds RC-145 wel het project, maar het is nog steeds niets dat
        een project ZELF kiest: het platform leidt het af van de projectnaam.

        Niet netheid maar noodzaak: `rijksoverheid.nl` publiceert p=reject en wij
        ondertekenen niet met DKIM, dus SPF-uitlijning tussen envelope en From: is het
        enige dat een bericht door DMARC krijgt. Een zelfgekozen adres - en zeker een
        zelfgekozen domein - breekt precies dat. Deze test valt om zodra iemand het veld
        terugzet.
        """
        velden = set(SendEmailConfig.model_fields) | {f.alias for f in SendEmailConfig.model_fields.values() if f.alias}
        assert "from_local_part" not in velden
        assert "from-local-part" not in velden
        assert "from_domain" not in velden
        assert "from-domain" not in velden
        # De weergavenaam blijft wel van het project.
        assert "from_name" in SendEmailConfig.model_fields

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
            {"accounts", "approval", "from-address-approval", "messages-per-day-approval"}
        )

    def test_the_user_fields_are_not(self) -> None:
        managed = SERVICE.platform_managed_fields(ConfigLayer.PROJECT)
        assert "from-name" not in managed
        assert "messages-per-day" not in managed

    def test_de_weergavenaam_gaat_wel_gewoon_door(self) -> None:
        """De naam is het enige dat een project kiest, en die is niet platform-managed."""
        from opi.api.v2.router import _refuse_platform_managed

        managed = SERVICE.platform_managed_fields(ConfigLayer.PROJECT)
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
        # De PODpoort, niet get_mail_relay_port (de servicepoort, 587): een NetworkPolicy
        # wordt na de service-DNAT beoordeeld en ziet dus 2525. Met 587 matchte de regel
        # niets en liep elke verbinding vanuit een component stuk op een timeout.
        assert egress[0]["ports"] == [RELAY_POD_PORT]

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
        # De weergavenaam is het tweede dat de relay moet horen: het ADRES leidt hij zelf
        # af uit de accountnaam, de naam valt nergens uit af te leiden.
        connector.set_sender = AsyncMock(return_value=True)  # type: ignore[method-assign]
        connector.set_rate_limit = AsyncMock(return_value=True)  # type: ignore[method-assign]
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
            from_name="",
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
            from_name="",
            messages_per_day=800,
        )
        connector.create_principal.assert_not_awaited()
        connector.update_principal.assert_awaited_once()
        # Beide adressen gaan mee: de relay herschrijft de envelope naar het bounce-adres
        # Adressen gaan NIET meer naar de relay: must-match-sender staat uit en een adres
        # zou een lokaal domein vereisen. Alleen naam en wachtwoord.
        assert set(connector.update_principal.await_args.kwargs) == {"name", "password"}

    def test_the_platform_caller_needs_no_project(self) -> None:
        """A staticmethod, so ZAD's account goes through the very same code without a
        project file. Make it an instance method again and the platform side needs a
        second implementation -- which is the failure mode the plan names."""
        assert isinstance(MailManager.__dict__["ensure_account"], staticmethod)

    @pytest.mark.asyncio
    async def test_er_wordt_geen_domein_geregistreerd(self) -> None:
        """Het afzenderdomein is `rijksoverheid.nl` en dat mag de relay NOOIT als lokaal
        domein kennen.

        Stalwart kiest zijn route per ontvanger en bezorgt een lokaal domein lokaal, dus
        een geregistreerd `rijksoverheid.nl` zou mail AAN collega's daar in onze eigen
        opslag laten verdwijnen in plaats van naar de upstream sturen. Precies de meest
        voorkomende ontvanger, en het zou stil misgaan. Daarom draagt het account ook geen
        adressen meer en staat must-match-sender uit.
        """
        connector = self._connector(existing=None)
        await MailManager.ensure_account(
            connector=connector,
            username="project-myproject",
            password="geheim",
            from_address="noreply-rijksapp@rijksoverheid.nl",
            bounce_address="noreply-rijksapp+project-myproject@rijksoverheid.nl",
            from_name="",
            messages_per_day=500,
        )
        assert not hasattr(MailConnector, "ensure_domain"), (
            "de connector mag geen weg meer hebben om een domein bij de relay te registreren"
        )
        assert set(connector.create_principal.await_args.kwargs) == {"name", "password"}


class TestHetPlatformaccountIsGeenProjectaccount:
    """Securityreview r8: de relay heeft EEN platte accountnaamruimte.

    Het platformaccount van ZAD (``MAIL_PLATFORM_ACCOUNT``) staat naast de projectaccounts.
    Zou een project dezelfde naam kunnen krijgen, dan kan het met goedkeuring het
    wachtwoord en het afzenderadres van ZAD overnemen (``ensure_account`` werkt een
    bestaand principal BIJ) en zonder goedkeuring het account van het platform verwijderen
    (``_delete_account``). Twee dingen houden dat tegen, en beide worden hier vastgelegd.
    """

    def _connector(self) -> MailConnector:
        connector = MailConnector("http://relay", "admin", "geheim")
        connector.get_principal = AsyncMock(return_value={"name": "zad-platform"})  # type: ignore[method-assign]
        connector.create_principal = AsyncMock()  # type: ignore[method-assign]
        connector.update_principal = AsyncMock()  # type: ignore[method-assign]
        connector.delete_principal = AsyncMock(return_value=True)  # type: ignore[method-assign]
        connector.set_sender = AsyncMock(return_value=True)  # type: ignore[method-assign]
        connector.set_rate_limit = AsyncMock(return_value=True)  # type: ignore[method-assign]
        connector.delete_sender_name = AsyncMock()  # type: ignore[method-assign]
        connector.delete_rate_limit = AsyncMock()  # type: ignore[method-assign]
        return connector

    def test_a_project_account_never_carries_the_platform_name(self) -> None:
        """De namen zijn disjunct door de constructie: elk projectaccount draagt het
        voorvoegsel, dus zelfs een project dat 'zad-platform' HEET krijgt een eigen
        account."""
        from opi.utils.naming import MAIL_PROJECT_ACCOUNT_PREFIX, generate_mail_account_name

        assert generate_mail_account_name("zad-platform") == f"{MAIL_PROJECT_ACCOUNT_PREFIX}zad-platform"
        assert generate_mail_account_name("zad-platform") != settings.MAIL_PLATFORM_ACCOUNT
        # En injectief: twee projecten komen nooit op een account uit.
        assert generate_mail_account_name("demo") != generate_mail_account_name("demo-twee")

    @pytest.mark.asyncio
    async def test_the_project_path_refuses_the_platform_name(self) -> None:
        """Het voorvoegsel alleen is niet genoeg: de naam komt op de projectweg ook uit het
        PROJECTBESTAND (``_revoke``), en dat is de plek waar hij niet berekend wordt."""
        from opi.manager.mail_manager import MailAccountNameError

        with pytest.raises(MailAccountNameError):
            await MailManager.ensure_account(
                connector=self._connector(),
                username=settings.MAIL_PLATFORM_ACCOUNT,
                password="overgenomen",
                from_address=f"noreply.{settings.MAIL_PLATFORM_ACCOUNT}@mail.example",
                bounce_address=f"bounce+{settings.MAIL_PLATFORM_ACCOUNT}@mail.example",
                from_name="",
                messages_per_day=500,
            )

    @pytest.mark.asyncio
    async def test_the_platform_caller_itself_is_allowed(self) -> None:
        """De weigering mag ZAD's eigen weg niet dichtzetten: alleen die zegt het zelf."""
        connector = self._connector()
        account = await MailManager.ensure_account(
            connector=connector,
            username=settings.MAIL_PLATFORM_ACCOUNT,
            password="geheim",
            from_address=f"noreply.{settings.MAIL_PLATFORM_ACCOUNT}@mail.example",
            bounce_address=f"bounce+{settings.MAIL_PLATFORM_ACCOUNT}@mail.example",
            from_name="",
            messages_per_day=2000,
            is_platform_account=True,
        )
        assert account.username == settings.MAIL_PLATFORM_ACCOUNT

    @pytest.mark.asyncio
    async def test_the_removal_never_takes_the_platform_account(self, monkeypatch) -> None:
        """Verwijderen is altijd een PROJECTverwijdering: het platformaccount heeft geen
        levenscyclus die eindigt. Dus geen uitzondering, voor niemand."""
        from opi.manager.mail_manager import MailAccountNameError

        connector = self._connector()
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=connector))
        manager = MailManager(project_manager=SimpleNamespace())  # type: ignore[arg-type]

        with pytest.raises(MailAccountNameError):
            await manager._delete_account(settings.MAIL_PLATFORM_ACCOUNT)

        connector.delete_principal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_platform_name_inside_the_project_prefix_is_refused(self, monkeypatch) -> None:
        """De andere kant van dezelfde botsing: wie ``MAIL_PLATFORM_ACCOUNT`` IN de
        projectnaamruimte zet, maakt hem weer bereikbaar voor een project. Dan gaat de
        projectweg dicht, niet het platformaccount open."""
        from opi.manager.mail_manager import MailAccountNameError

        monkeypatch.setattr(settings, "MAIL_PLATFORM_ACCOUNT", "project-zad")
        with pytest.raises(MailAccountNameError):
            await MailManager.ensure_account(
                connector=self._connector(),
                username="project-demo",
                password="geheim",
                from_address="noreply.project-demo@mail.example",
                bounce_address="bounce+project-demo@mail.example",
                from_name="",
                messages_per_day=500,
            )


class TestTheAddresses:
    """De afzender IS het project, en envelope en From: zijn hetzelfde adres."""

    def _manager(self) -> MailManager:
        return MailManager(project_manager=SimpleNamespace())  # type: ignore[arg-type]

    def test_elk_project_verstuurt_van_zijn_eigen_adres(self) -> None:
        """Het plusdeel draagt de PROJECTnaam, dus twee projecten delen geen afzender.

        Dit is de kern van de afspraak met het mailteam: een bericht komt herkenbaar van
        een project. Zolang dit adres voor iedereen gelijk was, was elke ontvanger op het
        platform aangewezen op de weergavenaam om te zien wie schreef.
        """
        een = MailManager._sender_address("sandboxed-local", "een")
        twee = MailManager._sender_address("sandboxed-local", "twee")
        assert een == "noreply-rijksapp+een@rijksoverheid.nl"
        assert twee == "noreply-rijksapp+twee@rijksoverheid.nl"
        assert een != twee

    def test_het_plusdeel_draagt_het_project_en_niet_het_account(self) -> None:
        """Het account heet ``project-myproject``; het adres zegt ``myproject``.

        Wie hier de accountnaam neemt, zet het voorvoegsel ``project-`` in elk
        afzenderadres van het platform - zichtbaar voor elke ontvanger, en het zegt niets.
        """
        adres = MailManager._sender_address("sandboxed-local", "myproject")
        assert adres == "noreply-rijksapp+myproject@rijksoverheid.nl"
        assert generate_mail_account_name("myproject") not in adres

    def test_envelope_en_from_zijn_hetzelfde_adres(self) -> None:
        """Ze verschilden een voorvoegsel en dat verschil diende niets.

        SPF-uitlijning kijkt naar het DOMEIN, dus het scheelde niets voor DMARC, terwijl de
        ontvanger in de From: geen project zag. Nu zijn ze gelijk, en dat maakt de
        uitlijning triviaal waar dan ook.
        """
        adres = MailManager._sender_address("sandboxed-local", "myproject")
        assert adres.partition("@")[2] == get_mail_from_address("sandboxed-local").partition("@")[2]

    def test_het_platformaccount_krijgt_het_kale_adres(self) -> None:
        """ZAD is geen project en heeft er dus geen om naar te wijzen.

        Het kale adres valt SAMEN met de terugval van de relay (een account zonder
        opgezochte afzender), dus het platformaccount vraagt nergens een uitzondering.
        """
        assert MailManager._sender_address("sandboxed-local", None) == get_mail_from_address("sandboxed-local")

    def test_een_lange_projectnaam_past_nog_in_het_lokale_deel(self) -> None:
        """De valkuil die niemand narekent: ``noreply-rijksapp+`` is zeventien tekens en
        een lokaal deel mag er vierenzestig.

        Zonder afkapping levert een projectnaam van meer dan zevenenveertig tekens een
        adres op dat de upstream weigert, en dat merk je pas bij het eerste bericht.
        """
        naam = "p" * 80
        adres = MailManager._sender_address("sandboxed-local", naam)
        lokaal, _, domein = adres.partition("@")
        assert len(lokaal) == 64
        assert lokaal.startswith("noreply-rijksapp+p")
        assert domein == "rijksoverheid.nl"

    def test_een_gewone_naam_wordt_niet_afgekapt(self) -> None:
        """De keerzijde van de vorige toets: afkappen mag alleen gebeuren als het moet."""
        adres = MailManager._sender_address("sandboxed-local", "a" * 47)
        assert adres.partition("@")[0] == "noreply-rijksapp+" + "a" * 47

    def test_de_relay_leidt_hetzelfde_adres_af_als_opi(self) -> None:
        """DE INVARIANT WAAR ALLES OP RUST, en de reden dat het label een constante is.

        De relay kan tijdens het aannemen van een bericht niets per account opzoeken - geen
        enkele weg in Stalwart v0.11.8 kan dat, gemeten - dus hij LEIDT het adres af: hij
        knipt ``project-`` van de accountnaam en plakt dat achter het plusdeel. Wat OPI
        hier uitrekent is daarmee een mededeling aan de ontwikkelaar (SMTP_FROM), en die
        mededeling moet kloppen.

        Kapt OPI het label in de accountnaam op een andere lengte dan in het adres, dan
        lopen ze uiteen zodra een projectnaam lang genoeg is - en merkt niemand het, want
        alleen dat ene project ziet het.
        """
        for naam in ("ai1-uit", "a", "p" * 47, "p" * 80, "Hoofdletters-Erin"):
            account = generate_mail_account_name(naam)
            adres = MailManager._sender_address("sandboxed-local", naam)
            afgeleid = account.removeprefix(MAIL_PROJECT_ACCOUNT_PREFIX)
            assert adres.partition("+")[2].partition("@")[0] == afgeleid, (
                f"voor {naam!r} leidt de relay een ander adres af dan OPI meldt"
            )

    def test_het_adres_past_op_elk_cluster_binnen_het_lokale_deel(self) -> None:
        """De grens die niemand narekent: een lokaal deel mag 64 tekens.

        Het label is op 47 gekapt omdat ``noreply-rijksapp+`` zeventien tekens is. Krijgt
        een cluster ooit een langer basisadres, dan loopt het adres eroverheen en weigert de
        upstream het - deze toets valt dan om in plaats van de post.
        """
        for cluster in ("local", "sandboxed-local", "odcn-production"):
            adres = MailManager._sender_address(cluster, "p" * 200)
            assert len(adres.partition("@")[0]) <= MAIL_LOCAL_PART_MAX_LENGTH, f"{cluster}: lokaal deel te lang"

    def test_de_relayconfiguratie_knipt_hetzelfde_voorvoegsel(self) -> None:
        """De afleiding staat in een andere taal in een ander bestand, dus hier is de
        grendel: verandert het voorvoegsel in Python, dan valt deze toets om tot de
        configmap meeverhuist.

        Zonder deze toets zou een hernoeming van ``MAIL_PROJECT_ACCOUNT_PREFIX`` elk
        afzenderadres op het platform stil veranderen in iets dat OPI niet meldt.

        Dezelfde grendel op de NAAM van het gegenereerde script, en daar is de faalstand nog
        stiller: ``include :optional`` slaat een script dat niet bestaat woordeloos over, dus
        bij drift verstuurt elk project gewoon zonder weergavenaam en meldt niets dat.
        """
        # config.toml en niet configmap.yaml: de relayconfiguratie is een
        # configMapGenerator geworden ("een configwijziging van de relay is vanzelf een
        # rollout"), en de inhoud staat sindsdien als los bestand naast de kustomization.
        # Deze toets las nog het verdwenen bestand en viel om op een FileNotFoundError -
        # rood, maar niet om de reden die hij bewaakt.
        configmap = (
            Path(__file__).resolve().parents[3]
            / "infrastructure/bootstrap/infrastructure/mail/controller/base/config.toml"
        ).read_text()
        assert f"strip_prefix(authenticated_as, '{MAIL_PROJECT_ACCOUNT_PREFIX}')" in configmap
        assert f"strip_prefix(account, '{MAIL_PROJECT_ACCOUNT_PREFIX}')" in configmap
        assert f"starts_with(authenticated_as, '{MAIL_PROJECT_ACCOUNT_PREFIX}')" in configmap
        assert f'include :optional :personal "{MAIL_SENDER_SCRIPT_NAME}"' in configmap, (
            "de relay sluit een ander script in dan OPI schrijft"
        )

    def test_alle_clusters_gebruiken_hetzelfde_basisadres(self) -> None:
        """Het BASISadres blijft overal gelijk; alleen het plusdeel verschilt per project.

        Wijkt een cluster af, dan zegt OPI iets anders dan de relay afdwingt en ziet een
        ontwikkelaar een adres dat nooit vertrekt.
        """
        adressen = {get_mail_from_address(c) for c in ("local", "sandboxed-local", "odcn-production")}
        assert adressen == {"noreply-rijksapp@rijksoverheid.nl"}


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
            "from-address": "noreply-rijksapp@rijksoverheid.nl",
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
    maar onbereikbare relay komt sinds RC-261 als ``MailRelayUnavailableError`` uit de
    connector, maar de kale transportfouten blijven nodig: de kubectl-aanroep en het
    ontsleutelen eromheen gooien ze nog steeds zelf, en daarom worden ze hier geworpen.
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

    @pytest.mark.asyncio
    async def test_an_unreachable_api_server_is_logged_and_not_raised(self, monkeypatch) -> None:
        """``KubectlConnectionError`` is GEEN subklasse van ``KubectlExecutionError``, en het
        is juist de toestand waar de kubectl-connector zijn eigen herhaallus voor heeft: de
        API-server is (nog) niet bereikbaar terwijl OPI opstart."""
        from opi.connectors.kubectl import KubectlConnectionError
        from opi.core import startup

        async def _boom() -> None:
            raise KubectlConnectionError("kubectl connection is not available")

        monkeypatch.setattr(MailManager, "ensure_platform_account", _boom)
        assert await startup.ensure_platform_mail_account() is False

    @pytest.mark.asyncio
    async def test_an_undecryptable_admin_password_is_logged_and_not_raised(self, monkeypatch) -> None:
        """``create_mail_connector`` ontsleutelt ``MAIL_RELAY_ADMIN_PASSWORD``; een waarde die
        nog niet te ontsleutelen is geeft een kale ``ValueError``. Dat is de waarschijnlijke
        eerste toestand na het aanzetten van ``MAIL_RELAY_API_URL``, geen randgeval."""
        from opi.core import startup

        async def _boom() -> None:
            raise ValueError("Failed to decrypt password: no matching AGE key")

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

    def test_production_sends_from_the_fixed_address(self) -> None:
        """Geen eigen maildomein: we versturen via de mailserver van de Rijksoverheid en
        dragen daarom hun domein. Zie docs/ron-koppeling.md."""
        assert get_mail_from_address("odcn-production") == "noreply-rijksapp@rijksoverheid.nl"


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
        assert [spec.key for spec in specs] == [
            "send-email",
            "from-address-approval",
            "messages-per-day-approval",
        ]
        assert {spec.approver for spec in specs} == {ApproverScope.PLATFORM_ADMIN}

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
        connector.delete_sender_name = AsyncMock()  # type: ignore[method-assign]
        connector.delete_rate_limit = AsyncMock()  # type: ignore[method-assign]
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=connector))

        saved: list[str] = []
        await self._manager(saved).create_resources_for_deployment(project, project["deployments"][0])

        assert verwijderd == ["myproject"]
        # En de vermelding gaat mee: laten staan toont een project een account dat het niet
        # heeft, en de volgende goedkeuring zou een wachtwoord hergebruiken dat de relay
        # niet meer kent.
        assert project["services"][0]["config"]["accounts"] == []
        assert saved, "het opruimen hoort te worden vastgelegd"
        # En de limiter, om dezelfde reden: blijft hij staan, dan geldt hij voor het
        # volgende account dat zo gaat heten (RC-400, toets 6).
        connector.delete_rate_limit.assert_awaited_once_with("myproject")

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
        from opi.web.router_approvals import groepeer_per_dienst

        item = self._item()
        html = templates_lotc.env.get_template(sjabloon).render(
            # De beheerpagina leest GROEPEN (per dienst) en de wizardpartial de losse
            # items. Allebei meegeven, want dit is een toets op twee schermen en de
            # groepen komen uit dezelfde functie als op de echte pagina.
            projects_data=[
                {
                    "project_name": "myproject",
                    "approval_items": [item],
                    "approval_groups": groepeer_per_dienst([item]),
                }
            ],
            _approval_items=[item],
        )
        assert "E-mail versturen" in html


class TestDeAanvraagVoorEenEigenAdres:
    """RC-399: een opgegeven adres IS de aanvraag, en hangt aan de WAARDE.

    ``ensure_approval_requests`` roept zowel de bestaande dienstgoedkeuring als
    ``_ensure_from_address_request`` aan; deze suite dekt de tweede.
    """

    def _blok(self, project: dict) -> dict | None:
        from opi.services.catalog.send_email import FROM_ADDRESS_APPROVAL

        return project["services"][0]["config"].get(FROM_ADDRESS_APPROVAL.spec.key)

    def test_een_adres_invullen_maakt_de_aanvraag(self) -> None:
        project = _project(config={"from-address": "noreply@rijksoverheid.nl"})
        SERVICE.ensure_approval_requests(project)
        assert self._blok(project) == {
            "value": "noreply@rijksoverheid.nl",
            "status": "requested",
            "history": [],
        }

    def test_nog_eens_aanroepen_verandert_niets(self) -> None:
        """Toestandsvormig: dezelfde waarde opnieuw aanbieden mag een goedkeuring niet
        terugzetten naar 'requested'."""
        project = _project(config={"from-address": "noreply@rijksoverheid.nl"})
        project["services"][0]["config"]["from-address-approval"] = {
            "value": "noreply@rijksoverheid.nl",
            "status": "approved",
            "history": [{"status": "approved", "by": "beheerder@example.nl"}],
        }
        SERVICE.ensure_approval_requests(project)
        assert self._blok(project)["status"] == "approved"
        assert len(self._blok(project)["history"]) == 1

    def test_het_adres_wijzigen_na_goedkeuring_maakt_een_nieuwe_aanvraag(self) -> None:
        """Dit is aanvulling 29 oktober 2026: de oude goedkeuring gaat niet mee."""
        project = _project(config={"from-address": "nieuw@rijksoverheid.nl"})
        project["services"][0]["config"]["from-address-approval"] = {
            "value": "oud@rijksoverheid.nl",
            "status": "approved",
            "history": [{"status": "approved", "by": "beheerder@example.nl"}],
        }
        SERVICE.ensure_approval_requests(project)
        blok = self._blok(project)
        assert blok == {"value": "nieuw@rijksoverheid.nl", "status": "requested", "history": []}

    def test_het_veld_leegmaken_trekt_de_aanvraag_in(self) -> None:
        project = _project(config={})
        project["services"][0]["config"]["from-address-approval"] = {
            "value": "oud@rijksoverheid.nl",
            "status": "approved",
            "history": [{"status": "approved"}],
        }
        SERVICE.ensure_approval_requests(project)
        assert self._blok(project) is None

    def test_zonder_de_dienst_geen_aanvraag(self) -> None:
        project = {"name": "myproject", "services": []}
        SERVICE.ensure_approval_requests(project)
        assert project["services"] == []


class TestDeAfzenderadresAanvraagInDeGenericeLijst:
    """Dezelfde generieke weg als de dienstgoedkeuring (``collect_approval_items`` /
    ``apply_approval_verdicts``), nu voor de tweede spec."""

    def test_de_aanvraag_komt_in_de_lijst(self) -> None:
        from opi.services.approvals import collect_approval_items

        project = _project(config={"from-address": "noreply@rijksoverheid.nl"})
        SERVICE.ensure_approval_requests(project)
        items = [item for item in collect_approval_items(project) if item["type"] == "from-address-approval"]
        assert len(items) == 1
        assert items[0]["subject"] == "noreply@rijksoverheid.nl"
        assert items[0]["name"] == "myproject"
        assert items[0]["current_status"] == "requested"

    def test_zonder_aanvraag_staat_er_niets_in_de_lijst(self) -> None:
        from opi.services.approvals import collect_approval_items

        project = _project(config={})
        items = [item for item in collect_approval_items(project) if item["type"] == "from-address-approval"]
        assert items == []

    def test_een_oordeel_landt_in_het_blok_met_geschiedenis(self) -> None:
        from opi.services.approvals import apply_approval_verdicts, collect_approval_items

        project = _project(config={"from-address": "noreply@rijksoverheid.nl"})
        SERVICE.ensure_approval_requests(project)
        items = collect_approval_items(project)
        for item in items:
            if item["type"] == "from-address-approval":
                item["status"] = "approved"
        apply_approval_verdicts(project, items, admin_email="beheerder@example.nl")

        blok = project["services"][0]["config"]["from-address-approval"]
        assert blok["status"] == "approved"
        assert blok["history"][-1]["by"] == "beheerder@example.nl"

    def test_een_oordeel_over_een_ander_adres_schrijft_niets(self) -> None:
        """De waardebinding op de SCHRIJFweg: het sjabloon POST ``subject`` niet, dus het
        oordeel draagt het beoordeelde adres in ``domain``. Wijzigt het project het veld
        tussen lijst en insturen, dan hoort het oordeel bij een adres dat er niet meer is."""
        from opi.services.approvals import apply_approval_verdicts, collect_approval_items
        from opi.services.catalog.send_email import approved_from_address

        project = _project(config={"from-address": "a@rijksoverheid.nl"})
        SERVICE.ensure_approval_requests(project)
        ingestuurd = [
            {k: v for k, v in item.items() if k in {"service", "type", "label", "domain", "name", "current_status"}}
            | {"status": "approved", "message": ""}
            for item in collect_approval_items(project)
            if item["type"] == "from-address-approval"
        ]

        project["services"][0]["config"]["from-address"] = "b@rijksoverheid.nl"
        SERVICE.ensure_approval_requests(project)
        apply_approval_verdicts(project, ingestuurd, admin_email="beheerder@example.nl")

        blok = project["services"][0]["config"]["from-address-approval"]
        assert blok == {"value": "b@rijksoverheid.nl", "status": "requested", "history": []}
        assert approved_from_address(project) == ""


class TestDeBudgetaanvraagLooptViaDeBestaandeWeg:
    """RC-400: hetzelfde generieke mechanisme als het afzenderadres, met een eigen regel
    erbovenop: alleen een getal BOVEN de standaard is een aanvraag, en een goedgekeurd
    getal laat zijn ``approved-value`` achter voor de volgende, hogere aanvraag."""

    def _blok(self, project: dict) -> dict | None:
        return project["services"][0]["config"].get("messages-per-day-approval")

    def test_een_getal_binnen_de_standaard_maakt_geen_aanvraag(self) -> None:
        project = _project(config={"messages-per-day": settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY})
        SERVICE.ensure_approval_requests(project)
        assert self._blok(project) is None

    def test_een_getal_boven_de_standaard_maakt_de_aanvraag(self) -> None:
        project = _project(config={"messages-per-day": 5000})
        SERVICE.ensure_approval_requests(project)
        assert self._blok(project) == {"value": "5000", "status": "requested", "history": []}

    def test_verlagen_onder_de_standaard_trekt_de_aanvraag_in(self) -> None:
        project = _project(config={"messages-per-day": 5000})
        SERVICE.ensure_approval_requests(project)
        project["services"][0]["config"]["messages-per-day"] = 10
        SERVICE.ensure_approval_requests(project)
        assert self._blok(project) is None

    def test_een_goedkeuring_en_een_nieuwe_hogere_aanvraag_dragen_het_laatst_goedgekeurde_getal_mee(self) -> None:
        """De kern van de aanname in ``TestDeBudgetpoortOpDeWaarde``, maar dan getoetst via
        de WERKELIJKE aanvraag- en oordeelcyclus in plaats van een handgebouwd blok."""
        from opi.services.approvals import apply_approval_verdicts, collect_approval_items
        from opi.services.catalog.send_email import effective_messages_per_day

        project = _project(config={"messages-per-day": 5000})
        SERVICE.ensure_approval_requests(project)
        items = collect_approval_items(project)
        for item in items:
            if item["type"] == "messages-per-day-approval":
                item["status"] = "approved"
        apply_approval_verdicts(project, items, admin_email="beheerder@example.nl")

        assert effective_messages_per_day(project) == 5000

        # Het project vraagt nu meer: een NIEUWE, nog niet beoordeelde aanvraag.
        project["services"][0]["config"]["messages-per-day"] = 20000
        SERVICE.ensure_approval_requests(project)

        blok = self._blok(project)
        assert blok["status"] == "requested"
        assert blok["value"] == "20000"
        assert blok.get("approved-value") == "5000", (
            "de laatst goedgekeurde waarde moet meegaan naar het nieuwe blok, anders verlaagt "
            "een openstaande aanvraag wat het project al mocht"
        )
        assert effective_messages_per_day(project) == 5000, "tot de 20000 is goedgekeurd, geldt de oude 5000"

    def test_verlagen_binnen_een_goedgekeurd_getal_vraagt_niets_en_geldt_meteen(self) -> None:
        """Verlagen vraagt geen goedkeuring, ook niet naar een getal boven de standaard: de
        bestaande goedkeuring blijft staan en het lagere getal geldt direct."""
        from opi.services.approvals import apply_approval_verdicts, collect_approval_items
        from opi.services.catalog.send_email import effective_messages_per_day

        project = _project(config={"messages-per-day": 20000})
        SERVICE.ensure_approval_requests(project)
        items = collect_approval_items(project)
        for item in items:
            if item["type"] == "messages-per-day-approval":
                item["status"] = "approved"
        apply_approval_verdicts(project, items, admin_email="beheerder@example.nl")
        assert effective_messages_per_day(project) == 20000

        project["services"][0]["config"]["messages-per-day"] = 5000
        SERVICE.ensure_approval_requests(project)

        blok = self._blok(project)
        assert blok["status"] == "approved", "een verlaging mag geen nieuwe aanvraag openen"
        assert blok["value"] == "20000"
        assert blok.get("approved-value") == "20000"
        assert effective_messages_per_day(project) == 5000

    def test_verlagen_met_een_openstaande_verhoging_ertussen_laat_geen_aanvraag_achter(self) -> None:
        """Dezelfde verlaging, maar nadat het project eerst hoger vroeg. Het blok draagt dan
        een ANDER getal, dus het wordt opnieuw gebouwd: terug bij het goedgekeurde getal is
        dat de staande goedkeuring, niet een aanvraag over een getal dat het project al mag."""
        from opi.services.approvals import apply_approval_verdicts, collect_approval_items
        from opi.services.catalog.send_email import effective_messages_per_day

        project = _project(config={"messages-per-day": 20000})
        SERVICE.ensure_approval_requests(project)
        items = collect_approval_items(project)
        for item in items:
            if item["type"] == "messages-per-day-approval":
                item["status"] = "approved"
        apply_approval_verdicts(project, items, admin_email="beheerder@example.nl")

        project["services"][0]["config"]["messages-per-day"] = 50000
        SERVICE.ensure_approval_requests(project)
        assert self._blok(project)["status"] == "requested"

        project["services"][0]["config"]["messages-per-day"] = 5000
        SERVICE.ensure_approval_requests(project)

        blok = self._blok(project)
        assert blok["status"] == "approved", (
            "de openstaande 50000 is ingetrokken, niet vervangen door een nieuwe aanvraag"
        )
        assert blok["value"] == "20000"
        assert blok.get("approved-value") == "20000"
        assert effective_messages_per_day(project) == 5000

    def test_terug_naar_een_goedgekeurd_adres_vraagt_niets_opnieuw(self) -> None:
        """Dezelfde regel op de andere waarde van deze dienst: een adres dat al eens is
        goedgekeurd blijft goedgekeurd als het project er na een wijziging op terugkomt."""
        from opi.services.approvals import apply_approval_verdicts, collect_approval_items
        from opi.services.catalog.send_email import approved_from_address

        project = _project(config={"from-address": "a@rijksoverheid.nl"})
        SERVICE.ensure_approval_requests(project)
        items = collect_approval_items(project)
        for item in items:
            if item["type"] == "from-address-approval":
                item["status"] = "approved"
        apply_approval_verdicts(project, items, admin_email="beheerder@example.nl")

        project["services"][0]["config"]["from-address"] = "b@rijksoverheid.nl"
        SERVICE.ensure_approval_requests(project)
        assert project["services"][0]["config"]["from-address-approval"]["status"] == "requested"

        project["services"][0]["config"]["from-address"] = "a@rijksoverheid.nl"
        SERVICE.ensure_approval_requests(project)

        blok = project["services"][0]["config"]["from-address-approval"]
        assert blok["status"] == "approved"
        assert approved_from_address(project) == "a@rijksoverheid.nl"


class TestDePoortOpHetAfzenderadres:
    """``approved_from_address`` is DE POORT: alleen een adres dat voor PRECIES DEZE
    WAARDE is goedgekeurd komt terug, in elk ander geval leeg."""

    def _project_met_blok(self, adres: str, status: str | None, blok_adres: str | None = None) -> dict:
        project = _project(config={"from-address": adres} if adres else {})
        if status is not None:
            project["services"][0]["config"]["from-address-approval"] = {
                "value": blok_adres if blok_adres is not None else adres,
                "status": status,
                "history": [],
            }
        return project

    def test_zonder_adres_is_er_niets_goedgekeurd(self) -> None:
        from opi.services.catalog.send_email import approved_from_address

        assert approved_from_address(self._project_met_blok("", None)) == ""

    def test_aangevraagd_maar_nog_niet_goedgekeurd_geeft_niets(self) -> None:
        from opi.services.catalog.send_email import approved_from_address

        project = self._project_met_blok("noreply@rijksoverheid.nl", "requested")
        assert approved_from_address(project) == ""

    def test_afgewezen_geeft_niets(self) -> None:
        from opi.services.catalog.send_email import approved_from_address

        project = self._project_met_blok("noreply@rijksoverheid.nl", "denied")
        assert approved_from_address(project) == ""

    def test_goedgekeurd_geeft_het_adres(self) -> None:
        from opi.services.catalog.send_email import approved_from_address

        project = self._project_met_blok("noreply@rijksoverheid.nl", "approved")
        assert approved_from_address(project) == "noreply@rijksoverheid.nl"

    def test_goedgekeurd_maar_daarna_gewijzigd_geeft_niets(self) -> None:
        """De tegenproef voor "de goedkeuring hangt aan de waarde": het goedgekeurde
        blok noemt nog het OUDE adres, het veld noemt het NIEUWE."""
        from opi.services.catalog.send_email import approved_from_address

        project = self._project_met_blok("nieuw@rijksoverheid.nl", "approved", blok_adres="oud@rijksoverheid.nl")
        assert approved_from_address(project) == ""


class TestDeWachtstandVoorEenEigenAdres:
    """Dezelfde fout als bij de dienstgoedkeuring mag hier niet terugkomen: een project
    dat wacht moet weten onder welk adres zijn post ONDERTUSSEN vertrekt."""

    def _notices(self, project: dict) -> list[dict]:
        from opi.services.approvals import collect_deployment_approval_notices

        return [
            notice
            for notice in collect_deployment_approval_notices(project, project["deployments"][0])
            if notice["type"] == "from-address-approval"
        ]

    def test_zonder_adres_meldt_niets(self) -> None:
        assert self._notices(_project(config={})) == []

    @pytest.mark.parametrize(
        ("status", "kern"),
        [("requested", "wacht op goedkeuring"), ("denied", "afgewezen")],
    )
    def test_elke_ongoedgekeurde_stand_meldt_zichzelf(self, status: str, kern: str) -> None:
        project = _project(config={"from-address": "noreply@rijksoverheid.nl"})
        project["services"][0]["config"]["from-address-approval"] = {
            "value": "noreply@rijksoverheid.nl",
            "status": status,
            "history": [],
        }
        notices = self._notices(project)
        assert len(notices) == 1
        assert kern in notices[0]["text"]

    def test_de_melding_noemt_het_afgeleide_adres(self) -> None:
        """Zonder dat weet de aanvrager wel dat hij wacht, maar niet onder welk adres
        zijn post nu vertrekt."""
        project = _project(config={"from-address": "noreply@rijksoverheid.nl"})
        project["services"][0]["config"]["from-address-approval"] = {
            "value": "noreply@rijksoverheid.nl",
            "status": "requested",
            "history": [],
        }
        notices = self._notices(project)
        assert "noreply-rijksapp+myproject@rijksoverheid.nl" in notices[0]["text"]

    def test_goedgekeurd_meldt_niets(self) -> None:
        project = _project(config={"from-address": "noreply@rijksoverheid.nl"})
        project["services"][0]["config"]["from-address-approval"] = {
            "value": "noreply@rijksoverheid.nl",
            "status": "approved",
            "history": [],
        }
        assert self._notices(project) == []

    def test_een_adres_zonder_blok_meldt_nog_niet_aangevraagd(self) -> None:
        """Een overgangstoestand: het veld staat al, het blok is er nog niet (bijvoorbeeld
        voor ``_ensure_from_address_request`` is geweest). Geen crash, en geen status die
        wacht/afgewezen/goedgekeurd beweert terwijl er niets is vastgelegd."""
        project = _project(config={"from-address": "noreply@rijksoverheid.nl"})
        notices = self._notices(project)
        assert len(notices) == 1
        assert "nog niet aangevraagd" in notices[0]["text"]
        assert notices[0]["status"] == "none"

    def test_een_onbekende_clusternaam_haalt_de_pagina_niet_omver(self) -> None:
        """Een clusternaam die het platform niet kent komt door de schemacontrole
        (``project_validation`` slaat zo'n deployment over), en deze mededeling wordt
        ongevangen gerenderd op de projectpagina (``opi/web/router.py``) en in het
        API-antwoord. Het afgeleide adres valt dan weg, de stand blijft staan."""
        project = _project(config={"from-address": "noreply@rijksoverheid.nl"})
        project["services"][0]["config"]["from-address-approval"] = {
            "value": "noreply@rijksoverheid.nl",
            "status": "requested",
            "history": [],
        }
        project["deployments"][0]["cluster"] = "een-cluster-dat-niet-bestaat"

        notices = self._notices(project)

        assert len(notices) == 1
        assert "wacht op goedkeuring" in notices[0]["text"]
        assert "@" not in notices[0]["text"].split("goedkeuring")[1]


class TestEenAfwijzingNeemtDeStaandeGoedkeuringTerug:
    """RC-400 rework: ``approved-value`` was een recht dat geen oordeel afnam. Een afwijzing
    van precies de staande goedkeuring haalt de sleutel nu weg, zodat een goedgekeurd getal
    in te trekken is en het project een afgewezen waarde niet zelf weer goedkeurt."""

    def _blok(self, project: dict, key: str) -> dict | None:
        return project["services"][0]["config"].get(key)

    def _oordeel(self, project: dict, status: str) -> None:
        from opi.services.approvals import apply_approval_verdicts, collect_approval_items

        items = collect_approval_items(project)
        for item in items:
            if item["type"] in ("messages-per-day-approval", "from-address-approval"):
                item["status"] = status
        apply_approval_verdicts(project, items, admin_email="beheerder@example.nl")

    def test_een_afgewezen_dagbudget_geldt_niet_meer_op_de_relay(self) -> None:
        from opi.services.catalog.send_email import effective_messages_per_day

        project = _project(config={"messages-per-day": 20000})
        SERVICE.ensure_approval_requests(project)
        self._oordeel(project, "approved")
        assert effective_messages_per_day(project) == 20000

        self._oordeel(project, "denied")

        assert self._blok(project, "messages-per-day-approval").get("approved-value") is None
        assert effective_messages_per_day(project) == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

    def test_terugkomen_op_een_afgewezen_waarde_is_een_nieuwe_aanvraag(self) -> None:
        """Het project wijzigt het veld en zet het terug op de afgewezen waarde. Zonder de
        intrekking zou dat het blok zelf op ``approved`` zetten, met een gewone opslagactie
        en zonder platformrol."""
        from opi.services.catalog.send_email import approved_from_address, effective_messages_per_day

        config = {"messages-per-day": 20000, "from-address": "noreply@rijksoverheid.nl"}
        project = _project(config=config)
        SERVICE.ensure_approval_requests(project)
        self._oordeel(project, "approved")
        self._oordeel(project, "denied")

        leefbaar = project["services"][0]["config"]
        leefbaar["messages-per-day"] = 500
        leefbaar["from-address"] = "anders@rijksoverheid.nl"
        SERVICE.ensure_approval_requests(project)
        leefbaar["messages-per-day"] = 20000
        leefbaar["from-address"] = "noreply@rijksoverheid.nl"
        SERVICE.ensure_approval_requests(project)

        assert self._blok(project, "messages-per-day-approval")["status"] == "requested"
        assert self._blok(project, "from-address-approval")["status"] == "requested"
        assert effective_messages_per_day(project) == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY
        assert approved_from_address(project) == ""

    def test_een_afwijzing_van_een_hoger_getal_raakt_de_staande_goedkeuring_niet(self) -> None:
        """Tegenproef: "een openstaande aanvraag verlaagt nooit wat er al gold" blijft staan,
        ook als die aanvraag wordt afgewezen."""
        from opi.services.catalog.send_email import effective_messages_per_day

        project = _project(config={"messages-per-day": 20000})
        SERVICE.ensure_approval_requests(project)
        self._oordeel(project, "approved")

        project["services"][0]["config"]["messages-per-day"] = 50000
        SERVICE.ensure_approval_requests(project)
        self._oordeel(project, "denied")

        assert self._blok(project, "messages-per-day-approval").get("approved-value") == "20000"
        assert effective_messages_per_day(project) == 20000

    def test_een_niet_decimaal_goedgekeurd_getal_valt_terug_op_de_standaard(self) -> None:
        """``isdigit`` is ook waar voor cijfers buiten ASCII, waar ``int()`` omvalt."""
        from opi.services.catalog.send_email import effective_messages_per_day

        project = _project(config={"messages-per-day": 20000})
        SERVICE.ensure_approval_requests(project)
        project["services"][0]["config"]["messages-per-day-approval"]["approved-value"] = "\N{SUPERSCRIPT TWO}"

        assert effective_messages_per_day(project) == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY


class TestDeBudgetpoortOpDeWaarde:
    """RC-400: ``effective_messages_per_day`` is DE POORT. Een getal boven de standaard
    telt alleen voor PRECIES DIE WAARDE, en een openstaande nieuwe aanvraag mag nooit minder
    geven dan het project al mocht - dat is het verschil met de afzenderpoort hierboven,
    die geen "laatst goedgekeurd" kent."""

    def _project_met_budget(
        self,
        getal: int | None,
        status: str | None = None,
        blok_waarde: int | None = None,
        laatst_goedgekeurd: int | None = None,
    ) -> dict:
        config: dict[str, Any] = {} if getal is None else {"messages-per-day": getal}
        project = _project(config=config)
        if status is not None:
            blok: dict[str, Any] = {
                "value": str(blok_waarde if blok_waarde is not None else getal),
                "status": status,
                "history": [],
            }
            if laatst_goedgekeurd is not None:
                blok["approved-value"] = str(laatst_goedgekeurd)
            project["services"][0]["config"]["messages-per-day-approval"] = blok
        return project

    def test_zonder_getal_is_de_standaard(self) -> None:
        from opi.services.catalog.send_email import effective_messages_per_day

        project = self._project_met_budget(None)
        assert effective_messages_per_day(project) == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

    def test_een_laag_getal_geldt_direct_zonder_goedkeuring(self) -> None:
        from opi.services.catalog.send_email import effective_messages_per_day

        project = self._project_met_budget(settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY - 1)
        assert effective_messages_per_day(project) == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY - 1

    def test_de_standaard_zelf_telt_ook_als_gekozen_en_niet_als_aanvraag(self) -> None:
        from opi.services.catalog.send_email import effective_messages_per_day

        project = self._project_met_budget(settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY)
        assert effective_messages_per_day(project) == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

    def test_een_hoog_getal_zonder_blok_geeft_de_standaard(self) -> None:
        from opi.services.catalog.send_email import effective_messages_per_day

        project = self._project_met_budget(5000)
        assert effective_messages_per_day(project) == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

    def test_aangevraagd_maar_nog_niet_goedgekeurd_geeft_de_standaard(self) -> None:
        from opi.services.catalog.send_email import effective_messages_per_day

        project = self._project_met_budget(5000, "requested")
        assert effective_messages_per_day(project) == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

    def test_afgewezen_geeft_de_standaard(self) -> None:
        from opi.services.catalog.send_email import effective_messages_per_day

        project = self._project_met_budget(5000, "denied")
        assert effective_messages_per_day(project) == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

    def test_goedgekeurd_geeft_het_gevraagde_getal(self) -> None:
        from opi.services.catalog.send_email import effective_messages_per_day

        project = self._project_met_budget(5000, "approved")
        assert effective_messages_per_day(project) == 5000

    def test_goedgekeurd_maar_daarna_verhoogd_geeft_het_laatst_goedgekeurde_getal(self) -> None:
        """De kern van RC-400: het blok draagt de NIEUWE, nog niet beoordeelde waarde
        (20000, requested), maar zolang die niet is goedgekeurd blijft het eerder
        goedgekeurde getal (5000) gelden - een openstaande aanvraag verlaagt dus nooit wat
        er al gold."""
        from opi.services.catalog.send_email import effective_messages_per_day

        project = self._project_met_budget(20000, "requested", laatst_goedgekeurd=5000)
        assert effective_messages_per_day(project) == 5000

    def test_verlagen_binnen_het_goedgekeurde_getal_geldt_direct(self) -> None:
        """Het project gebruikt minder dan het mag: dat is zijn eigen keuze, dus het lagere
        getal gaat direct naar de relay in plaats van de goedgekeurde 20000."""
        from opi.services.catalog.send_email import effective_messages_per_day

        project = self._project_met_budget(5000, "approved", blok_waarde=20000, laatst_goedgekeurd=20000)
        assert effective_messages_per_day(project) == 5000

    def test_zonder_eerdere_goedkeuring_valt_een_nieuwe_aanvraag_terug_op_de_standaard(self) -> None:
        """Tegenproef voor de vorige: zonder een eerder goedgekeurd getal is er niets om op
        terug te vallen."""
        from opi.services.catalog.send_email import effective_messages_per_day

        project = self._project_met_budget(20000, "requested")
        assert effective_messages_per_day(project) == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY


class TestDePoortOpDeSchrijfwegNaarDeRelay:
    """Toets 4 uit het plan: alleen bij goedgekeurd ontvangt ``set_sender`` het adres,
    in elke andere stand gaat er een lege waarde heen (en die wist een eerder adres)."""

    def _manager(self) -> MailManager:
        from opi.handlers.project_file_handler import ProjectFileHandler

        async def get_name() -> str:
            return "myproject"

        async def save_and_commit_project(project_data, message, enforce_validation=True) -> None:
            pass

        return MailManager(
            project_manager=SimpleNamespace(  # type: ignore[arg-type]
                _project_file_handler=ProjectFileHandler(),
                get_name=get_name,
                save_and_commit_project=save_and_commit_project,
                _add_secret_to_create=lambda *a, **k: None,
            )
        )

    def _connector(self) -> MailConnector:
        connector = MailConnector("http://relay", "admin", "geheim")
        connector.get_principal = AsyncMock(return_value=None)  # type: ignore[method-assign]
        connector.create_principal = AsyncMock()  # type: ignore[method-assign]
        connector.update_principal = AsyncMock()  # type: ignore[method-assign]
        connector.set_sender = AsyncMock(return_value=True)  # type: ignore[method-assign]
        connector.set_rate_limit = AsyncMock(return_value=True)  # type: ignore[method-assign]
        return connector

    async def _verwerk(self, project: dict, monkeypatch) -> MailConnector:
        connector = self._connector()
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=connector))
        await self._manager().create_resources_for_deployment(project, project["deployments"][0])
        return connector

    def _project_met_adres(self, status: str | None, blok_adres: str | None = None) -> dict:
        from opi.utils.sops import generate_sops_key_pair

        adres = "noreply@rijksoverheid.nl"
        project = _project(
            component_services=[ServiceType.SEND_EMAIL.value],
            config={"from-address": adres} if status else {},
        )
        if status is not None:
            project["services"][0]["config"]["from-address-approval"] = {
                "value": blok_adres if blok_adres is not None else adres,
                "status": status,
                "history": [],
            }
        _, public_key = generate_sops_key_pair()
        project["config"] = {"age-public-key": public_key}
        return project

    @pytest.mark.asyncio
    async def test_geen_adres_gaat_er_niets_heen(self, monkeypatch) -> None:
        connector = await self._verwerk(self._project_met_adres(None), monkeypatch)
        _, _, sender_address = connector.set_sender.call_args.args
        assert sender_address == ""

    @pytest.mark.asyncio
    async def test_aangevraagd_gaat_er_niets_heen(self, monkeypatch) -> None:
        connector = await self._verwerk(self._project_met_adres("requested"), monkeypatch)
        _, _, sender_address = connector.set_sender.call_args.args
        assert sender_address == ""

    @pytest.mark.asyncio
    async def test_goedgekeurd_gaat_het_adres_mee(self, monkeypatch) -> None:
        connector = await self._verwerk(self._project_met_adres("approved"), monkeypatch)
        _, _, sender_address = connector.set_sender.call_args.args
        assert sender_address == "noreply@rijksoverheid.nl"

    @pytest.mark.asyncio
    async def test_goedgekeurd_maar_gewijzigd_gaat_er_niets_heen(self, monkeypatch) -> None:
        """De tegenproef: zet de waardebinding uit (in dit geval door het blok met een ander
        adres te laten verwijzen) en de toets hierboven wordt rood -- precies waar de
        plan-eis "tegenproef" om vraagt."""
        connector = await self._verwerk(
            self._project_met_adres("approved", blok_adres="ander@rijksoverheid.nl"), monkeypatch
        )
        _, _, sender_address = connector.set_sender.call_args.args
        assert sender_address == ""


class TestDeBudgetpoortOpDeSchrijfwegNaarDeRelay:
    """Toets 4 uit het plan, voor het budget: een unittest op
    ``create_resources_for_deployment`` met een nep-connector, voor geen getal, laag getal,
    aangevraagd, goedgekeurd en goedgekeurd-maar-verhoogd."""

    def _manager(self) -> MailManager:
        from opi.handlers.project_file_handler import ProjectFileHandler

        async def get_name() -> str:
            return "myproject"

        async def save_and_commit_project(project_data, message, enforce_validation=True) -> None:
            pass

        return MailManager(
            project_manager=SimpleNamespace(  # type: ignore[arg-type]
                _project_file_handler=ProjectFileHandler(),
                get_name=get_name,
                save_and_commit_project=save_and_commit_project,
                _add_secret_to_create=lambda *a, **k: None,
            )
        )

    def _connector(self) -> MailConnector:
        connector = MailConnector("http://relay", "admin", "geheim")
        connector.get_principal = AsyncMock(return_value=None)  # type: ignore[method-assign]
        connector.create_principal = AsyncMock()  # type: ignore[method-assign]
        connector.update_principal = AsyncMock()  # type: ignore[method-assign]
        connector.set_sender = AsyncMock(return_value=True)  # type: ignore[method-assign]
        connector.set_rate_limit = AsyncMock(return_value=True)  # type: ignore[method-assign]
        return connector

    async def _verwerk(self, project: dict, monkeypatch) -> MailConnector:
        connector = self._connector()
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=connector))
        await self._manager().create_resources_for_deployment(project, project["deployments"][0])
        return connector

    def _project_met_budget(
        self,
        getal: int | None,
        status: str | None = None,
        blok_waarde: int | None = None,
        laatst_goedgekeurd: int | None = None,
    ) -> dict:
        from opi.utils.sops import generate_sops_key_pair

        config: dict[str, Any] = {} if getal is None else {"messages-per-day": getal}
        project = _project(component_services=[ServiceType.SEND_EMAIL.value], config=config)
        if status is not None:
            blok: dict[str, Any] = {
                "value": str(blok_waarde if blok_waarde is not None else getal),
                "status": status,
                "history": [],
            }
            if laatst_goedgekeurd is not None:
                blok["approved-value"] = str(laatst_goedgekeurd)
            project["services"][0]["config"]["messages-per-day-approval"] = blok
        _, public_key = generate_sops_key_pair()
        project["config"] = {"age-public-key": public_key}
        return project

    @pytest.mark.asyncio
    async def test_geen_getal_gaat_de_standaard_naar_de_relay(self, monkeypatch) -> None:
        connector = await self._verwerk(self._project_met_budget(None), monkeypatch)
        _, limiet = connector.set_rate_limit.call_args.args
        assert limiet == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

    @pytest.mark.asyncio
    async def test_een_laag_getal_gaat_rechtstreeks_mee(self, monkeypatch) -> None:
        connector = await self._verwerk(self._project_met_budget(500), monkeypatch)
        _, limiet = connector.set_rate_limit.call_args.args
        assert limiet == 500

    @pytest.mark.asyncio
    async def test_aangevraagd_maar_nog_niet_goedgekeurd_gaat_de_standaard_mee(self, monkeypatch) -> None:
        connector = await self._verwerk(self._project_met_budget(5000, "requested"), monkeypatch)
        _, limiet = connector.set_rate_limit.call_args.args
        assert limiet == settings.MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY

    @pytest.mark.asyncio
    async def test_goedgekeurd_gaat_het_gevraagde_getal_mee(self, monkeypatch) -> None:
        connector = await self._verwerk(self._project_met_budget(5000, "approved"), monkeypatch)
        _, limiet = connector.set_rate_limit.call_args.args
        assert limiet == 5000

    @pytest.mark.asyncio
    async def test_goedgekeurd_maar_daarna_verhoogd_houdt_het_laatst_goedgekeurde_getal(self, monkeypatch) -> None:
        """De tegenproef: laat de nieuwe, hogere aanvraag (20000) nog niet goedgekeurd zijn
        en de toets hierboven wordt rood, want dan gaat er geen 5000 maar de standaard mee."""
        connector = await self._verwerk(
            self._project_met_budget(20000, "requested", laatst_goedgekeurd=5000), monkeypatch
        )
        _, limiet = connector.set_rate_limit.call_args.args
        assert limiet == 5000


class TestDeAfzenderverwerkingIsHerhaalbaar:
    """Toets 5 uit het plan: een tweede verwerking met een goedgekeurd adres schrijft
    niets opnieuw naar de relay en maakt geen commit."""

    @pytest.mark.asyncio
    async def test_tweede_verwerking_slaat_niets_opnieuw_op(self, monkeypatch) -> None:
        from opi.core.config import settings
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.utils.age import encrypt_age_content
        from opi.utils.sops import generate_sops_key_pair

        project = _project(
            component_services=[ServiceType.SEND_EMAIL.value],
            config={"from-address": "noreply@rijksoverheid.nl"},
        )
        project["services"][0]["config"]["from-address-approval"] = {
            "value": "noreply@rijksoverheid.nl",
            "status": "approved",
            "history": [],
        }
        platform_private, platform_public = generate_sops_key_pair()
        project_private, project_public = generate_sops_key_pair()
        monkeypatch.setattr(settings, "SOPS_AGE_PRIVATE_KEY", platform_private)
        project["config"] = {
            "age-public-key": project_public,
            "age-private-key": await encrypt_age_content(project_private, platform_public),
        }

        async def get_name() -> str:
            return "myproject"

        opgeslagen: list[str] = []

        async def save_and_commit_project(project_data, message, enforce_validation=True) -> None:
            opgeslagen.append(message)

        manager = MailManager(
            project_manager=SimpleNamespace(  # type: ignore[arg-type]
                _project_file_handler=ProjectFileHandler(),
                get_name=get_name,
                save_and_commit_project=save_and_commit_project,
                _add_secret_to_create=lambda *a, **k: None,
            )
        )

        connector = MailConnector("http://relay", "admin", "geheim")
        connector.get_principal = AsyncMock(return_value=None)  # type: ignore[method-assign]
        connector.create_principal = AsyncMock()  # type: ignore[method-assign]
        connector.update_principal = AsyncMock()  # type: ignore[method-assign]
        connector.set_sender = AsyncMock(return_value=True)  # type: ignore[method-assign]
        connector.set_rate_limit = AsyncMock(return_value=True)  # type: ignore[method-assign]
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=connector))

        await manager.create_resources_for_deployment(project, project["deployments"][0])
        assert len(opgeslagen) == 1, "de eerste run schrijft het account weg"

        # Het bestaande account dat de eerste run wegschreef, zit nu in het projectbestand
        # (encrypted wachtwoord, from-address, bounce-address): de tweede run leest het
        # terug en ziet geen verschil, dus geen tweede schrijfactie.
        connector.get_principal = AsyncMock(return_value={"name": "project-myproject"})  # type: ignore[method-assign]

        await manager.create_resources_for_deployment(project, project["deployments"][0])
        assert len(opgeslagen) == 1, "een ongewijzigde tweede run mag geen tweede commit maken"


class TestDeRelayAntwoordtGeen404:
    """Gemeten tegen Stalwart v0.11.8, en het is precies de valkuil van deze connector.

    Een onbekend account is bij de management-API GEEN 404: het antwoord is ``200`` met
    ``{"error": "notFound", "item": "<naam>"}`` in het lichaam. Wie alleen naar de
    statuscode kijkt, leest dat als "het account bestaat" en gaat bijwerken in plaats van
    aanmaken - dan wordt het account nooit gemaakt en authenticeert de applicatie nergens.
    Deze suite draait daarom tegen een echte HTTP-server die die antwoorden nabootst; een
    mock op ``_request`` zou juist de laag overslaan waar de fout zat.
    """

    def _app(self, aangemaakt: list[dict]) -> web.Application:
        async def get_principal(request: web.Request) -> web.Response:
            naam = request.match_info["naam"]
            if naam == "bestaat":
                return web.json_response({"data": {"id": 1, "name": naam, "type": "individual"}})
            return web.json_response({"error": "notFound", "item": naam})

        async def post_principal(request: web.Request) -> web.Response:
            aangemaakt.append(await request.json())
            return web.json_response({"data": 42})

        async def delete_principal(request: web.Request) -> web.Response:
            naam = request.match_info["naam"]
            if naam == "bestaat":
                return web.json_response({"data": None})
            return web.json_response({"error": "notFound", "item": naam})

        app = web.Application()
        app.router.add_get("/api/principal/{naam}", get_principal)
        app.router.add_post("/api/principal", post_principal)
        app.router.add_delete("/api/principal/{naam}", delete_principal)
        return app

    async def _connector(self, aangemaakt: list[dict]):
        server = TestServer(self._app(aangemaakt))
        await server.start_server()
        return MailConnector(str(server.make_url("")).rstrip("/"), "admin", "geheim", verify_tls=False), server

    @pytest.mark.asyncio
    async def test_een_onbekend_account_leest_als_afwezig(self) -> None:
        connector, server = await self._connector([])
        try:
            assert await connector.get_principal("kent-hij-niet") is None
            assert await connector.get_principal("bestaat") is not None
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_een_al_verwijderd_account_meldt_geen_succes(self) -> None:
        connector, server = await self._connector([])
        try:
            assert await connector.delete_principal("kent-hij-niet") is False
            assert await connector.delete_principal("bestaat") is True
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_het_aangemaakte_account_kan_ook_echt_versturen(self) -> None:
        """Drie dingen die de relay eist, alle drie nagespeeld:

        zonder een ROL wordt het account na een geslaagde authenticatie alsnog geweigerd
        ("550 5.7.1 Your account is not authorized to use this service"); zonder het
        BOUNCE-adres op het account faalt elke MAIL FROM, want de envelope wordt daarheen
        herschreven voordat de afzenderpolicy hem toetst; en een ``limits``-veld bestaat
        niet op een principal, dus de hele aanroep zou stranden op "JSON deserialization
        failed".
        """
        aangemaakt: list[dict] = []
        connector, server = await self._connector(aangemaakt)
        try:
            await connector.create_principal(name="myproject", password="geheim")
        finally:
            await server.close()

        payload = aangemaakt[0]
        assert payload["roles"] == ["user"]
        # GEEN adressen op het account: die zouden rijksoverheid.nl als lokaal domein
        # vereisen, en dan bezorgt de relay mail AAN dat domein bij zichzelf.
        assert "emails" not in payload
        assert "limits" not in payload


class TestEenPodwisselKostGeenUitrol:
    """RC-261 punt 4: een korte onderbreking van de relay mag een aanroep niet meteen
    laten stranden, zolang de volgende poging het al weer doet."""

    @pytest.mark.asyncio
    async def test_een_eenmalige_transportfout_wordt_overleefd(self, monkeypatch) -> None:
        connector = MailConnector("http://rig-mail-relay:8080", "admin", "geheim")
        fout = aiohttp.ClientConnectorError(
            connection_key=SimpleNamespace(ssl=None, host="rig-mail-relay", port=8080, is_ssl=False),  # type: ignore[arg-type]
            os_error=OSError("Connection refused"),
        )
        pogingen = AsyncMock(side_effect=[fout, {"data": {"id": 1, "name": "project-demo"}}])
        monkeypatch.setattr(MailConnector, "_eenmalig_verzoek", pogingen)
        monkeypatch.setattr("opi.utils.connect_retry.asyncio.sleep", AsyncMock())

        resultaat = await connector.get_principal("project-demo")

        assert resultaat == {"id": 1, "name": "project-demo"}
        assert pogingen.await_count == 2, "de tweede poging had de eerste transportfout moeten overleven"


class TestDeWeergavenaamStaatOpDeRelay:
    """De relay kan het ADRES zelf afleiden, de weergavenaam niet.

    Wat er in Stalwart v0.11.8 allemaal NIET kan, want dat is waarom dit de vorm heeft die
    het heeft (alles gemeten op 20 augustus 2026 tegen de sandbox): geen expressiefunctie
    leest een principal uit, dus het description-veld dat OPI al zet is onbereikbaar;
    ``config_get`` neemt alleen een LETTERLIJKE sleutel en wordt bij het bouwen van de
    configuratie opgelost, niet per bericht; een opzoektabel in het geheugen wordt maar EEN
    KEER gebouwd en een reload ververst hem niet, dus het eerste project kreeg zijn waarde
    en elk volgend project las leeg tot een HERSTART; en de opzoekopslag die wel live is,
    heeft geen schrijfweg in de management-API.

    Wat wel kan: een sieve-script dat via de settings-API wordt geschreven, wordt bij elke
    herbouw opnieuw gecompileerd. Daarom is de tabel een gegenereerd script.

    Deze suite draait tegen een echte HTTP-server en niet tegen een mock op ``_request``,
    om dezelfde reden als de suite hierboven: de vorm van het lichaam IS de afspraak met de
    relay, en die vorm is precies wat een mock zou overslaan.
    """

    def _app(self, geschreven: list[dict], herladen: list[int], opgeslagen: dict[str, str]) -> web.Application:
        async def list_settings(request: web.Request) -> web.Response:
            prefix = request.query.get("prefix", "")
            items = {k[len(prefix) :]: v for k, v in opgeslagen.items() if k.startswith(prefix)}
            return web.json_response({"data": {"total": len(items), "items": items}})

        async def post_settings(request: web.Request) -> web.Response:
            wijzigingen = await request.json()
            geschreven.extend(wijzigingen)
            for wijziging in wijzigingen:
                if wijziging["type"] == "insert":
                    opgeslagen.update(dict(wijziging["values"]))
                else:
                    for sleutel in wijziging["keys"]:
                        opgeslagen.pop(sleutel, None)
            return web.json_response({"data": None})

        async def reload(request: web.Request) -> web.Response:
            herladen.append(1)
            return web.json_response({"data": {"warnings": {}, "errors": {}}})

        app = web.Application()
        app.router.add_get("/api/settings/list", list_settings)
        app.router.add_post("/api/settings", post_settings)
        app.router.add_get("/api/reload", reload)
        return app

    async def _connector(self, geschreven: list[dict], herladen: list[int], opgeslagen: dict[str, str]):
        server = TestServer(self._app(geschreven, herladen, opgeslagen))
        await server.start_server()
        return MailConnector(str(server.make_url("")).rstrip("/"), "admin", "geheim", verify_tls=False), server

    @pytest.mark.asyncio
    async def test_de_naam_en_de_tabel_gaan_samen_en_de_relay_herlaadt(self) -> None:
        """De sleutel EN het gegenereerde script in een verzoek, met een reload erachter.

        Samen, want de sleutel is de gegevensbron en het script is wat de relay werkelijk
        leest; twee verzoeken kunnen de relay even iets anders laten lezen dan OPI denkt te
        hebben geschreven. En herladen, want zonder herbouw wordt het script niet opnieuw
        gecompileerd en geldt de oude tabel nog.
        """
        geschreven: list[dict] = []
        herladen: list[int] = []
        connector, server = await self._connector(geschreven, herladen, {})
        try:
            gewijzigd = await connector.set_sender_name("project-ai1-uit", "Vrije Naam")
        finally:
            await server.close()

        assert gewijzigd is True
        assert len(geschreven) == 3, "de twee sleutels en de tabel horen in een enkel verzoek te gaan"
        assert geschreven[0]["values"] == [[f"{MAIL_SENDER_NAME_PREFIX}.project-ai1-uit", "Vrije Naam"]]
        # Een project kiest zijn adres NIET, dus die sleutel wordt verwijderd en niet gezet:
        # de relay leidt het adres af uit de accountnaam.
        assert geschreven[1]["type"] == "delete"
        assert geschreven[1]["keys"] == [f"{MAIL_SENDER_ADDRESS_PREFIX}.project-ai1-uit"]
        assert geschreven[2]["values"][0][0] == MAIL_SENDER_TABLE_KEY
        assert 'set "naam" "Vrije Naam"' in geschreven[2]["values"][0][1]
        assert herladen == [1]

    @pytest.mark.asyncio
    async def test_de_tabel_houdt_de_andere_projecten(self) -> None:
        """Het gegenereerde script wordt uit ALLE sleutels opgebouwd.

        Dit is waarom de sleutels de gegevensbron zijn en het script een afgeleide: wie de
        tabel als bron zou nemen, moet gegenereerde code parsen om er een project bij te
        zetten - en is bij de eerste vergissing de andere projecten kwijt.
        """
        geschreven: list[dict] = []
        opgeslagen = {f"{MAIL_SENDER_NAME_PREFIX}.project-eerste": "Eerste"}
        connector, server = await self._connector(geschreven, [], opgeslagen)
        try:
            await connector.set_sender_name("project-tweede", "Tweede")
        finally:
            await server.close()

        tabel = geschreven[2]["values"][0][1]
        assert 'set "naam" "Eerste"' in tabel
        assert 'set "naam" "Tweede"' in tabel

    @pytest.mark.asyncio
    async def test_dezelfde_naam_nog_eens_schrijft_niets(self) -> None:
        """Herhaalbaarheid, en het is geen schoonheidsfoutje: elke schrijfactie sleept een
        herbouw van de hele relayconfiguratie mee, en een project wordt bij elke wijziging
        opnieuw verwerkt."""
        geschreven: list[dict] = []
        herladen: list[int] = []
        opgeslagen = {f"{MAIL_SENDER_NAME_PREFIX}.project-ai1-uit": "Vrije Naam"}
        connector, server = await self._connector(geschreven, herladen, opgeslagen)
        try:
            gewijzigd = await connector.set_sender_name("project-ai1-uit", "Vrije Naam")
        finally:
            await server.close()

        assert gewijzigd is False
        assert geschreven == []
        assert herladen == []

    @pytest.mark.asyncio
    async def test_zonder_naam_verdwijnt_de_sleutel_en_de_regel(self) -> None:
        """Geen naam is een geldige uitkomst, geen terugval: dan vertrekt de post met een
        kaal PROJECTadres. De sleutel gaat weg in plaats van leeg te worden, zodat een
        beheerder die de instellingen leest niets hoeft te duiden."""
        geschreven: list[dict] = []
        opgeslagen = {f"{MAIL_SENDER_NAME_PREFIX}.project-zonder": "Oude Naam"}
        connector, server = await self._connector(geschreven, [], opgeslagen)
        try:
            await connector.delete_sender_name("project-zonder")
        finally:
            await server.close()

        assert geschreven[0]["type"] == "delete"
        assert geschreven[0]["keys"] == [f"{MAIL_SENDER_NAME_PREFIX}.project-zonder"]
        assert geschreven[1]["type"] == "delete"
        assert geschreven[1]["keys"] == [f"{MAIL_SENDER_ADDRESS_PREFIX}.project-zonder"]
        assert "project-zonder" not in geschreven[2]["values"][0][1]


class TestSetRateLimitOpDeRelay:
    """RC-400: ``set_rate_limit`` en ``delete_rate_limit`` schrijven de limiter van een
    account via de settings-API. Zelfde opzet als ``TestDeWeergavenaamStaatOpDeRelay``: een
    echte HTTP-server, want de vorm van het verzoek IS de afspraak met de relay."""

    def _app(self, geschreven: list[dict], herladen: list[int], opgeslagen: dict[str, str]) -> web.Application:
        async def list_settings(request: web.Request) -> web.Response:
            prefix = request.query.get("prefix", "")
            items = {k[len(prefix) :]: v for k, v in opgeslagen.items() if k.startswith(prefix)}
            return web.json_response({"data": {"total": len(items), "items": items}})

        async def post_settings(request: web.Request) -> web.Response:
            wijzigingen = await request.json()
            geschreven.extend(wijzigingen)
            for wijziging in wijzigingen:
                if wijziging["type"] == "insert":
                    opgeslagen.update(dict(wijziging["values"]))
                else:
                    for sleutel in wijziging["keys"]:
                        opgeslagen.pop(sleutel, None)
            return web.json_response({"data": None})

        async def reload(request: web.Request) -> web.Response:
            herladen.append(1)
            return web.json_response({"data": {"warnings": {}, "errors": {}}})

        app = web.Application()
        app.router.add_get("/api/settings/list", list_settings)
        app.router.add_post("/api/settings", post_settings)
        app.router.add_get("/api/reload", reload)
        return app

    async def _connector(self, geschreven: list[dict], herladen: list[int], opgeslagen: dict[str, str]):
        server = TestServer(self._app(geschreven, herladen, opgeslagen))
        await server.start_server()
        return MailConnector(str(server.make_url("")).rstrip("/"), "admin", "geheim", verify_tls=False), server

    @pytest.mark.asyncio
    async def test_een_nieuwe_limiet_wordt_in_een_verzoek_geschreven_en_de_relay_herlaadt(self) -> None:
        geschreven: list[dict] = []
        herladen: list[int] = []
        connector, server = await self._connector(geschreven, herladen, {})
        try:
            gewijzigd = await connector.set_rate_limit("project-ai1-uit", 500)
        finally:
            await server.close()

        assert gewijzigd is True
        prefix = f"{MAIL_LIMITER_PREFIX}-project-ai1-uit"
        assert len(geschreven) == 1, "de vier sleutels horen in een enkel verzoek te gaan"
        waarden = dict(geschreven[0]["values"])
        assert waarden[f"{prefix}.enable"] == "true"
        assert waarden[f"{prefix}.key"] == "authenticated_as"
        assert waarden[f"{prefix}.match"] == "authenticated_as == 'project-ai1-uit'"
        assert waarden[f"{prefix}.rate"] == "500/1d"
        assert herladen == [1]

    @pytest.mark.asyncio
    async def test_dezelfde_limiet_nog_eens_schrijft_niets(self) -> None:
        """Herhaalbaarheid (toets 5 uit het plan): elke schrijfactie sleept een herbouw van
        de hele relayconfiguratie mee, en een project wordt bij elke wijziging opnieuw
        verwerkt."""
        geschreven: list[dict] = []
        herladen: list[int] = []
        prefix = f"{MAIL_LIMITER_PREFIX}-project-ai1-uit"
        opgeslagen = {
            f"{prefix}.enable": "true",
            f"{prefix}.key": "authenticated_as",
            f"{prefix}.match": "authenticated_as == 'project-ai1-uit'",
            f"{prefix}.rate": "500/1d",
        }
        connector, server = await self._connector(geschreven, herladen, opgeslagen)
        try:
            gewijzigd = await connector.set_rate_limit("project-ai1-uit", 500)
        finally:
            await server.close()

        assert gewijzigd is False
        assert geschreven == []
        assert herladen == []

    @pytest.mark.asyncio
    async def test_een_halve_limiter_wordt_gerepareerd(self) -> None:
        """Een limiter met het juiste getal maar zonder ``match`` valt in de groep
        ``remote`` en telt per verbinding; die stand moet worden hersteld in plaats van
        voor bij-de-tijd doorgaan."""
        geschreven: list[dict] = []
        prefix = f"{MAIL_LIMITER_PREFIX}-project-ai1-uit"
        opgeslagen = {f"{prefix}.enable": "true", f"{prefix}.rate": "500/1d"}
        connector, server = await self._connector(geschreven, [], opgeslagen)
        try:
            gewijzigd = await connector.set_rate_limit("project-ai1-uit", 500)
        finally:
            await server.close()

        assert gewijzigd is True
        waarden = dict(geschreven[0]["values"])
        assert waarden[f"{prefix}.match"] == "authenticated_as == 'project-ai1-uit'"
        assert waarden[f"{prefix}.key"] == "authenticated_as"

    @pytest.mark.asyncio
    async def test_een_ander_getal_schrijft_wel(self) -> None:
        geschreven: list[dict] = []
        prefix = f"{MAIL_LIMITER_PREFIX}-project-ai1-uit"
        opgeslagen = {f"{prefix}.enable": "true", f"{prefix}.rate": "500/1d"}
        connector, server = await self._connector(geschreven, [], opgeslagen)
        try:
            gewijzigd = await connector.set_rate_limit("project-ai1-uit", 2000)
        finally:
            await server.close()

        assert gewijzigd is True
        waarden = dict(geschreven[0]["values"])
        assert waarden[f"{prefix}.rate"] == "2000/1d"

    @pytest.mark.asyncio
    async def test_opruimen_verwijdert_de_vier_sleutels_en_herlaadt(self) -> None:
        """Toets 6 uit het plan: een verwijderd project laat geen limitersleutels achter."""
        geschreven: list[dict] = []
        herladen: list[int] = []
        prefix = f"{MAIL_LIMITER_PREFIX}-project-weg"
        opgeslagen = {
            f"{prefix}.enable": "true",
            f"{prefix}.key": "authenticated_as",
            f"{prefix}.match": "authenticated_as == 'project-weg'",
            f"{prefix}.rate": "500/1d",
        }
        connector, server = await self._connector(geschreven, herladen, opgeslagen)
        try:
            await connector.delete_rate_limit("project-weg")
        finally:
            await server.close()

        assert geschreven[0]["type"] == "delete"
        assert sorted(geschreven[0]["keys"]) == sorted(
            [f"{prefix}.enable", f"{prefix}.key", f"{prefix}.match", f"{prefix}.rate"]
        )
        assert opgeslagen == {}
        assert herladen == [1]

    @pytest.mark.asyncio
    async def test_opruimen_zonder_bestaande_sleutels_doet_niets(self) -> None:
        """Replay-safe: een tweede intrekking of een project dat zijn limiter nooit kreeg
        mag geen verzoek en geen herlaadbeurt kosten."""
        geschreven: list[dict] = []
        herladen: list[int] = []
        connector, server = await self._connector(geschreven, herladen, {})
        try:
            await connector.delete_rate_limit("project-had-nooit-een-limiet")
        finally:
            await server.close()

        assert geschreven == []
        assert herladen == []

    @pytest.mark.asyncio
    async def test_set_rate_limit_weigert_een_accountnaam_met_een_quote(self) -> None:
        """Toets 7 uit het plan: de accountnaam komt tussen enkele quotes in de
        ``match``-expressie, dus een naam die er een draagt mag die expressie niet openen."""
        connector = MailConnector("http://relay", "admin", "geheim")
        with pytest.raises(MailSenderNameError):
            await connector.set_rate_limit("project-x' || '1'=='1", 500)

    @pytest.mark.asyncio
    async def test_delete_rate_limit_weigert_dezelfde_namen(self) -> None:
        connector = MailConnector("http://relay", "admin", "geheim")
        with pytest.raises(MailSenderNameError):
            await connector.delete_rate_limit("project met een spatie")


class TestDeGegenereerdeTabelIsGeenInvoerkanaal:
    """De tabel is CODE die uit projectdata wordt opgebouwd, en dat is de plek om streng te
    zijn.

    De validatie op het configmodel houdt dit al tegen op allebei de schrijfwegen (formulier
    en API) -- en dat is een afspraak die je moet TOETSEN, niet aannemen: zolang ``$`` alleen
    hier verboden was, kwam een naam met een dollarteken door het formulier en viel hij pas
    om in ``ensure_account``. ``TestDeWeergavenaamWordtGetoetst`` legt de twee lijsten nu
    naast elkaar, in beide richtingen.

    Deze laag houdt stand als een waarde de connector ooit langs een derde weg bereikt.
    Precies de reden waarom een validator op het VELD niet genoeg is: een veld kan van
    eigenaar wisselen, de connector blijft de laatste die de waarde in code zet.
    """

    def test_een_gewone_tabel_ziet_er_saai_uit(self) -> None:
        tabel = render_sender_table({"project-b": "Bee", "project-a": "Aa"})
        assert 'require ["variables"];' in tabel
        assert 'global "naam";' in tabel
        # Op alfabet, zodat dezelfde gegevens dezelfde tabel opleveren en een run die niets
        # verandert ook echt niets verandert.
        assert tabel.index("project-a") < tabel.index("project-b")

    def test_een_lege_naam_levert_geen_regel_op(self) -> None:
        assert "project-leeg" not in render_sender_table({"project-leeg": ""})

    @pytest.mark.parametrize(
        "naam",
        [
            'Zeg "hoi"',
            "pad\\weg",
            "Kwaad\r\nBcc: iedereen@example.org",
            "beveiliging@bank.nl",
            "Iemand <spoof@evil.example>",
            "${adres}",
            "a" * 65,
        ],
    )
    def test_een_naam_die_de_code_zou_openbreken_wordt_geweigerd(self, naam: str) -> None:
        """Let op ``${adres}``: sieve vult ``${...}`` in een string in, dus een naam met een
        dollarteken zou een VARIABELE lezen in plaats van tekst zijn."""
        with pytest.raises(MailSenderNameError):
            render_sender_table({"project-x": naam})

    @pytest.mark.parametrize(
        "account",
        [
            "project-x; set",
            'project-"x',
            "PROJECT-X",
            "project x",
            "",
            # Zelfde val als bij het adres: ``$`` matcht ook vlak voor een slot-newline, dus
            # dit kwam erlangs en zette een regeleinde midden in de gegenereerde tabel.
            "project-x\n",
        ],
    )
    def test_een_accountnaam_buiten_de_vorm_wordt_geweigerd(self, account: str) -> None:
        with pytest.raises(MailSenderNameError):
            render_sender_table({account: "Naam"})


class TestDeAfzenderWordtVastgelegdBijHetAccount:
    """``ensure_account`` is de ENE plek waar een account ontstaat, en dus ook de ene plek
    waar zijn weergavenaam wordt vastgelegd."""

    def _connector(self, existing: dict | None) -> MailConnector:
        connector = MailConnector("http://relay", "admin", "geheim")
        connector.get_principal = AsyncMock(return_value=existing)  # type: ignore[method-assign]
        connector.create_principal = AsyncMock()  # type: ignore[method-assign]
        connector.update_principal = AsyncMock()  # type: ignore[method-assign]
        connector.set_sender = AsyncMock(return_value=True)  # type: ignore[method-assign]
        connector.set_rate_limit = AsyncMock(return_value=True)  # type: ignore[method-assign]
        return connector

    async def _ensure(self, connector: MailConnector, from_name: str = "Vrije Naam") -> None:
        await MailManager.ensure_account(
            connector=connector,
            username="project-ai1-uit",
            password="geheim",
            from_address="noreply-rijksapp+ai1-uit@rijksoverheid.nl",
            bounce_address="noreply-rijksapp+ai1-uit@rijksoverheid.nl",
            from_name=from_name,
            messages_per_day=500,
        )

    @pytest.mark.asyncio
    async def test_een_nieuw_account_krijgt_meteen_zijn_weergavenaam(self) -> None:
        connector = self._connector(existing=None)
        await self._ensure(connector)
        connector.set_sender.assert_awaited_once_with("project-ai1-uit", "Vrije Naam", "")

    @pytest.mark.asyncio
    async def test_een_bestaand_account_ook(self) -> None:
        """Replay-veilig: een gewijzigde ``from-name`` moet op de volgende verwerking gelden,
        en dat is precies wat er tot RC-145 NIET gebeurde - het veld had geen enkele lezer.
        Of er dan echt iets naar de relay gaat, beslist de connector op een verschil."""
        connector = self._connector(existing={"name": "project-ai1-uit"})
        await self._ensure(connector, from_name="Nieuwe Naam")
        connector.set_sender.assert_awaited_once_with("project-ai1-uit", "Nieuwe Naam", "")

    @pytest.mark.asyncio
    async def test_zonder_naam_gaat_er_een_lege_naam_heen(self) -> None:
        """En dat is een OPDRACHT, geen overslaan: een project dat zijn naam weghaalt moet
        hem ook op de relay kwijtraken."""
        connector = self._connector(existing={"name": "project-ai1-uit"})
        await self._ensure(connector, from_name="")
        connector.set_sender.assert_awaited_once_with("project-ai1-uit", "", "")


class TestDeWeergavenaamWordtGetoetst:
    """``from-name`` gaat rechtstreeks een mailheader in, en had tot RC-145 geen enkele
    controle.

    De regel staat op het CONFIGMODEL en niet in het formulier, en dat is het punt van deze
    suite: het model is waar de API tegenaan schrijft en waar een opgeslagen projectbestand
    mee wordt getoetst, en het formulier wijst via ``ModelFieldValidator`` naar diezelfde
    constraints. Zou het formulier een eigen kopie hebben, dan is er een weg om de regel
    heen zodra een van de twee verandert.
    """

    #: De weergavenaam uit het projectbestand van ai1-uit, en een paar die het net zo goed
    #: moeten halen: een punt en een komma zijn in een naam heel gewoon, en de relay zet
    #: er aanhalingstekens omheen zodat ze de From: niet in tweeen knippen.
    GOED: ClassVar[list[str]] = [
        "Robbert Uittenbroek",
        "R. Uittenbroek",
        "Jan, Piet",
        "Algoritmeregister",
        "a" * 64,
        "",
    ]

    FOUT: ClassVar[dict[str, str]] = {
        "regeleinde": "Kwaad\r\nBcc: iedereen@example.org",
        "losse newline": "Kwaad\nBcc: iedereen@example.org",
        "stuurteken": "Kwaad\x00stil",
        "apenstaartje": "beveiliging@bank.nl",
        "punthaken": "Iemand <spoof@evil.example>",
        "aanhalingsteken": 'Zeg "hoi"',
        "backslash": "pad\\weg",
        "dollarteken": "Aanbod $5 korting",
        "te lang": "a" * 65,
    }

    @pytest.mark.parametrize("naam", GOED)
    def test_een_gewone_naam_mag(self, naam: str) -> None:
        assert SendEmailConfig(**{"from-name": naam}).from_name == naam

    @pytest.mark.parametrize(("geval", "naam"), list(FOUT.items()))
    def test_de_api_weigert(self, geval: str, naam: str) -> None:
        """Dit is de weg die het projectbestand schrijft: model en JSON-schema."""
        with pytest.raises(ValidationError):
            SendEmailConfig(**{"from-name": naam})

    @pytest.mark.parametrize(("geval", "naam"), list(FOUT.items()))
    def test_het_formulier_weigert_hetzelfde(self, geval: str, naam: str) -> None:
        """En dit is de weg die de wizard schrijft. Een waarde die het formulier doorlaat
        maar de API weigert, laat de gebruiker een opslagfout zien die hij niet kan duiden;
        andersom is er een weg om de regel heen."""
        from opi.services.catalog.send_email.editables import SEND_EMAIL_FROM_NAME_EDITABLE

        assert SEND_EMAIL_FROM_NAME_EDITABLE.validator.validate(naam), f"{geval} hoort geweigerd te worden"

    @pytest.mark.parametrize("naam", GOED)
    def test_het_formulier_laat_een_gewone_naam_door(self, naam: str) -> None:
        from opi.services.catalog.send_email.editables import SEND_EMAIL_FROM_NAME_EDITABLE

        assert SEND_EMAIL_FROM_NAME_EDITABLE.validator.validate(naam) == []

    @pytest.mark.parametrize(("geval", "naam"), list(FOUT.items()))
    def test_de_relay_weigert_precies_dezelfde_namen(self, geval: str, naam: str) -> None:
        """De laatste laag is de connector, die de naam in een gegenereerd sieve-script zet.

        Deze toets bestaat omdat de twee lijsten UIT ELKAAR kunnen lopen, en dat gebeurde:
        ``$`` stond wel in ``_NAAM_VERBODEN`` en niet in ``FROM_NAME_PATTERN``, dus een naam
        met een dollarteken kwam door het formulier en door het model heen en viel pas om in
        ``ensure_account`` -- midden in het verwerken van een project, waar niets die fout
        vangt. Een naam die hierboven mag, moet hier ook mogen, en andersom.
        """
        with pytest.raises(MailSenderNameError):
            render_sender_table({"project-x": naam})

    @pytest.mark.parametrize("naam", GOED)
    def test_de_relay_laat_dezelfde_gewone_namen_door(self, naam: str) -> None:
        """De andere richting: een naam die het formulier goedkeurt, moet ook echt in het
        gegenereerde script passen, anders is de goedkeuring een belofte die de verwerking
        niet nakomt."""
        render_sender_table({"project-x": naam})

    def test_het_vastgelegde_schema_draagt_de_regel_ook(self) -> None:
        """Het schemafragment is wat externe gereedschappen lezen; drift daarin betekent
        dat een client een waarde aanbiedt die de opslag weigert."""
        import json
        from pathlib import Path

        fragment = json.loads(
            (Path(__file__).resolve().parents[1] / "opi/services/catalog/send_email/send-email.v1.0.json").read_text()
        )
        veld = fragment["properties"]["from-name"]["anyOf"][0]
        assert veld["maxLength"] == 64
        assert veld["pattern"]


class TestHetAfzenderadresWordtGetoetst:
    """RC-399: het adres gaat net als de naam rechtstreeks een mailheader in.

    Dezelfde regel toetst het model, het formulier en de connector
    (``mail_sender_address_error`` in ``opi.utils.naming``), zodat een adres dat hier mag
    ook echt in de sieve-stringliteraal past. Zie ``TestMailSenderAddressError`` in
    ``test_naming_utils.py`` voor de regel zelf.
    """

    GOED: ClassVar[list[str]] = [
        "",
        "noreply@rijksoverheid.nl",
        "noreply.team@rijksoverheid.nl",
        "a" * MAIL_LOCAL_PART_MAX_LENGTH + "@rijksoverheid.nl",
    ]

    FOUT: ClassVar[dict[str, str]] = {
        "zonder apenstaart": "zonderapenstaart",
        "hoofdletter": "NoReply@rijksoverheid.nl",
        "spatie": "no reply@rijksoverheid.nl",
        "te lang lokaal deel": "a" * (MAIL_LOCAL_PART_MAX_LENGTH + 1) + "@rijksoverheid.nl",
        "verkeerd domein": "noreply@minbzk.nl",
    }

    @pytest.mark.parametrize("adres", GOED)
    def test_een_geldig_adres_mag(self, adres: str) -> None:
        assert SendEmailConfig(**{"from-address": adres}).from_address == adres

    @pytest.mark.parametrize(("geval", "adres"), list(FOUT.items()))
    def test_de_api_weigert(self, geval: str, adres: str) -> None:
        """Dit is de weg die het projectbestand schrijft: model en JSON-schema."""
        with pytest.raises(ValidationError):
            SendEmailConfig(**{"from-address": adres})

    @pytest.mark.parametrize(("geval", "adres"), list(FOUT.items()))
    def test_het_formulier_weigert_hetzelfde(self, geval: str, adres: str) -> None:
        """En dit is de weg die de wizard schrijft; zonder deze toets kan de twee uit
        elkaar lopen zoals bij de weergavenaam al eens gebeurde."""
        from opi.services.catalog.send_email.editables import SEND_EMAIL_FROM_ADDRESS_EDITABLE

        assert SEND_EMAIL_FROM_ADDRESS_EDITABLE.validator.validate(adres), f"{geval} hoort geweigerd te worden"

    @pytest.mark.parametrize("adres", [a for a in GOED if a])
    def test_het_formulier_laat_een_geldig_adres_door(self, adres: str) -> None:
        from opi.services.catalog.send_email.editables import SEND_EMAIL_FROM_ADDRESS_EDITABLE

        assert SEND_EMAIL_FROM_ADDRESS_EDITABLE.validator.validate(adres) == []

    def test_leeg_betekent_het_afgeleide_adres(self) -> None:
        """Leegmaken is een geldige uitkomst en geen fout: zie ``remove_when_none``."""
        from opi.services.catalog.send_email.editables import SEND_EMAIL_FROM_ADDRESS_EDITABLE

        assert SEND_EMAIL_FROM_ADDRESS_EDITABLE.remove_when_none is True


class TestWatDeDeploymentEnDeRelayTeZienKrijgen:
    """De hele weg een keer af: van ``from-name`` in het projectbestand tot het adres dat
    de applicatie in ``SMTP_FROM`` krijgt en de afzender die op de relay komt te staan.

    De losse toetsen hierboven pinnen elk een schakel; deze pint dat ze aan elkaar zitten.
    Dat is precies waar RC-145 over gaat: ``from-name`` stond al in het projectbestand van
    ai1-uit en had geen enkele lezer.
    """

    def _manager(self, secrets: list[tuple], monkeypatch) -> MailManager:
        from opi.handlers.project_file_handler import ProjectFileHandler

        async def get_name() -> str:
            return "ai1-uit"

        async def save_and_commit_project(*args, **kwargs) -> None:
            return None

        manager = MailManager(
            project_manager=SimpleNamespace(  # type: ignore[arg-type]
                _project_file_handler=ProjectFileHandler(),
                get_name=get_name,
                save_and_commit_project=save_and_commit_project,
                _add_secret_to_create=lambda deployment, dienst, secret: secrets.append((deployment, dienst, secret)),
            )
        )
        # Het bewaren van het account vraagt de AGE-sleutel van het project en dat is niet
        # wat hier wordt getoetst; de vorm van het opgeschreven blok staat in
        # TestHetOpgeschrevenAccountVeroudertNiet.
        monkeypatch.setattr(MailManager, "_store_account", AsyncMock())
        return manager

    @pytest.mark.asyncio
    async def test_het_projectadres_en_de_weergavenaam_komen_op_de_relay(self, monkeypatch) -> None:
        project = _project(
            component_services=[ServiceType.SEND_EMAIL.value],
            config={"from-name": "Robbert Uittenbroek"},
        )
        project["name"] = "ai1-uit"
        secrets: list[tuple] = []

        connector = MailConnector("http://relay", "admin", "geheim")
        connector.get_principal = AsyncMock(return_value=None)  # type: ignore[method-assign]
        connector.create_principal = AsyncMock()  # type: ignore[method-assign]
        connector.set_sender = AsyncMock(return_value=True)  # type: ignore[method-assign]
        connector.set_rate_limit = AsyncMock(return_value=True)  # type: ignore[method-assign]
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=connector))

        manager = self._manager(secrets, monkeypatch)
        await manager.create_resources_for_deployment(project, project["deployments"][0])

        connector.set_sender.assert_awaited_once_with("project-ai1-uit", "Robbert Uittenbroek", "")

        # En de applicatie krijgt hetzelfde adres te zien, want SMTP_FROM is een mededeling
        # over wat de ontvanger krijgt en niet iets waar de applicatie iets aan verandert.
        assert secrets[0][2].from_address == "noreply-rijksapp+ai1-uit@rijksoverheid.nl"

    @pytest.mark.asyncio
    async def test_zonder_from_name_gaat_er_geen_naam_mee(self, monkeypatch) -> None:
        """Een project zonder weergavenaam verstuurt met een kaal PROJECTadres. Dat is een
        geldige uitkomst en geen terugval: de terugval is het kale PLATFORMadres."""
        project = _project(component_services=[ServiceType.SEND_EMAIL.value])
        project["name"] = "ai1-uit"
        secrets: list[tuple] = []

        connector = MailConnector("http://relay", "admin", "geheim")
        connector.get_principal = AsyncMock(return_value=None)  # type: ignore[method-assign]
        connector.create_principal = AsyncMock()  # type: ignore[method-assign]
        connector.set_sender = AsyncMock(return_value=True)  # type: ignore[method-assign]
        connector.set_rate_limit = AsyncMock(return_value=True)  # type: ignore[method-assign]
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=connector))

        await self._manager(secrets, monkeypatch).create_resources_for_deployment(project, project["deployments"][0])

        connector.set_sender.assert_awaited_once_with("project-ai1-uit", "", "")
        # En het adres dat de applicatie te zien krijgt, is het adres dat de relay zelf
        # samenstelt uit de accountnaam - zie TestHetAdresWordtEenKeerSamengesteld.
        assert secrets[0][2].from_address == "noreply-rijksapp+ai1-uit@rijksoverheid.nl"


class TestTweeProjectenTegelijk:
    """De tabel wordt gelezen EN geschreven, dus twee projecten tegelijk is een echte vraag.

    Zonder grendel lezen ze allebei de tabel zoals hij was en schrijft de laatste die van de
    eerste weg - stil, en tot iemand dat project toevallig opnieuw verwerkt. Dat is precies
    het soort verlies dat niemand meldt, want de post gaat gewoon weg, alleen zonder naam.
    """

    @pytest.mark.asyncio
    async def test_de_naam_van_de_ander_blijft_staan(self) -> None:
        opgeslagen: dict[str, str] = {}
        traag = TestDeWeergavenaamStaatOpDeRelay()
        server = TestServer(traag._app([], [], opgeslagen))
        await server.start_server()
        connector = MailConnector(str(server.make_url("")).rstrip("/"), "admin", "geheim", verify_tls=False)
        try:
            await asyncio.gather(
                connector.set_sender_name("project-een", "Een"),
                connector.set_sender_name("project-twee", "Twee"),
            )
        finally:
            await server.close()

        tabel = opgeslagen[MAIL_SENDER_TABLE_KEY]
        assert 'set "naam" "Een"' in tabel
        assert 'set "naam" "Twee"' in tabel


class TestHetAfzenderadresPerAccount:
    """RC-159: het adres wordt AFGELEID, behalve voor een account dat er een heeft.

    De afleiding (accountnaam min het voorvoegsel ``project-``) is de regel en blijft dat.
    De uitzondering bestaat voor een account dat post moet versturen die van de post van de
    portal te onderscheiden hoort te zijn: dat van Keycloak, en sinds RC-399 ook een project
    met een goedgekeurd eigen adres. Het is een uitzondering met een scherpe rand: een
    instelbaar afzenderadres is een spoofingknop, en de reden dat hij hier mag bestaan is dat
    alleen een platformbeheerder hem openzet (``approved_from_address``).
    """

    def test_de_tabel_verklaart_beide_variabelen_global(self) -> None:
        """``global`` is wat een waarde over de scriptgrens tilt (RFC 6609).

        Zonder de verklaring aan BEIDE kanten leest het insluitende script stil leeg, en dan
        vertrekt de post gewoon - alleen onder het verkeerde adres. Dat is precies het soort
        stilte waar niets over meldt.
        """
        tabel = render_sender_table({"project-een": "Een"}, {})
        assert 'global "naam";' in tabel
        assert 'global "afzender";' in tabel

    def test_een_account_met_een_adres_krijgt_beide_toekenningen(self) -> None:
        tabel = render_sender_table(
            {"zad-keycloak": "Rijksapps"}, {"zad-keycloak": "noreply-inloggen@rijksoverheid.nl"}
        )
        assert (
            'if string :is "${env.authenticated_as}" "zad-keycloak" '
            '{ set "naam" "Rijksapps"; set "afzender" "noreply-inloggen@rijksoverheid.nl"; }'
        ) in tabel

    def test_een_account_zonder_adres_krijgt_geen_afzenderregel(self) -> None:
        """De afleiding is de regel: een project dat niets kiest, hoort geen ``afzender`` te
        zetten, want een lege waarde zou de afleiding hieronder juist overrulen."""
        tabel = render_sender_table({"project-een": "Een"}, {})
        assert 'set "naam" "Een";' in tabel
        assert "afzender" not in tabel.split("global", 2)[-1].split("\n", 1)[-1]

    def test_een_account_met_alleen_een_adres_krijgt_toch_een_regel(self) -> None:
        """Geen naam is een geldige uitkomst, ook naast een adres. De twee reeksen worden
        daarom SAMEN doorlopen; alleen de namen doorlopen zou dit account overslaan."""
        tabel = render_sender_table({}, {"zad-keycloak": "noreply-inloggen@rijksoverheid.nl"})
        assert 'set "afzender" "noreply-inloggen@rijksoverheid.nl";' in tabel
        assert '"naam"' not in tabel.split('global "afzender";', 1)[1]

    @pytest.mark.parametrize(
        "adres",
        [
            "zonder-apenstaart",
            "spatie erin@rijksoverheid.nl",
            'aanhaling"erin@rijksoverheid.nl',
            "dollar${x}@rijksoverheid.nl",
            "iemand@example.org",
            "iemand@rijksoverheid.nl.evil.example",
            "a" * 65 + "@rijksoverheid.nl",
            # Een afsluitende newline: die kwam er tot RC-159 langs, want het patroon
            # eindigde op ``$`` en dat matcht OOK vlak voor een slot-newline. Een newline
            # is precies het teken dat de sieve-string beeindigt waar dit adres in gaat.
            "iemand\n@rijksoverheid.nl",
            "iemand@rijksoverheid.nl\n",
        ],
    )
    def test_een_verkeerd_adres_wordt_geweigerd(self, adres: str) -> None:
        """De weigering hoort HIER te vallen en niet halverwege een verwerking.

        Twee soorten fout in een lijst, en dat is met opzet: tekens die de sieve-string
        zouden beeindigen (dan is de gegenereerde tabel een invoerkanaal), en een domein
        buiten de lijst (dan lijnt de From: niet meer uit met de envelope en haalt geen
        enkel bericht nog DMARC).
        """
        with pytest.raises(MailSenderNameError):
            render_sender_table({}, {"zad-keycloak": adres})

    def test_de_lijst_met_domeinen_is_het_afzenderdomein(self) -> None:
        """Uitlijning is het enige dat onze post door DMARC krijgt: wij ondertekenen niet met
        DKIM, dus het From:-domein moet gelijk blijven aan dat van de envelope, en die houdt
        het afgeleide adres."""
        _, _, domein = get_mail_from_address("odcn-production").partition("@")
        assert frozenset({domein}) == MAIL_SENDER_DOMAIN_ALLOWLIST

    def test_de_relay_is_met_hetzelfde_adres_geconfigureerd(self) -> None:
        """Het DERDE bestand waar dit afzenderdomein staat, en het enige dat nergens aan
        vastzat.

        ``MAIL_SENDER_DOMAIN_ALLOWLIST`` hangt aan ``cluster_config.py`` (de toets hierboven),
        maar de relay leest zijn eigen ``MAIL_FROM_LOCAL``/``MAIL_DOMAIN`` uit zijn Secret en
        stelt daar de envelope mee samen. Lopen die twee uiteen, dan meldt OPI een adres dat
        niet vertrekt en lijnt de From: niet meer uit met de envelope: de fout die de
        docstring van ``get_mail_from_address`` beschrijft als "a developer is shown one
        address while another one leaves the building", zonder dat iets dat tegenhield.

        De lijst blijft een HARDE lijst en wordt hier expres niet uit de configuratie
        afgeleid: dan zou een tikfout in een clusterconfiguratie het toegestane domein stil
        verbreden, en juist dat is wat een instelbaar afzenderadres gevaarlijk maakt.
        """
        template = (
            Path(__file__).resolve().parents[3]
            / "infrastructure/bootstrap/infrastructure/secrets/templates/mail-relay-secret.yaml"
        ).read_text()
        lokaal, _, domein = get_mail_from_address("odcn-production").partition("@")
        assert f'MAIL_FROM_LOCAL: "{lokaal}"' in template, "de relay stelt een ander lokaal deel samen dan OPI meldt"
        assert f'MAIL_DOMAIN: "{domein}"' in template, "de relay stelt een ander domein samen dan OPI toelaat"

    def test_de_relayconfiguratie_verklaart_dezelfde_variabele(self) -> None:
        """Drift tussen het gegenereerde script en het insluitende script faalt STIL.

        Verklaart de ene kant ``afzender`` global en de andere niet, dan leest de waarde
        leeg, valt de relay terug op de afleiding, en vertrekt Keycloak-post onder het adres
        van de portal. Er komt geen foutmelding en niets in een log.
        """
        config = (
            Path(__file__).resolve().parents[3]
            / "infrastructure/bootstrap/infrastructure/mail/controller/base/config.toml"
        ).read_text()
        assert 'global "afzender";' in config, "het insluitende script verklaart de variabele niet"
        assert 'set "adres" "${afzender}";' in config, "een ingeladen adres wint niet van de afleiding"

    def test_de_envelope_houdt_het_afgeleide_adres(self) -> None:
        """Bewust, en het ziet eruit als een vergissing.

        DMARC-uitlijning vergelijkt de DOMEINEN van envelope en From:, en die blijven gelijk.
        Het plusdeel blijft ondertussen in de envelope staan en blijft dus de bounce dragen.
        Zou de rewrite-regel het ingeladen adres ook gaan gebruiken, dan is dat weg.
        """
        config = (
            Path(__file__).resolve().parents[3]
            / "infrastructure/bootstrap/infrastructure/mail/controller/base/config.toml"
        ).read_text()
        rewrite = config.split("[session.mail]", 1)[1].split("[session.data]", 1)[0]
        assert "afzender" not in rewrite, "de envelope mag het ingeladen adres NIET gebruiken"


class TestDeXZadProjectKopregel:
    """Punt 4 uit het plan: de kopregel noemt het project uit de LOGIN, niet uit het
    bericht, en platformaccounts krijgen hem niet.

    Dezelfde valkuil als bij ``projectadres`` hierboven staat letterlijk in het
    commentaar: een ``let`` binnen een ``if``-blok overleeft dat blok niet. Zonder de
    ``deleteheader`` BUITEN de ``if`` kan een applicatie de kopregel zelf meesturen en de
    naam van een ander project voeren.
    """

    def _regels(self) -> list[str]:
        config = (
            Path(__file__).resolve().parents[3]
            / "infrastructure/bootstrap/infrastructure/mail/controller/base/config.toml"
        ).read_text()
        return config.splitlines()

    def test_de_deleteheader_en_de_let_staan_buiten_het_if_blok(self) -> None:
        regels = self._regels()
        let_regel = next(r for r in regels if r.startswith('let "project"'))
        delete_regel = next(r for r in regels if r.strip() == 'deleteheader "X-ZAD-Project";')
        assert not let_regel.startswith((" ", "\t")), "de let hoort buiten de if, anders overleeft hij hem niet"
        assert not delete_regel.startswith((" ", "\t")), (
            "de deleteheader hoort buiten de if, anders zet een applicatie zelf de kopregel"
        )

    def test_de_addheader_staat_wel_binnen_het_if_blok(self) -> None:
        """De tegenproef: alleen deze regel mag voorwaardelijk zijn, anders krijgt ook
        een platformaccount de kopregel."""
        regels = self._regels()
        add_regel = next(r for r in regels if 'addheader :last "X-ZAD-Project"' in r)
        assert add_regel.startswith((" ", "\t")), "zonder de if krijgt elk account de kopregel, ook platformaccounts"

    def test_het_project_komt_uit_de_login_niet_uit_het_bericht(self) -> None:
        config = "\n".join(self._regels())
        x_zad_block = config.split('deleteheader "X-ZAD-Project"', 1)[1]
        assert f"strip_prefix(account, '{MAIL_PROJECT_ACCOUNT_PREFIX}')" in config
        assert f"starts_with(account, '{MAIL_PROJECT_ACCOUNT_PREFIX}')" in x_zad_block


class TestDeLimietenInConfigToml:
    """RC-400: ``queue.limiter.inbound.account`` is vervangen door een vangnet per bericht
    en twee ingebouwde Stalwart-standaarden zijn overschreven (fase 0, punten 4 en 5 uit
    het plan). Deze toets bestaat zodat een latere wijziging die een regel weghaalt -
    waarna de ingebouwde standaard stil terugkeert - hier opvalt in plaats van pas in de
    sandbox."""

    def _limiters(self) -> dict[str, Any]:
        import tomllib

        config = (
            Path(__file__).resolve().parents[3]
            / "infrastructure/bootstrap/infrastructure/mail/controller/base/config.toml"
        ).read_bytes()
        return tomllib.loads(config.decode()).get("queue", {}).get("limiter", {}).get("inbound", {})

    def test_de_oude_vaste_limiet_per_account_is_weg(self) -> None:
        """Die limiet gold voor elk account hetzelfde; het echte plafond staat sinds
        RC-400 per account op de relay (``MailConnector.set_rate_limit``)."""
        assert "account" not in self._limiters()

    def test_het_platformplafond_telt_per_bericht_en_niet_per_verbinding(self) -> None:
        """Zonder een ``match`` op ``authenticated_as`` valt deze limiter in de groep
        ``remote`` en telt hij per VERBINDING (fase 0, punt 5): een pod die per bericht
        verbindt zou dan ver boven het bedoelde plafond uitkomen."""
        platform = self._limiters()["platform"]
        assert platform["enable"] is True
        assert "authenticated_as" in platform["match"]

    def test_het_ip_plafond_overschrijft_de_ingebouwde_vijf_per_seconde(self) -> None:
        """Ingebouwd standaard: 5 verbindingen per seconde per IP. Ongewijzigd haalt een
        toepassing die per bericht verbindt zo'n 18.000 berichten per uur."""
        ip = self._limiters()["ip"]
        assert ip["key"] == ["remote_ip"]
        assert ip["rate"] != "5/1s"

    def test_het_ontvangerplafond_telt_per_project_en_niet_over_alle_projecten_samen(self) -> None:
        """Ingebouwd standaard sleutelt op ``sender_domain`` + ``rcpt``; ons afzenderdomein
        is voor elk project ``rijksoverheid.nl``, dus zonder wijziging zou ELK project
        samen de grens van een ontvanger opsouperen. Met ``authenticated_as`` erbij telt
        het weer per project."""
        sender = self._limiters()["sender"]
        assert sender["key"] == ["authenticated_as", "rcpt"]

    def test_elke_limiter_staat_aan(self) -> None:
        for naam, limiter in self._limiters().items():
            assert limiter.get("enable") is True, f"{naam} staat uit en begrenst dus niets"


class TestHetKeycloakAccountKomtUitDeBootstrap:
    """RC-159: het derde geval, en het verschilt in precies een ding van het tweede.

    ``zad-platform`` wordt door niemand anders gelezen, dus OPI genereert dat wachtwoord
    zelf. Keycloak is een ander programma dat OPI niet kent en niet op OPI hoort te wachten,
    dus zijn wachtwoord komt uit de bootstrap en OPI is hier de RECONCILER, niet de bron.
    """

    @pytest.fixture(autouse=True)
    def _relay(self, monkeypatch):
        monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", "http://relay")
        monkeypatch.setattr(settings, "CLUSTER_MANAGER", "sandboxed-local")

    def _record_relay(self, monkeypatch) -> AsyncMock:
        ensured = AsyncMock(
            return_value=MailAccount(
                username="zad-keycloak",
                from_address="noreply-rijksapp@rijksoverheid.nl",
                bounce_address="noreply-rijksapp@rijksoverheid.nl",
                messages_per_day=2000,
            )
        )
        monkeypatch.setattr(MailManager, "ensure_account", ensured)
        monkeypatch.setattr("opi.manager.mail_manager.create_mail_connector", AsyncMock(return_value=object()))
        return ensured

    @pytest.mark.asyncio
    async def test_zonder_relay_gebeurt_er_niets(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", "")
        assert await MailManager.ensure_keycloak_account() is None

    @pytest.mark.asyncio
    async def test_zonder_geheim_is_dat_geen_fout(self, monkeypatch) -> None:
        """Een cluster waarvan de infrastructuurgeheimen ouder zijn dan deze functie heeft
        geen Keycloak-mail. Dat is een toestand en geen storing: het mag de start niet
        tegenhouden, net als een cluster zonder relay."""
        monkeypatch.setattr(MailManager, "_read_keycloak_secret", AsyncMock(return_value=None))
        ensured = self._record_relay(monkeypatch)

        assert await MailManager.ensure_keycloak_account() is None
        ensured.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_het_wachtwoord_komt_uit_het_geheim_en_wordt_niet_gegenereerd(self, monkeypatch) -> None:
        """De kern van deze weg. Genereert OPI hier alsnog iets, dan draagt de relay een
        wachtwoord dat het BESTAND van Keycloak niet heeft, en faalt elke inlogmail op
        authenticatie."""
        monkeypatch.setattr(MailManager, "_read_keycloak_secret", AsyncMock(return_value="uit-de-bootstrap"))
        ensured = self._record_relay(monkeypatch)

        await MailManager.ensure_keycloak_account()

        kwargs = ensured.await_args.kwargs
        assert kwargs["password"] == "uit-de-bootstrap"
        assert kwargs["username"] == settings.MAIL_KEYCLOAK_ACCOUNT
        assert kwargs["is_platform_account"] is True

    @pytest.mark.asyncio
    async def test_een_gewijzigd_geheim_bereikt_de_relay(self, monkeypatch) -> None:
        """Wat een rotatie werkend maakt: OPI leest bij elke start opnieuw uit het geheim en
        zet door wat het daar vindt."""
        monkeypatch.setattr(MailManager, "_read_keycloak_secret", AsyncMock(return_value="tweede-waarde"))
        ensured = self._record_relay(monkeypatch)

        await MailManager.ensure_keycloak_account()

        assert ensured.await_args.kwargs["password"] == "tweede-waarde"

    @pytest.mark.asyncio
    async def test_het_account_krijgt_het_kale_adres_en_de_eigen_naam(self, monkeypatch) -> None:
        """Inlogpost vertrekt onder het BASISadres van het cluster - geen eigen lokaal deel,
        geen plusdeel - en onderscheidt zich alleen nog door zijn weergavenaam.

        Wat we daarmee bewust opgeven: een bounce is niet meer te herleiden tot inlogpost in
        plaats van portalpost. Er is geen bounce-postbus, dus dat is vandaag theoretisch, en
        het is de prijs van EEN herkenbaar afzenderadres.
        """
        monkeypatch.setattr(MailManager, "_read_keycloak_secret", AsyncMock(return_value="w"))
        ensured = self._record_relay(monkeypatch)

        await MailManager.ensure_keycloak_account()

        kwargs = ensured.await_args.kwargs
        assert kwargs["sender_address"] == get_mail_from_address(settings.CLUSTER_MANAGER)
        assert kwargs["sender_address"] == "noreply-rijksapp@rijksoverheid.nl"
        assert kwargs["from_address"] == kwargs["sender_address"]
        assert kwargs["bounce_address"] == kwargs["sender_address"]
        assert kwargs["from_name"] == settings.MAIL_KEYCLOAK_FROM_NAME

    @pytest.mark.asyncio
    async def test_de_drie_plekken_noemen_hetzelfde_afzenderadres(self, monkeypatch) -> None:
        """Het adres van inlogpost staat op DRIE plekken, en die moeten hetzelfde noemen.

        Beweegt er een niet mee, dan vertrekt post onder een ander adres dan een realm
        claimt - precies de faalvorm die niemand ziet, want de relay stelt de ``From:`` zelf
        vast en de realm liegt dan alleen in zijn eigen configuratie. Twee van de drie zijn
        dezelfde aanroep, de derde is een LETTERLIJKE waarde in een manifest dat geen enkele
        Python-toets anders leest; die is de reden dat deze toets bestaat.

        Knip er een los - zet een ander adres in ``ZAD_MAIL_RELAY_FROM``, of laat
        ``_smtp_server_settings`` iets anders teruggeven - en deze toets valt om.
        """
        monkeypatch.setattr(MailManager, "_read_keycloak_secret", AsyncMock(return_value="w"))
        ensured = self._record_relay(monkeypatch)
        await MailManager.ensure_keycloak_account()

        # 1. Wat de relay als afzender van zad-keycloak krijgt.
        op_de_relay = ensured.await_args.kwargs["sender_address"]

        # 2. Wat OPI in de smtpServer van elke realm schrijft.
        op_de_realm = KeycloakYamlHandler._smtp_server_settings()["from"]

        # 3. Wat de Keycloak-pod uit zijn omgeving leest.
        deployment = yaml.safe_load(
            (
                Path(__file__).resolve().parents[3]
                / "infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml"
            ).read_text()
        )
        container = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "keycloak")
        in_de_pod = next(v["value"] for v in container["env"] if v["name"] == "ZAD_MAIL_RELAY_FROM")

        assert op_de_relay == op_de_realm == in_de_pod

    def test_het_adres_is_het_kale_adres_van_de_portal(self) -> None:
        """De aparte afleiding is VERVALLEN: er is geen eigen lokaal deel meer, dus er is
        ook niets meer dat van het basisadres kan afdrijven. Wat overblijft is de eis die er
        altijd al onder lag: envelope en From: in EEN domein, anders valt DMARC om."""
        for cluster in ("local", "sandboxed-local", "odcn-production"):
            assert get_mail_from_address(cluster) == "noreply-rijksapp@rijksoverheid.nl"

    @pytest.mark.asyncio
    async def test_de_projectweg_mag_ook_het_keycloak_account_niet_aanraken(self, monkeypatch) -> None:
        """De grendel kende een naam en kent er nu twee.

        Structureel kan een project er niet bij (projectaccounts dragen een voorvoegsel),
        maar deze grendel bestaat juist omdat accountnamen ook uit een projectbestand kunnen
        komen dat ouder is of gerepareerd is.
        """
        connector = MailConnector("http://relay", "admin", "geheim")
        connector.get_principal = AsyncMock(return_value=None)  # type: ignore[method-assign]
        with pytest.raises(MailAccountNameError):
            await MailManager.ensure_account(
                connector=connector,
                username=settings.MAIL_KEYCLOAK_ACCOUNT,
                password="x",
                from_address="a@b",
                bounce_address="a@b",
                from_name="",
                messages_per_day=1,
            )

    def test_beide_platformaccounts_staan_buiten_de_projectnaamruimte(self) -> None:
        """Een platformaccount BINNEN het voorvoegsel is vanaf de projectweg bereikbaar."""
        for naam in (settings.MAIL_PLATFORM_ACCOUNT, settings.MAIL_KEYCLOAK_ACCOUNT):
            assert not naam.startswith(MAIL_PROJECT_ACCOUNT_PREFIX)

    def test_de_twee_platformaccounts_zijn_niet_dezelfde(self) -> None:
        """Een account voor allebei zou een bounce op inlogpost niet te onderscheiden maken
        van een bounce op de post van de portal."""
        assert settings.MAIL_PLATFORM_ACCOUNT != settings.MAIL_KEYCLOAK_ACCOUNT


class TestDeKeycloakStartuptaakTrektDeBootNietOm:
    """RC-159: dezelfde eis als bij ``ensure_platform_mail_account``, en om een scherpere reden.

    ``server.py`` doet ``await run_startup_tasks(app)`` zonder ``try``, dus een uitzondering
    die uit fase 3c ontsnapt haalt fase 4 (Keycloak) en 5 (OAuth) onderuit. Wat deze taak
    inricht is de inlogpost van ALLE realms; een mislukte inrichting mag de portal dus niet
    meenemen, want dan ligt bij een onbereikbare relay ook het inloggen zelf plat.

    Vijf uitzonderingsvormen, en het zijn niet de exotische: de transportfout van aiohttp
    (sinds RC-261 maakt de connector er een ``MailRelayUnavailableError`` van, maar de
    kubectl-aanroep en het ontsleutelen eromheen gooien hem nog kaal), de DNS-fout, de twee
    kubectl-vormen van het lezen van het Secret, en de kale ``ValueError`` uit het
    ontsleutelen van het relay-wachtwoord.
    """

    @pytest.mark.asyncio
    async def test_an_unreachable_relay_is_logged_and_not_raised(self, monkeypatch) -> None:
        from opi.core import startup

        async def _boom() -> None:
            raise aiohttp.ClientConnectorError(
                connection_key=SimpleNamespace(ssl=None, host="relay", port=443, is_ssl=True),  # type: ignore[arg-type]
                os_error=OSError("Network is unreachable"),
            )

        monkeypatch.setattr(MailManager, "ensure_keycloak_account", _boom)
        assert await startup.ensure_keycloak_mail_account() is False

    @pytest.mark.asyncio
    async def test_a_relay_that_refuses_the_call_is_logged_and_not_raised(self, monkeypatch) -> None:
        from opi.connectors.mail import MailRelayError
        from opi.core import startup

        async def _boom() -> None:
            raise MailRelayError("POST /api/principal gaf 500")

        monkeypatch.setattr(MailManager, "ensure_keycloak_account", _boom)
        assert await startup.ensure_keycloak_mail_account() is False

    @pytest.mark.asyncio
    async def test_a_dns_failure_is_logged_and_not_raised(self, monkeypatch) -> None:
        """``socket.gaierror`` is een ``OSError``, en het is wat een onbekende relayhostnaam
        geeft voordat aiohttp iets eigens te werpen heeft."""
        from opi.core import startup

        async def _boom() -> None:
            raise OSError("Name or service not known")

        monkeypatch.setattr(MailManager, "ensure_keycloak_account", _boom)
        assert await startup.ensure_keycloak_mail_account() is False

    @pytest.mark.asyncio
    async def test_a_refused_secret_read_is_logged_and_not_raised(self, monkeypatch) -> None:
        """Deze weg LEEST een Secret in plaats van er een te schrijven, en een cluster dat
        die leesactie weigert moet gewoon opstarten zonder Keycloak-mail."""
        from opi.connectors.kubectl import KubectlExecutionError
        from opi.core import startup

        async def _boom() -> None:
            raise KubectlExecutionError('secrets "keycloak-mail-credentials" is forbidden')

        monkeypatch.setattr(MailManager, "ensure_keycloak_account", _boom)
        assert await startup.ensure_keycloak_mail_account() is False

    @pytest.mark.asyncio
    async def test_an_unreachable_api_server_is_logged_and_not_raised(self, monkeypatch) -> None:
        """``KubectlConnectionError`` is GEEN subklasse van ``KubectlExecutionError``, en het
        is juist de toestand waar de kubectl-connector zijn eigen herhaallus voor heeft: de
        API-server is tijdens het opstarten nog niet bereikbaar."""
        from opi.connectors.kubectl import KubectlConnectionError
        from opi.core import startup

        async def _boom() -> None:
            raise KubectlConnectionError("kubectl connection is not available")

        monkeypatch.setattr(MailManager, "ensure_keycloak_account", _boom)
        assert await startup.ensure_keycloak_mail_account() is False

    @pytest.mark.asyncio
    async def test_an_undecryptable_admin_password_is_logged_and_not_raised(self, monkeypatch) -> None:
        """``create_mail_connector`` ontsleutelt ``MAIL_RELAY_ADMIN_PASSWORD``; een waarde die
        nog niet te ontsleutelen is geeft een kale ``ValueError``."""
        from opi.core import startup

        async def _boom() -> None:
            raise ValueError("Failed to decrypt password: no matching AGE key")

        monkeypatch.setattr(MailManager, "ensure_keycloak_account", _boom)
        assert await startup.ensure_keycloak_mail_account() is False

    @pytest.mark.asyncio
    async def test_geen_relay_of_geen_geheim_is_none_zonder_uitzondering(self, monkeypatch) -> None:
        """De twee toestanden die GEEN storing zijn. ``ensure_keycloak_account`` antwoordt dan
        ``None``, en de taak geeft dat als ``None`` door en niet als ``False``.

        RC-261 maakte dat verschil dragend: de opstart-herhaallus probeert een ``False``
        opnieuw en zou op een cluster zonder relay anders eeuwig blijven tikken."""
        from opi.core import startup

        monkeypatch.setattr(MailManager, "ensure_keycloak_account", AsyncMock(return_value=None))
        assert await startup.ensure_keycloak_mail_account() is None

    @pytest.mark.asyncio
    async def test_een_ingericht_account_meldt_true(self, monkeypatch) -> None:
        from opi.core import startup

        account = MailAccount(
            username=settings.MAIL_KEYCLOAK_ACCOUNT,
            from_address="noreply-rijksapp@rijksoverheid.nl",
            bounce_address="noreply-rijksapp@rijksoverheid.nl",
            messages_per_day=settings.MAIL_KEYCLOAK_MESSAGES_PER_DAY,
        )
        monkeypatch.setattr(MailManager, "ensure_keycloak_account", AsyncMock(return_value=account))

        assert await startup.ensure_keycloak_mail_account() is True

    @pytest.mark.asyncio
    async def test_de_boot_roept_deze_taak_ook_echt_aan(self, monkeypatch) -> None:
        """Zonder deze schakel is alle dekking hierboven dood: de relay zou het account nooit
        krijgen, en elke realm zou een verwijzing dragen naar een wachtwoord dat aan de
        andere kant niet bestaat.

        Twee hops sinds RC-261: de boot roept ``_setup_mail_accounts`` aan en die roept beide
        accounts aan. Een toets op alleen de laatste zou een losgeraakte boot niet zien."""
        from opi.core import startup

        assert "_setup_mail_accounts(" in inspect.getsource(startup.run_startup_tasks)
        bron = inspect.getsource(startup._setup_mail_accounts)
        assert "ensure_keycloak_mail_account()" in bron
        assert "ensure_platform_mail_account()" in bron


class TestDeOpstartlusProbeertDeMailaccountsOpnieuw:
    """RC-261: bij een verse uitrol is de relay er nog niet als OPI opstart.

    Fase 3b was "non-critical, no retry", dus ``zad-platform`` en ``zad-keycloak``
    ontstonden dan pas bij de VOLGENDE herstart van OPI, en tot die tijd ging er geen
    platformmail en geen inlogmail uit. De bestaande herhaallus doet dit al voor database,
    projects, keycloak en oauth; de accounts horen daarin en niet in een tweede lus
    ernaast.
    """

    @pytest.fixture(autouse=True)
    def _snel(self, monkeypatch):
        """Zonder wachttijd, anders duurt elke ronde van de lus een minuut."""
        from opi.core import startup

        monkeypatch.setattr(startup, "STARTUP_RETRY_INTERVAL", 0)

    @staticmethod
    def _account() -> MailAccount:
        return MailAccount(
            username=settings.MAIL_PLATFORM_ACCOUNT,
            from_address="noreply-rijksapp@rijksoverheid.nl",
            bounce_address="noreply-rijksapp@rijksoverheid.nl",
            messages_per_day=settings.MAIL_PLATFORM_MESSAGES_PER_DAY,
        )

    @pytest.mark.asyncio
    async def test_een_onbereikbare_relay_wordt_opnieuw_geprobeerd_en_de_lus_stopt_na_succes(self, monkeypatch) -> None:
        from opi.core import startup

        pogingen: list[int] = []

        async def _platform() -> MailAccount:
            pogingen.append(1)
            if len(pogingen) == 1:
                raise aiohttp.ClientConnectorError(
                    connection_key=SimpleNamespace(ssl=None, host="relay", port=8080, is_ssl=False),  # type: ignore[arg-type]
                    os_error=OSError("Connection refused"),
                )
            return self._account()

        monkeypatch.setattr(MailManager, "ensure_platform_account", _platform)
        monkeypatch.setattr(MailManager, "ensure_keycloak_account", AsyncMock(return_value=self._account()))

        await asyncio.wait_for(
            startup._startup_retry_loop(MagicMock(), skip_checks=False, mail_accounts_done=False), timeout=10
        )

        assert len(pogingen) == 2, "de lus hoort het account na de transportfout nog een keer te proberen"

    @pytest.mark.asyncio
    async def test_een_cluster_zonder_relay_laat_de_lus_meteen_eindigen(self, monkeypatch) -> None:
        """De tegenproef, en de reden dat ``None`` iets anders moet betekenen dan ``False``:
        zonder dat verschil zou deze lus op zo'n cluster eeuwig elke minuut een regel
        schrijven."""
        from opi.core import startup

        platform = AsyncMock(return_value=None)
        keycloak = AsyncMock(return_value=None)
        monkeypatch.setattr(MailManager, "ensure_platform_account", platform)
        monkeypatch.setattr(MailManager, "ensure_keycloak_account", keycloak)

        await asyncio.wait_for(
            startup._startup_retry_loop(MagicMock(), skip_checks=False, mail_accounts_done=False), timeout=10
        )

        assert platform.await_count == 1
        assert keycloak.await_count == 1

    @pytest.mark.asyncio
    async def test_de_lus_blijft_proberen_zolang_een_van_de_twee_faalt(self, monkeypatch) -> None:
        """Keycloak faalt en het platformaccount staat al: dat is nog niet afgehandeld. Zonder
        deze eis zou inlogmail stil wegblijven terwijl de portalmail werkt."""
        from opi.core import startup

        monkeypatch.setattr(MailManager, "ensure_platform_account", AsyncMock(return_value=self._account()))
        pogingen: list[int] = []

        async def _keycloak() -> MailAccount:
            pogingen.append(1)
            if len(pogingen) < 3:
                raise MailRelayError("POST /api/principal gaf 500")
            return self._account()

        monkeypatch.setattr(MailManager, "ensure_keycloak_account", _keycloak)

        await asyncio.wait_for(
            startup._startup_retry_loop(MagicMock(), skip_checks=False, mail_accounts_done=False), timeout=10
        )

        assert len(pogingen) == 3

    @pytest.mark.asyncio
    async def test_de_boot_geeft_de_uitkomst_aan_de_lus_door(self, monkeypatch) -> None:
        """De schakel die het geheel pas laat werken: zonder doorgeven start de lus met
        ``mail_accounts_done=True`` (de standaard) en probeert hij de accounts nooit."""
        from fastapi import FastAPI
        from opi.core import startup
        from opi.core.readiness import get_readiness_state

        for dienst in get_readiness_state().get_all_services():
            dienst.mark_ready()

        monkeypatch.setattr(startup, "check_schema_versions", lambda *a, **k: None)
        monkeypatch.setattr(
            startup, "get_metrics_connector", AsyncMock(return_value=SimpleNamespace(is_connected=True))
        )
        monkeypatch.setattr(startup, "_setup_database", AsyncMock(return_value=True))
        monkeypatch.setattr(startup, "_setup_projects", AsyncMock(return_value=True))
        monkeypatch.setattr(startup, "_resolve_running_image", AsyncMock(return_value=None))
        monkeypatch.setattr(startup, "check_minio_availability", AsyncMock(return_value=True))
        monkeypatch.setattr(startup, "_setup_keycloak", AsyncMock(return_value=True))
        monkeypatch.setattr(startup, "_setup_oauth", AsyncMock(return_value=True))
        monkeypatch.setattr(startup, "reconcile_caa_records", AsyncMock(return_value=None))
        monkeypatch.setattr(startup, "reconcile_no_mail_records", AsyncMock(return_value=None))
        monkeypatch.setattr(startup, "_setup_mail_accounts", AsyncMock(return_value=False))

        meegegeven: list[object] = []

        async def _lus(app, skip_checks, mail_accounts_done=True) -> None:
            meegegeven.append(mail_accounts_done)

        monkeypatch.setattr(startup, "_startup_retry_loop", _lus)

        app = FastAPI()
        assert await startup.run_startup_tasks(app) is False
        await app.state.startup_retry_task

        assert meegegeven == [False]
