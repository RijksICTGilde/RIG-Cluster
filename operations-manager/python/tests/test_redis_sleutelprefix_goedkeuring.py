"""Redis zonder sleutelprefix vraagt het besluit van een platformbeheerder (RC-338).

De gedeelde Redis geeft elk project een ACL-gebruiker die alleen bij zijn eigen prefix
mag. ``acl-key-prefix: false`` zet dat uit en geeft bereik over de hele gedeelde
instantie, dus die stand geldt pas na goedkeuring. Deze tests houden de vier plekken
vast waar dat uit elkaar kan lopen: de generieke vorm ``config_value_approval``, de
declaratie bij redis, de poort in ``redis_manager`` en de overname van bestaande
opt-outs door de migratie naar 2.10.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.project_schema import validate_project_schema
from opi.manager.project_validation import validate_approval_decisions
from opi.services.approvals import (
    apply_approval_verdicts,
    collect_approval_items,
    collect_deployment_approval_notices,
    ensure_approval_requests,
)
from opi.services.catalog.approval import ApprovalStatus, ApproverScope, config_value_approval
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.redis import APPROVAL as REDIS_APPROVAL
from opi.services.catalog.redis import is_acl_key_prefix_approved
from opi.services.catalog.redis.config_model import ACL_KEY_PREFIX_FIELD
from opi.services.registry import SERVICES, approval_services
from opi.services.schema_migration import (
    LATEST_SCHEMA_VERSION_VALUE,
    approve_existing_acl_key_prefix_optout,
    migrate_to_latest,
)
from opi.services.services_enums import ServiceType
from opi.utils.yaml_util import load_yaml_from_string

APPROVAL_FIELD = f"{ACL_KEY_PREFIX_FIELD}-approval"
DEPLOYMENT = {"name": "productie", "cluster": "sandboxed-local", "namespace": "p1"}


def _project(config: dict[str, Any] | None, *, service: str = ServiceType.REDIS.value) -> dict[str, Any]:
    """Een project met een dienstvermelding, of zonder als ``config`` None is."""
    services: list[Any] = [] if config is None else [{"name": service, "config": config}]
    return {"name": "p1", "services": services, "deployments": [dict(DEPLOYMENT)]}


def _redis_block(project: dict[str, Any]) -> dict[str, Any] | None:
    return project["services"][0]["config"].get(APPROVAL_FIELD)


class TestDeVormZelf:
    """``config_value_approval`` met een booleaans en een stringveld (plan stap 1)."""

    @staticmethod
    def _boolean() -> Any:
        return config_value_approval(
            ServiceType.REDIS,
            field="wijd",
            label="Wijd bereik",
            activity="Wijd bereik gebruiken",
            consequence="Het bereik blijft smal.",
            required_when=lambda config: config.get("wijd", False) is True,
        )

    @staticmethod
    def _string() -> Any:
        return config_value_approval(
            ServiceType.SEND_EMAIL,
            field="afzender",
            label="Eigen afzenderadres",
            activity="Een eigen afzenderadres gebruiken",
            consequence="De standaardafzender blijft gelden.",
            required_when=lambda config: bool(config.get("afzender")),
        )

    def test_waarde_die_het_predicaat_raakt_levert_een_aanvraag(self) -> None:
        approval = self._boolean()
        project = _project({"wijd": True})
        approval.ensure_requested(project)
        assert project["services"][0]["config"]["wijd-approval"] == {
            "status": ApprovalStatus.REQUESTED.value,
            "value": True,
            "history": [],
        }

    def test_waarde_die_het_predicaat_niet_raakt_levert_niets(self) -> None:
        approval = self._boolean()
        project = _project({"wijd": False})
        approval.ensure_requested(project)
        assert "wijd-approval" not in project["services"][0]["config"]

    def test_een_tweede_keer_draaien_verandert_niets(self) -> None:
        approval = self._boolean()
        project = _project({"wijd": True})
        approval.ensure_requested(project)
        before = dict(project["services"][0]["config"]["wijd-approval"])
        approval.ensure_requested(project)
        assert project["services"][0]["config"]["wijd-approval"] == before

    def test_een_stringveld_gaat_net_zo(self) -> None:
        """De vorm die #177 straks gebruikt: een string in plaats van een boolean."""
        approval = self._string()
        project = _project({"afzender": "post@example.nl"}, service=ServiceType.SEND_EMAIL.value)
        approval.ensure_requested(project)
        block = project["services"][0]["config"]["afzender-approval"]
        assert block == {"status": ApprovalStatus.REQUESTED.value, "value": "post@example.nl", "history": []}

    def test_een_goedgekeurde_waarde_die_verandert_gaat_terug_naar_requested(self) -> None:
        approval = self._string()
        project = _project(
            {
                "afzender": "post@example.nl",
                "afzender-approval": {"status": "approved", "value": "post@example.nl", "history": []},
            },
            service=ServiceType.SEND_EMAIL.value,
        )
        assert approval.is_approved(project)

        project["services"][0]["config"]["afzender"] = "anders@example.nl"
        approval.ensure_requested(project)
        block = project["services"][0]["config"]["afzender-approval"]
        assert block["status"] == ApprovalStatus.REQUESTED.value
        assert block["value"] == "anders@example.nl"
        assert not approval.is_approved(project)

    def test_terug_naar_de_onschuldige_stand_ruimt_het_besluit_op(self) -> None:
        """Een besluit geldt voor de stand waarvoor het gold.

        Bleef het blok staan, dan gold een oude goedkeuring opnieuw zodra het veld
        terugging, en weigerde ``validate_approval_decisions`` daarna het opslaan.
        """
        approval = self._boolean()
        project = _project({"wijd": True, "wijd-approval": {"status": "approved", "value": True, "history": []}})

        project["services"][0]["config"]["wijd"] = False
        approval.ensure_requested(project)
        assert "wijd-approval" not in project["services"][0]["config"]

        project["services"][0]["config"]["wijd"] = True
        approval.ensure_requested(project)
        assert project["services"][0]["config"]["wijd-approval"]["status"] == ApprovalStatus.REQUESTED.value

    def test_een_dienst_die_niet_geselecteerd_is_levert_niets(self) -> None:
        approval = self._boolean()
        project = _project(None)
        approval.ensure_requested(project)
        assert project["services"] == []

    def test_het_oordeel_landt_met_een_history_regel(self) -> None:
        approval = self._boolean()
        spec = approval.spec
        project = _project({"wijd": True})
        approval.ensure_requested(project)

        items = spec.list_items(project) if spec.list_items else []
        assert len(items) == 1
        items[0]["status"] = "approved"
        items[0]["message"] = "akkoord"
        assert spec.record is not None
        spec.record(project, items[0], {"date": "2026-10-18T10:00:00+00:00", "status": "approved", "by": "beheerder"})

        block = project["services"][0]["config"]["wijd-approval"]
        assert block["status"] == "approved"
        assert block["value"] is True
        assert block["history"][-1] == {
            "date": "2026-10-18T10:00:00+00:00",
            "status": "approved",
            "by": "beheerder",
        }
        assert approval.is_approved(project)

    def test_de_approver_is_standaard_de_platformbeheerder(self) -> None:
        assert self._boolean().spec.approver is ApproverScope.PLATFORM_ADMIN


