"""Een restore naar een nieuwe deployment is een clone-from met een backup als databron.

RC-417. Wat een nieuwe deployment van een bestaande overneemt is één afleiding
(``derive_deployment_from_source``), die de upsert met ``cloneFrom`` en de restore samen
gebruiken, en één bouwer (``build_restore_target_deployment``) die daar de wizardkeuzes en
de ``clone-from`` naar de backup-run op zet.

De tweede helft van dit bestand (``TestRequireCloneSourceAccess`` en verder) toetst de
rechten: ``RESTORE_RUN`` op de bron en het doel, ``DEPLOYMENT_CLONE_FROM`` op de bron van
elke nieuwe of gewijzigde ``clone-from``, en de step-up-dialoog als de restore een project
voor het eerst op productie zet.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.manager.clone_validation import (
    build_restore_target_deployment,
    derive_deployment_from_source,
    new_or_changed_clone_sources,
)
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.authz import require_clone_source_access
from opi.web.router_detail_edit import _authorize_restore, _process_and_save_modal_edit

AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nY2lwaGVydGV4dA==\n-----END AGE ENCRYPTED FILE-----"


def _source() -> dict[str, Any]:
    return {
        "name": "productie",
        "cluster": "odcn-production",
        "namespace": "demo",
        "repository": "main-repo",
        "environment": "productie",
        "backup": {"schedule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"},
        "sleep": {"state": "asleep"},
        "services": [
            {
                "reference": "publish-on-web",
                "config": {
                    "subdomain": "productie",
                    "base-domain": "rijksapps.nl",
                    "domain-format": "deployment-project",
                },
            },
            "namespace-postgresql-database",
        ],
        "components": [
            {"reference": "api", "image": "ghcr.io/demo/api:1.2", "user-env-vars": AGE_BLOCK},
            {"reference": "web", "image": "ghcr.io/demo/web:1.2"},
        ],
    }


def _project() -> dict[str, Any]:
    return {
        "name": "demo",
        "clusters": ["odcn-production"],
        "repositories": [{"name": "main-repo"}],
        "deployments": [_source()],
    }


class TestDeriveDeploymentFromSource:
    def test_the_backup_block_and_the_runtime_state_stay_behind(self) -> None:
        derived = derive_deployment_from_source(_source(), "preview")
        assert "backup" not in derived
        assert "sleep" not in derived
        assert "name" not in derived

    def test_the_other_configuration_travels(self) -> None:
        derived = derive_deployment_from_source(_source(), "preview")
        assert derived["cluster"] == "odcn-production"
        assert derived["repository"] == "main-repo"
        assert derived["environment"] == "productie"
        assert "namespace-postgresql-database" in derived["services"]

    def test_the_hostnames_stay_behind_but_the_shape_does_not(self) -> None:
        derived = derive_deployment_from_source(_source(), "preview")
        assert get_domain_setting(derived, DomainSetting.BASE_DOMAIN) is None
        assert get_domain_setting(derived, DomainSetting.DOMAIN_FORMAT) == "deployment-project"

    def test_a_subdomain_equal_to_the_source_name_becomes_the_new_name(self) -> None:
        derived = derive_deployment_from_source(_source(), "preview")
        assert get_domain_setting(derived, DomainSetting.SUBDOMAIN) == "preview"

    def test_a_requested_web_address_beats_the_source(self) -> None:
        derived = derive_deployment_from_source(
            _source(), "preview", {DomainSetting.SUBDOMAIN: "pr-857", DomainSetting.BASE_DOMAIN: "sandbox.dev"}
        )
        assert get_domain_setting(derived, DomainSetting.BASE_DOMAIN) == "sandbox.dev"
        # Onveranderd gedrag van de upsert: de heuristiek "subdomein gelijk aan de naam van
        # de bron" loopt na het gevraagde subdomein, en gaat er dus over heen. De restore
        # zet zijn eigen keuze er daarna weer op, zie TestBuildRestoreTargetDeployment.
        assert get_domain_setting(derived, DomainSetting.SUBDOMAIN) == "preview"

    def test_without_components_the_images_come_from_the_caller(self) -> None:
        """De upsert zet de componenten zelf, uit zijn request."""
        assert "components" not in derive_deployment_from_source(_source(), "preview")

    def test_with_components_the_image_and_the_env_vars_travel(self) -> None:
        derived = derive_deployment_from_source(_source(), "preview", include_components=True)
        assert derived["components"] == [
            {"reference": "api", "image": "ghcr.io/demo/api:1.2", "user-env-vars": AGE_BLOCK},
            {"reference": "web", "image": "ghcr.io/demo/web:1.2"},
        ]

    def test_the_source_is_not_mutated(self) -> None:
        source = _source()
        derive_deployment_from_source(source, "preview", include_components=True)["components"][0]["image"] = "x"
        assert source["components"][0]["image"] == "ghcr.io/demo/api:1.2"
        assert get_domain_setting(source, DomainSetting.BASE_DOMAIN) == "rijksapps.nl"


class TestBuildRestoreTargetDeployment:
    def _build(self, **overrides: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "target_deployment": "herstel",
            "backup_source": "productie",
            "backup_run_id": "20261103020000",
            "config_source": "productie",
            "wizard_config": {"name": "herstel", "components": [{"reference": "api", "image": "ghcr.io/demo/api:1.2"}]},
        }
        kwargs.update(overrides)
        return build_restore_target_deployment(_project(), **kwargs)

    def test_the_clone_from_names_the_backup_run(self) -> None:
        assert self._build()["clone-from"] == {
            "type": "backup",
            "reference": "productie",
            "mode": "once",
            "run": "20261103020000",
        }

    def test_the_wizard_owns_the_components_and_the_source_their_env_vars(self) -> None:
        built = self._build()
        assert built["components"] == [
            {"reference": "api", "image": "ghcr.io/demo/api:1.2", "user-env-vars": AGE_BLOCK}
        ]

    def test_an_edited_image_wins_over_the_source(self) -> None:
        built = self._build(
            wizard_config={"name": "herstel", "components": [{"reference": "api", "image": "ghcr.io/demo/api:9.9"}]}
        )
        assert built["components"][0]["image"] == "ghcr.io/demo/api:9.9"
        assert built["components"][0]["user-env-vars"] == AGE_BLOCK

    def test_without_a_config_source_nothing_is_inherited(self) -> None:
        built = self._build(config_source=None)
        assert built["components"] == [{"reference": "api", "image": "ghcr.io/demo/api:1.2"}]
        assert "namespace-postgresql-database" not in str(built.get("services", ""))

    def test_the_system_fields_fall_back_to_the_backups_deployment(self) -> None:
        built = self._build(config_source=None)
        assert built["cluster"] == "odcn-production"
        assert built["namespace"] == "demo"
        assert built["repository"] == "main-repo"

    def test_the_wizards_web_address_beats_the_source(self) -> None:
        wizard = {"name": "herstel", "components": []}
        from opi.services.catalog.publish_on_web.domain_config import set_domain_setting

        set_domain_setting(wizard, DomainSetting.SUBDOMAIN, "herstel-2")
        built = self._build(wizard_config=wizard)
        assert get_domain_setting(built, DomainSetting.SUBDOMAIN) == "herstel-2"
        assert get_domain_setting(built, DomainSetting.BASE_DOMAIN) is None


class TestNewOrChangedCloneSources:
    def test_an_added_clone_from_names_its_source(self) -> None:
        before = {"deployments": [{"name": "productie"}]}
        after = {
            "deployments": [
                {"name": "productie"},
                {"name": "preview", "clone-from": {"type": "deployment", "reference": "productie", "mode": "once"}},
            ]
        }
        assert new_or_changed_clone_sources(before, after) == ["productie"]

    def test_a_backup_clone_from_names_its_source_too(self) -> None:
        before = {"deployments": [{"name": "productie"}]}
        after = {
            "deployments": [
                {"name": "productie"},
                {"name": "herstel", "clone-from": {"type": "backup", "reference": "productie", "run": "1"}},
            ]
        }
        assert new_or_changed_clone_sources(before, after) == ["productie"]

    def test_an_unchanged_clone_from_weighs_nothing(self) -> None:
        kloon = {"type": "deployment", "reference": "productie", "mode": "once"}
        project = {"deployments": [{"name": "productie"}, {"name": "preview", "clone-from": kloon}]}
        assert new_or_changed_clone_sources(project, project) == []

    def test_a_changed_reference_weighs_the_new_source(self) -> None:
        before = {
            "deployments": [
                {"name": "preview", "clone-from": {"type": "deployment", "reference": "acceptatie", "mode": "once"}}
            ]
        }
        after = {
            "deployments": [
                {"name": "preview", "clone-from": {"type": "deployment", "reference": "productie", "mode": "once"}}
            ]
        }
        assert new_or_changed_clone_sources(before, after) == ["productie"]

    def test_a_source_that_is_not_a_deployment_weighs_nothing_here(self) -> None:
        after = {
            "deployments": [
                {"name": "preview", "clone-from": {"type": "remote-source", "reference": "extern", "mode": "once"}}
            ]
        }
        assert new_or_changed_clone_sources({"deployments": []}, after) == []


# ---------------------------------------------------------------------------
# Rechten (ontwerp paragraaf "Rechten (besloten)" en plan punt 4 en 5)
#
# De bron van een clone-from is altijd ops (ongeacht waarnaartoe), en RESTORE_RUN weegt
# sinds RC-417 op de bron EN het doel in plaats van op de hoogste markering van het
# project. ``_project`` en ``_request`` bouwen de vaste fixture voor alle gevallen
# hieronder: een productiedeployment, een niet-productiedeployment, een developer en een
# ops-lid (via de legacy-rol "owner", die ADMIN + OPS opent).
# ---------------------------------------------------------------------------

PROJECT_NAME = "restoreproject"
DEVELOPER_EMAIL = "developer@example.com"
OPS_EMAIL = "ops@example.com"


def _rights_project(deployments: list[dict[str, Any]], *, name: str = PROJECT_NAME) -> ProjectSummary:
    return ProjectSummary(
        name=name,
        filename=f"{name}.yaml",
        data={
            "name": name,
            "clusters": ["odcn-production"],
            "repositories": [{"name": "main-repo"}],
            "deployments": deployments,
        },
        users=[
            ProjectUser(email=OPS_EMAIL, roles=["admin", "ops"]),
            ProjectUser(email=DEVELOPER_EMAIL, roles=["developer"]),
        ],
    )


def _rights_request(email: str, *, verse_step_up: bool = False) -> Any:
    sessie: dict[str, Any] = {}
    if verse_step_up:
        sessie["step_up_at"] = datetime.now(UTC).isoformat()
    return SimpleNamespace(state=SimpleNamespace(user={"email": email}), session=sessie)


class TestRequireCloneSourceAccess:
    """``require_clone_source_access`` weegt DEPLOYMENT_CLONE_FROM op de bron."""

    def _project(self) -> ProjectSummary:
        return _rights_project(
            [
                {"name": "productie", "environment": "productie"},
                {"name": "staging", "environment": "acceptatie"},
            ]
        )

    def _after_with_clone_from(self, source: str) -> dict[str, Any]:
        return {
            "name": PROJECT_NAME,
            "deployments": [
                {"name": "productie", "environment": "productie"},
                {"name": "staging", "environment": "acceptatie"},
                {"name": "preview", "clone-from": {"type": "deployment", "reference": source, "mode": "once"}},
            ],
        }

    def test_developer_wordt_geweigerd_bij_een_productiebron(self) -> None:
        project = self._project()
        with (
            patch("opi.web.authz.get_project_store", return_value=SimpleNamespace(get=lambda _n: project)),
            patch("opi.web.authz.get_user_service", return_value=SimpleNamespace(is_platform_admin=lambda _e: False)),
            pytest.raises(HTTPException) as exc,
        ):
            require_clone_source_access(
                _rights_request(DEVELOPER_EMAIL),
                PROJECT_NAME,
                project.data,
                self._after_with_clone_from("productie"),
            )
        assert exc.value.status_code == 403

    def test_developer_mag_klonen_van_niet_productie(self) -> None:
        project = self._project()
        with (
            patch("opi.web.authz.get_project_store", return_value=SimpleNamespace(get=lambda _n: project)),
            patch("opi.web.authz.get_user_service", return_value=SimpleNamespace(is_platform_admin=lambda _e: False)),
        ):
            require_clone_source_access(
                _rights_request(DEVELOPER_EMAIL),
                PROJECT_NAME,
                project.data,
                self._after_with_clone_from("staging"),
            )

    def test_ops_mag_klonen_van_productie(self) -> None:
        project = self._project()
        with (
            patch("opi.web.authz.get_project_store", return_value=SimpleNamespace(get=lambda _n: project)),
            patch("opi.web.authz.get_user_service", return_value=SimpleNamespace(is_platform_admin=lambda _e: False)),
        ):
            require_clone_source_access(
                _rights_request(OPS_EMAIL),
                PROJECT_NAME,
                project.data,
                self._after_with_clone_from("productie"),
            )

    def test_een_onveranderde_clone_from_weegt_niets(self) -> None:
        """Geen nieuwe of gewijzigde ``clone-from``: geen weging, dus geen weigering."""
        project = self._project()
        after = self._after_with_clone_from("productie")
        with patch(
            "opi.web.authz.get_project_store",
            side_effect=AssertionError("mag niet gelezen worden zonder iets om te wegen"),
        ):
            require_clone_source_access(_rights_request(DEVELOPER_EMAIL), PROJECT_NAME, after, after)


class TestAuthorizeRestore:
    """``_authorize_restore`` weegt RESTORE_RUN op de bron en het doel (plan punt 4)."""

    async def _run(
        self,
        email: str,
        *,
        deployments: list[dict[str, Any]],
        source_deployment: str,
        target_deployment: str,
        deployment_config: dict[str, Any] | None,
        config_source: str | None = None,
        verse_step_up: bool = False,
    ) -> Any:
        project = _rights_project(deployments)
        with (
            patch("opi.web.router_detail_edit.get_project_store", return_value=SimpleNamespace(get=lambda _n: project)),
            patch("opi.web.authz.get_project_store", return_value=SimpleNamespace(get=lambda _n: project)),
            patch("opi.web.authz.get_user_service", return_value=SimpleNamespace(is_platform_admin=lambda _e: False)),
            patch("opi.web.router_detail_edit.render_step_up_dialog", new=AsyncMock(return_value="<dialoog>")),
        ):
            return await _authorize_restore(
                _rights_request(email, verse_step_up=verse_step_up),
                PROJECT_NAME,
                "tok",
                source_deployment=source_deployment,
                target_deployment=target_deployment,
                deployment_config=deployment_config,
                config_source=config_source,
                backup_run_id="20261103020000",
            )

    @pytest.mark.asyncio
    async def test_developer_geweigerd_als_de_bron_productie_is(self) -> None:
        deployments = [
            {"name": "productie", "environment": "productie"},
            {"name": "staging", "environment": "acceptatie"},
        ]
        with pytest.raises(HTTPException) as exc:
            await self._run(
                DEVELOPER_EMAIL,
                deployments=deployments,
                source_deployment="productie",
                target_deployment="staging",
                deployment_config=None,
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_developer_geweigerd_als_het_doel_productie_wordt(self) -> None:
        """De bron is niet-productie, maar de NIEUWE deployment krijgt ``environment:
        productie`` uit de wizard: het doel weegt op de NA-staat, niet op de store.

        Het project heeft al een productiedeployment, zodat de markering van het PROJECT
        niet verandert (``ENVIRONMENT_SET`` wordt dus niet apart geraakt) en de weigering
        hier echt van de weging op het DOEL zelf komt."""
        deployments = [
            {"name": "productie", "environment": "productie"},
            {"name": "staging", "environment": "acceptatie"},
        ]
        with pytest.raises(HTTPException) as exc:
            await self._run(
                DEVELOPER_EMAIL,
                deployments=deployments,
                source_deployment="staging",
                target_deployment="nieuw-prod",
                deployment_config={
                    "name": "nieuw-prod",
                    "environment": "productie",
                    "components": [{"reference": "api", "image": "nginx:1.25"}],
                },
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_developer_geweigerd_bij_terugzetten_op_een_bestaande_productiedeployment(self) -> None:
        """Terugzetten op een BESTAANDE deployment (geen nieuwe) verandert de markering
        niet, dus dit is de enige plek die het doel zelf weegt: een backup van
        'staging' (niet-productie) teruggezet op de bestaande 'productie'-deployment."""
        deployments = [
            {"name": "productie", "environment": "productie"},
            {"name": "staging", "environment": "acceptatie"},
        ]
        with pytest.raises(HTTPException) as exc:
            await self._run(
                DEVELOPER_EMAIL,
                deployments=deployments,
                source_deployment="staging",
                target_deployment="productie",
                deployment_config=None,
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_developer_geweigerd_als_de_config_bron_productie_is(self) -> None:
        """De backup komt van een niet-productiedeployment, maar de config wordt
        overgenomen van een ANDERE, productiedeployment: die weegt apart.

        Het doel staat expliciet op ``ontwikkeling``. Zonder die markering erft het de
        ``productie`` van de config-bron en komt de weigering van de weging op het DOEL,
        terwijl de config-bron zelf ongewogen blijft: dan nam een developer de env-vars
        van productie mee naar een deployment die hij wel mag beheren (RC-417
        securityreview)."""
        deployments = [
            {"name": "productie", "environment": "productie"},
            {"name": "staging", "environment": "acceptatie"},
        ]
        with pytest.raises(HTTPException) as exc:
            await self._run(
                DEVELOPER_EMAIL,
                deployments=deployments,
                source_deployment="staging",
                target_deployment="preview",
                config_source="productie",
                deployment_config={"name": "preview", "environment": "ontwikkeling", "components": []},
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_ops_mag_de_config_van_productie_overnemen(self) -> None:
        """De tegenproef op de weging hierboven: de poort weigert de config-bron op de
        ROL, en niet elke restore met een config-bron."""
        deployments = [
            {"name": "productie", "environment": "productie"},
            {"name": "staging", "environment": "acceptatie"},
        ]
        uitkomst = await self._run(
            OPS_EMAIL,
            deployments=deployments,
            source_deployment="staging",
            target_deployment="preview",
            config_source="productie",
            deployment_config={"name": "preview", "environment": "ontwikkeling", "components": []},
            verse_step_up=True,
        )
        assert uitkomst is None

    @pytest.mark.asyncio
    async def test_developer_mag_tussen_niet_productie_deployments_terugzetten(self) -> None:
        """Besloten punt 3: binnen een project MET productie mag een developer tussen
        twee niet-productiedeployments terugzetten."""
        deployments = [
            {"name": "productie", "environment": "productie"},
            {"name": "staging", "environment": "acceptatie"},
        ]
        uitkomst = await self._run(
            DEVELOPER_EMAIL,
            deployments=deployments,
            source_deployment="staging",
            target_deployment="preview",
            config_source="staging",
            deployment_config={"name": "preview", "components": []},
        )
        assert uitkomst is None

    @pytest.mark.asyncio
    async def test_ops_zonder_step_up_krijgt_de_dialoog_bij_de_eerste_productiedeployment(self) -> None:
        """Het project heeft nog geen productie; deze restore maakt het doel de eerste.
        Dat is de markering zetten, en zonder verse step-up krijgt ops de dialoog."""
        deployments = [{"name": "staging", "environment": "acceptatie"}]
        uitkomst = await self._run(
            OPS_EMAIL,
            deployments=deployments,
            source_deployment="staging",
            target_deployment="eerste-productie",
            deployment_config={
                "name": "eerste-productie",
                "environment": "productie",
                "components": [{"reference": "api", "image": "nginx:1.25"}],
            },
        )
        assert uitkomst is not None
        assert getattr(uitkomst, "status_code", None) == 200

    @pytest.mark.asyncio
    async def test_ops_met_verse_step_up_mag_door(self) -> None:
        deployments = [{"name": "staging", "environment": "acceptatie"}]
        uitkomst = await self._run(
            OPS_EMAIL,
            deployments=deployments,
            source_deployment="staging",
            target_deployment="eerste-productie",
            deployment_config={
                "name": "eerste-productie",
                "environment": "productie",
                "components": [{"reference": "api", "image": "nginx:1.25"}],
            },
            verse_step_up=True,
        )
        assert uitkomst is None


class TestDeploymentToevoegenMetCloneFromViaDeModalOpslag:
    """Dezelfde poort op de andere schrijfweg: een deployment toevoegen met clone-from
    (geen restore) gaat via ``_process_and_save_modal_edit`` -> ``require_clone_source_access``."""

    def _voor(self) -> dict[str, Any]:
        return {
            "name": PROJECT_NAME,
            "deployments": [
                {"name": "productie", "environment": "productie"},
                {"name": "staging", "environment": "acceptatie"},
            ],
        }

    def _na_met_clone_from(self, source: str) -> dict[str, Any]:
        na = copy.deepcopy(self._voor())
        na["deployments"].append(
            {
                "name": "preview",
                "environment": "acceptatie",
                "clone-from": {"type": "deployment", "reference": source, "mode": "once"},
            }
        )
        return na

    async def _opslaan(self, email: str, *, bron: str) -> MagicMock:
        voor = self._voor()
        na = self._na_met_clone_from(bron)
        project = ProjectSummary(
            name=PROJECT_NAME,
            filename=f"{PROJECT_NAME}.yaml",
            data=voor,
            users=[
                ProjectUser(email=OPS_EMAIL, roles=["admin", "ops"]),
                ProjectUser(email=DEVELOPER_EMAIL, roles=["developer"]),
            ],
        )
        manager = MagicMock()
        manager.get_contents = AsyncMock(return_value=copy.deepcopy(voor))
        manager.save_and_commit_project = AsyncMock(return_value=None)
        store = SimpleNamespace(get=lambda _n: project)
        flow = SimpleNamespace(flow_id="modal-edit-deployment", sections=[])
        with (
            patch("opi.web.authz.get_project_store", return_value=store),
            patch("opi.web.authz.get_user_service", return_value=SimpleNamespace(is_platform_admin=lambda _e: False)),
            patch("opi.web.router_detail_edit.apply_modal_edit", new=AsyncMock(return_value=copy.deepcopy(na))),
            patch("opi.web.router_detail_edit.ensure_approval_requests"),
        ):
            await _process_and_save_modal_edit(_rights_request(email), manager, PROJECT_NAME, flow, None, {}, [], None)
        return manager

    @pytest.mark.asyncio
    async def test_developer_geweigerd_bij_clone_from_vanaf_productie(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await self._opslaan(DEVELOPER_EMAIL, bron="productie")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_developer_mag_klonen_vanaf_niet_productie(self) -> None:
        manager = await self._opslaan(DEVELOPER_EMAIL, bron="staging")
        manager.save_and_commit_project.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ops_mag_klonen_vanaf_productie(self) -> None:
        manager = await self._opslaan(OPS_EMAIL, bron="productie")
        manager.save_and_commit_project.assert_awaited_once()