class TestRedisDeclareertDeGoedkeuring:
    """Plan stap 2: de aanvraag komt op de beheerpagina en het oordeel komt terug."""

    def test_redis_staat_tussen_de_diensten_met_een_goedkeuring(self) -> None:
        assert SERVICES[ServiceType.REDIS] in approval_services()
        specs = SERVICES[ServiceType.REDIS].config_approvals(ConfigLayer.PROJECT)
        assert [spec.key for spec in specs] == [ACL_KEY_PREFIX_FIELD]
        assert SERVICES[ServiceType.REDIS].config_approvals(ConfigLayer.COMPONENT) == []

    def test_het_veld_is_platform_managed(self) -> None:
        """Een project dat zijn eigen status mag zetten heeft geen goedkeuring."""
        assert APPROVAL_FIELD in SERVICES[ServiceType.REDIS].platform_managed_fields(ConfigLayer.PROJECT)

    def test_de_aanvraag_staat_op_de_beheerpagina(self) -> None:
        project = _project({ACL_KEY_PREFIX_FIELD: False})
        ensure_approval_requests(project)
        items = [item for item in collect_approval_items(project) if item["type"] == ACL_KEY_PREFIX_FIELD]
        assert len(items) == 1
        assert items[0]["service"] == ServiceType.REDIS.value
        assert items[0]["current_status"] == ApprovalStatus.REQUESTED.value
        assert items[0]["label"] == "Redis zonder sleutelprefix"
        assert items[0]["subject"] == "Redis gebruiken zonder sleutelprefix"

    def test_met_het_prefix_aan_staat_er_niets_open(self) -> None:
        project = _project({ACL_KEY_PREFIX_FIELD: True})
        ensure_approval_requests(project)
        assert [item for item in collect_approval_items(project) if item["type"] == ACL_KEY_PREFIX_FIELD] == []

    def test_het_oordeel_komt_via_het_paar_service_type_terug_bij_redis(self) -> None:
        project = _project({ACL_KEY_PREFIX_FIELD: False})
        ensure_approval_requests(project)
        items = collect_approval_items(project)
        for item in items:
            if item["type"] == ACL_KEY_PREFIX_FIELD:
                item["status"] = "approved"
        apply_approval_verdicts(project, items, admin_email="beheerder@example.nl")

        block = _redis_block(project)
        assert block is not None
        assert block["status"] == "approved"
        assert block["value"] is False
        assert block["history"][-1]["by"] == "beheerder@example.nl"
        assert is_acl_key_prefix_approved(project)


class TestDeMeldingAanDeAanvrager:
    """Plan stap 6: wat de eigenaar op de projectpagina te zien krijgt."""

    @staticmethod
    def _notices(project: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            notice
            for notice in collect_deployment_approval_notices(project, project["deployments"][0])
            if notice["type"] == ACL_KEY_PREFIX_FIELD
        ]

    def test_uitzetten_en_opslaan_meldt_de_aanvraag_en_het_gevolg(self) -> None:
        project = _project({ACL_KEY_PREFIX_FIELD: False})
        ensure_approval_requests(project)
        notices = self._notices(project)
        assert len(notices) == 1
        assert notices[0]["status"] == ApprovalStatus.REQUESTED.value
        assert notices[0]["label"] == "Redis zonder sleutelprefix"
        assert "wacht op goedkeuring" in notices[0]["text"]
        assert "houdt het prefix" in notices[0]["text"]

    def test_een_afwijzing_meldt_het_oordeel(self) -> None:
        project = _project(
            {
                ACL_KEY_PREFIX_FIELD: False,
                APPROVAL_FIELD: {
                    "status": "denied",
                    "value": False,
                    "history": [{"date": "2026-10-18T10:00:00+00:00", "status": "denied", "by": "beheerder"}],
                },
            }
        )
        notices = self._notices(project)
        assert len(notices) == 1
        assert notices[0]["status"] == "denied"
        assert notices[0]["by"] == "beheerder"
        assert "is afgewezen" in notices[0]["text"]

    def test_goedgekeurd_of_prefix_aan_meldt_niets(self) -> None:
        approved = _project(
            {ACL_KEY_PREFIX_FIELD: False, APPROVAL_FIELD: {"status": "approved", "value": False, "history": []}}
        )
        assert self._notices(approved) == []
        assert self._notices(_project({ACL_KEY_PREFIX_FIELD: True})) == []


class TestDePoortInDeManager:
    """Plan stap 4: welk prefix de ACL-gebruiker krijgt."""

    @pytest.mark.parametrize(
        ("block", "approved"),
        [
            (None, False),
            ({"status": "requested", "value": False, "history": []}, False),
            ({"status": "denied", "value": False, "history": []}, False),
            ({"status": "approved", "value": False, "history": []}, True),
            # Een oordeel over een andere waarde telt niet mee.
            ({"status": "approved", "value": True, "history": []}, False),
        ],
    )
    def test_de_opt_out_geldt_alleen_als_hij_is_goedgekeurd(self, block: dict | None, approved: bool) -> None:
        config: dict[str, Any] = {ACL_KEY_PREFIX_FIELD: False}
        if block is not None:
            config[APPROVAL_FIELD] = block
        assert is_acl_key_prefix_approved(_project(config)) is approved

    def test_zonder_opt_out_geldt_de_poort_nooit(self) -> None:
        assert is_acl_key_prefix_approved(_project({ACL_KEY_PREFIX_FIELD: True})) is False
        assert is_acl_key_prefix_approved(_project({})) is False
        assert is_acl_key_prefix_approved(_project(None)) is False

    def test_de_manager_leidt_het_prefix_hieruit_af(self) -> None:
        """De manager leest de status niet zelf: een functie beslist."""
        from opi.utils.naming import generate_redis_key_prefix

        niet_goedgekeurd = _project({ACL_KEY_PREFIX_FIELD: False})
        goedgekeurd = _project(
            {ACL_KEY_PREFIX_FIELD: False, APPROVAL_FIELD: {"status": "approved", "value": False, "history": []}}
        )
        assert _key_prefix(niet_goedgekeurd) == generate_redis_key_prefix("p1", "productie")
        assert _key_prefix(goedgekeurd) == ""

    @pytest.mark.parametrize(
        ("config", "verwacht_prefix"),
        [
            ({ACL_KEY_PREFIX_FIELD: False}, "productie-p1"),
            (
                {ACL_KEY_PREFIX_FIELD: False, APPROVAL_FIELD: {"status": "approved", "value": False, "history": []}},
                "",
            ),
        ],
    )
    async def test_create_resources_for_deployment_geeft_het_prefix_mee_aan_de_acl_gebruiker(
        self, config: dict[str, Any], verwacht_prefix: str
    ) -> None:
        """Niet de afgeleide regel, maar de echte manager: dit zou rood worden als de
        poort losraakt van ``create_resources_for_deployment`` zelf."""
        from opi.manager.redis_manager import RedisManager

        project_manager = MagicMock()
        project_manager.get_name = AsyncMock(return_value="p1")
        project_manager.get_progress_manager = MagicMock(return_value=None)
        project_manager._add_secret_to_create = MagicMock()

        manager = RedisManager(project_manager)
        manager._deployment_uses_redis = AsyncMock(return_value=True)
        manager._project_uses_namespace_redis = MagicMock(return_value=False)
        manager._test_redis_connection = AsyncMock(return_value=True)
        manager._get_existing_redis_credentials_from_k8s = AsyncMock(return_value=None)
        manager._check_acl_user_exists = AsyncMock(return_value=False)
        manager._create_acl_user = AsyncMock()

        project_data = _project(config)
        await manager.create_resources_for_deployment(project_data, dict(DEPLOYMENT))

        manager._create_acl_user.assert_awaited_once()
        assert manager._create_acl_user.await_args.args[-1] == verwacht_prefix


def _key_prefix(project: dict[str, Any]) -> str:
    """De regel uit ``RedisManager.setup_redis_for_deployment`` die het prefix bepaalt."""
    from opi.utils.naming import generate_redis_key_prefix

    use_key_prefix = not is_acl_key_prefix_approved(project)
    return generate_redis_key_prefix(project["name"], "productie") if use_key_prefix else ""


class TestDeMigratie:
    """Plan stap 5: bestaande opt-outs worden overgenomen, nieuwe niet."""

    @staticmethod
    def _v29(config: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema-version": 2.9,
            "name": "mb-grist-helmfile",
            "display-name": "Grist",
            "users": [{"email": "beheerder@example.nl", "role": "admin"}],
            "clusters": ["sandboxed-local"],
            "services": [{"name": ServiceType.REDIS.value, "config": config}],
        }

    def test_een_bestaande_opt_out_wordt_goedgekeurd_overgenomen(self) -> None:
        migrated, was_migrated = migrate_to_latest(self._v29({ACL_KEY_PREFIX_FIELD: False}))
        assert was_migrated
        assert migrated["schema-version"] == LATEST_SCHEMA_VERSION_VALUE

        block = migrated["services"][0]["config"][APPROVAL_FIELD]
        assert block["status"] == "approved"
        assert block["value"] is False
        assert len(block["history"]) == 1
        entry = block["history"][0]
        assert entry["status"] == "approved"
        assert entry["by"] == "schema-migration"
        assert entry["message"] == "Bestaande opt-out overgenomen bij de migratie naar 2.10"
        assert entry["date"]

        assert is_acl_key_prefix_approved(migrated)
        validate_project_schema(migrated)

    def test_een_bestand_zonder_opt_out_krijgt_geen_blok(self) -> None:
        migrated, _ = migrate_to_latest(self._v29({ACL_KEY_PREFIX_FIELD: True}))
        assert APPROVAL_FIELD not in migrated["services"][0]["config"]
        validate_project_schema(migrated)

    def test_de_migratie_is_idempotent(self) -> None:
        migrated, _ = migrate_to_latest(self._v29({ACL_KEY_PREFIX_FIELD: False}))
        again, was_migrated = migrate_to_latest(migrated)
        assert not was_migrated
        assert (
            again["services"][0]["config"][APPROVAL_FIELD]["history"]
            == (migrated["services"][0]["config"][APPROVAL_FIELD]["history"])
        )

    def test_de_stapfunctie_zelf_is_idempotent_ook_los_van_de_versiepoort(self) -> None:
        """``migrate_to_latest`` draait de stap maar een keer per bestand (de versiepoort),
        maar de stapfunctie bewaakt dat ook zelf: roep haar twee keer rechtstreeks aan en
        de tweede keer mag geen tweede history-regel bijschrijven."""
        project = self._v29({ACL_KEY_PREFIX_FIELD: False})

        eerste = approve_existing_acl_key_prefix_optout(project)
        assert eerste is True
        tweede = approve_existing_acl_key_prefix_optout(project)
        assert tweede is False

        history = project["services"][0]["config"][APPROVAL_FIELD]["history"]
        assert len(history) == 1

    def test_een_opt_out_na_de_migratie_begint_als_aanvraag(self) -> None:
        """Het vinkje weer aan en daarna weer uit: een nieuw besluit, en opslaanbaar."""
        migrated, _ = migrate_to_latest(self._v29({ACL_KEY_PREFIX_FIELD: False}))
        migrated["deployments"] = [dict(DEPLOYMENT)]
        config = migrated["services"][0]["config"]

        config[ACL_KEY_PREFIX_FIELD] = True
        ensure_approval_requests(migrated)
        assert APPROVAL_FIELD not in config

        previous = deepcopy(migrated)
        config[ACL_KEY_PREFIX_FIELD] = False
        ensure_approval_requests(migrated)
        assert config[APPROVAL_FIELD]["status"] == ApprovalStatus.REQUESTED.value
        assert not is_acl_key_prefix_approved(migrated)
        # RC-286: de hernieuwde opt-out is geen besluit, dus de schrijfweg laat hem door.
        validate_approval_decisions(previous, migrated)
        validate_project_schema(migrated)


class TestDeKaartEnDeUitleg:
    """De kaart en de popup-uitleg gaan over het AANZETTEN, niet over de spec.

    ``requires_approval`` op de kaart en de banner boven de uitleg kwamen uit
    ``approval_specs()`` niet-leeg. Redis kreeg daarmee de pil "vereist goedkeuring"
    terwijl alleen de wijde stand van ``acl-key-prefix`` een besluit vraagt.
    """

    @staticmethod
    def _card(service: ServiceType) -> dict[str, Any]:
        from opi.forms.visualizers.providers import ServiceOptionsProvider

        options = {option["value"]: option for option in ServiceOptionsProvider().get_options()}
        assert service.value in options
        return options[service.value]

    def test_het_aanzetten_van_redis_vraagt_niets(self) -> None:
        assert SERVICES[ServiceType.REDIS].enabling_needs_approval() is False
        assert SERVICES[ServiceType.SEND_EMAIL].enabling_needs_approval() is True

    def test_de_kaart_van_redis_draagt_de_pil_niet(self) -> None:
        assert "requires_approval" not in self._card(ServiceType.REDIS)
        assert self._card(ServiceType.SEND_EMAIL)["requires_approval"] is True

    def test_de_uitleg_van_redis_draagt_de_banner_niet(self) -> None:
        from opi.services.help_text import render_service_help

        assert "Vereist goedkeuring" not in render_service_help("redis/help.md")
        assert "Vereist goedkeuring" in render_service_help("send_email/help.md")


class TestDeImplicieteAanmelding:
    """Plan stap 3: redis blijft impliciet in de smalle stand."""

    def test_redis_meldt_zich_nog_impliciet_aan(self) -> None:
        assert SERVICES[ServiceType.REDIS].implicit_project_entry() == ServiceType.REDIS.value

    def test_de_spec_vraagt_alleen_om_een_besluit_bij_de_wijde_stand(self) -> None:
        spec = REDIS_APPROVAL.spec
        assert spec.requires_approval({}) is False
        assert spec.requires_approval({ACL_KEY_PREFIX_FIELD: True}) is False
        assert spec.requires_approval({ACL_KEY_PREFIX_FIELD: False}) is True


class TestDeSchrijfwegenVanHetFormulier:
    """Elke formulier-schrijfweg legt de aanvraag vast, niet alleen de bewerkwizard.

    ``ensure_approval_requests`` draaide alleen bij het opslaan van de bewerkwizard en in
    de API. Het aanmaken van een project en de bewerkdialogen kwamen er niet langs, dus
    zag de platformbeheerder een opt-out die daar werd gezet nooit, en liep een project
    met een goedgekeurde opt-out in de weigering van RC-286 zodra het vinkje heen en weer
    ging.
    """

    @staticmethod
    def _nieuw_project(config: dict[str, Any]) -> dict[str, Any]:
        """Een volledig projectbestand zoals de create-wizard het aanlevert."""
        age = "-----BEGIN AGE ENCRYPTED FILE-----\nnep\n-----END AGE ENCRYPTED FILE-----"
        return {
            "schema-version": LATEST_SCHEMA_VERSION_VALUE,
            "name": "p1",
            "display-name": "Project 1",
            "users": [{"email": "beheerder@example.nl", "role": "admin"}],
            "clusters": ["sandboxed-local"],
            "services": [{"name": ServiceType.REDIS.value, "config": config}],
            "components": [{"name": "frontend", "type": "single", "services": [ServiceType.REDIS.value]}],
            "config": {
                "age-public-key": "age1nepnepnep",
                "age-private-key": age,
            },
        }

    @staticmethod
    async def _modal_save(project: dict[str, Any]) -> dict[str, Any]:
        """De opslagstap van een bewerkdialoog, met de merge als gegeven.

        Dezelfde weg als ``_modal_do_submit``: de merge levert het projectbestand, en
        daarna is dit de enige stap tussen de merge en de store.
        """
        from opi.web.router_detail_edit import _process_and_save_modal_edit

        flow = MagicMock()
        flow.flow_id = "modal-edit-redis-config"
        manager = MagicMock()
        manager.get_contents = AsyncMock(return_value=deepcopy(project))
        manager.save_and_commit_project = AsyncMock()

        with (
            patch("opi.web.router_detail_edit.apply_modal_edit", new=AsyncMock(return_value=deepcopy(project))),
            patch("opi.web.router_detail_edit.extract_attachment_catalog", return_value={}),
        ):
            saved, response = await _process_and_save_modal_edit(
                request=MagicMock(),
                project_manager=manager,
                project_name="p1",
                flow=flow,
                wizard_token="token",
                merged_data={},
                active_sections=[],
                state=MagicMock(),
            )
        assert response is None
        manager.save_and_commit_project.assert_awaited_once()
        return saved

    async def test_een_opt_out_in_de_bewerkdialoog_komt_op_de_beheerpagina(self) -> None:
        saved = await self._modal_save(_project({ACL_KEY_PREFIX_FIELD: False}))

        items = [item for item in collect_approval_items(saved) if item["type"] == ACL_KEY_PREFIX_FIELD]
        assert len(items) == 1
        assert items[0]["current_status"] == ApprovalStatus.REQUESTED.value

    async def test_het_vinkje_heen_en_weer_blijft_opslaanbaar(self) -> None:
        """De val van RC-286 op deze schrijfweg: een besluit dat zijn stand overleeft."""
        goedgekeurd = _project(
            {ACL_KEY_PREFIX_FIELD: False, APPROVAL_FIELD: {"status": "approved", "value": False, "history": []}}
        )

        aan = deepcopy(goedgekeurd)
        aan["services"][0]["config"][ACL_KEY_PREFIX_FIELD] = True
        na_aan = await self._modal_save(aan)
        validate_approval_decisions(goedgekeurd, na_aan)
        assert APPROVAL_FIELD not in na_aan["services"][0]["config"]

        uit = deepcopy(na_aan)
        uit["services"][0]["config"][ACL_KEY_PREFIX_FIELD] = False
        na_uit = await self._modal_save(uit)
        validate_approval_decisions(na_aan, na_uit)
        assert na_uit["services"][0]["config"][APPROVAL_FIELD]["status"] == ApprovalStatus.REQUESTED.value
        assert not is_acl_key_prefix_approved(na_uit)

    async def test_een_opt_out_bij_het_aanmaken_komt_op_de_beheerpagina(self) -> None:
        from opi.web.router_wizard import _start_project_creation

        created: dict[str, Any] = {}

        async def _create_async_task(**kwargs: Any) -> dict[str, Any]:
            created.update(kwargs["payload"])
            return {"task_id": "t1"}

        with (
            patch("opi.core.task_helpers.create_async_task", new=_create_async_task),
            patch("opi.web.router_wizard.clear_wizard_state"),
        ):
            await _start_project_creation(MagicMock(), self._nieuw_project({ACL_KEY_PREFIX_FIELD: False}))

        geschreven = load_yaml_from_string(created["yaml_content"]) or {}
        items = [item for item in collect_approval_items(geschreven) if item["type"] == ACL_KEY_PREFIX_FIELD]
        assert len(items) == 1
        assert items[0]["current_status"] == ApprovalStatus.REQUESTED.value

    async def test_aanmaken_met_het_prefix_aan_vraagt_niets_aan(self) -> None:
        from opi.web.router_wizard import _start_project_creation

        created: dict[str, Any] = {}

        async def _create_async_task(**kwargs: Any) -> dict[str, Any]:
            created.update(kwargs["payload"])
            return {"task_id": "t1"}

        with (
            patch("opi.core.task_helpers.create_async_task", new=_create_async_task),
            patch("opi.web.router_wizard.clear_wizard_state"),
        ):
            await _start_project_creation(MagicMock(), self._nieuw_project({ACL_KEY_PREFIX_FIELD: True}))

        geschreven = load_yaml_from_string(created["yaml_content"]) or {}
        assert APPROVAL_FIELD not in geschreven["services"][0]["config"]
