"""Een samengestelde naam van twee projecten mag niet dezelfde zijn (RC-361).

Een database, bucket, MinIO-gebruiker en Keycloak-client worden benoemd door de
projectnaam en de deploymentnaam aan elkaar te plakken, dus project ``amt`` met
deployment ``odc-prd-staging`` levert exact dezelfde namen op als project
``amt-odc-prd`` met deployment ``staging``. Provisioning behandelt een bestaande
resource met die naam als de zijne: ``_create_or_update_user`` ziet ``exists``, zet het
wachtwoord om en geeft de database van het andere project weg.

Twee lagen houden dat tegen, en beide staan hier:

* de schrijfguard in ``validate_project_structure``, het knooppunt dat elke schrijfweg
  passeert (wizard, API v1 en v2, en een hand-geschreven bestand via
  ``GitProjectStore._validate``);
* de provisioningguard in ``database_manager`` en ``minio_manager``, voor een
  projectbestand dat de schrijfguard nooit zag.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.clone_validation import composite_name_collision, other_project_deployments
from opi.manager.database_manager import DatabaseManager
from opi.manager.minio_manager import MinioManager
from opi.manager.project_validation import validate_project_structure
from opi.services.project_service import get_project_service
from opi.services.project_store import GitProjectStore

# Het paar dat de opdrachtgever natelde: beide spellingen bestaan los van elkaar en
# leveren samen dezelfde databasenaam, databasegebruiker en bucketnaam op.
EIGEN_PROJECT = "amt"
EIGEN_DEPLOYMENT = "odc-prd-staging"
ANDER_PROJECT = "amt-odc-prd"
ANDER_DEPLOYMENT = "staging"


def _project(name: str, deployments: list[str]) -> dict[str, Any]:
    """Een projectbestand dat schema- en structuurvalidatie haalt."""
    return {
        "schema-version": 2,
        "name": name,
        "display-name": name,
        "description": "test project",
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["odcn-production"],
        "services": [],
        "components": [],
        "deployments": [
            {"name": dep, "cluster": "odcn-production", "namespace": name, "components": []} for dep in deployments
        ],
        "config": {"age-public-key": "age1nepnepnep"},
    }


@pytest.fixture
def ander_project_in_de_store():
    """Registreer het andere project in de projectcache en ruim het daarna op."""
    service = get_project_service()
    service.register(
        ANDER_PROJECT,
        api_key="ander",
        filename=f"{ANDER_PROJECT}.yaml",
        data=_project(ANDER_PROJECT, [ANDER_DEPLOYMENT]),
    )
    yield
    service.remove_project(ANDER_PROJECT)


# ---------------------------------------------------------------------------
# De beslissing zelf
# ---------------------------------------------------------------------------


class TestDeBeslissing:
    def test_het_paar_van_de_opdrachtgever_botst(self):
        refusal = composite_name_collision(EIGEN_PROJECT, EIGEN_DEPLOYMENT, {ANDER_PROJECT: [ANDER_DEPLOYMENT]})

        assert refusal is not None
        assert "amt_odc_prd_staging" in refusal
        assert ANDER_PROJECT in refusal

    def test_de_omgekeerde_vorm_botst_ook(self):
        """Redis plakt ``{deployment}-{project}``, dus daar botst een ander paar."""
        refusal = composite_name_collision("staging-amt", "odc-prd", {"amt": ["odc-prd-staging"]})

        assert refusal is not None
        assert "odc-prd-staging-amt" in refusal

    def test_een_eigen_naam_is_vrij(self):
        assert composite_name_collision("amt", "staging", {ANDER_PROJECT: [ANDER_DEPLOYMENT]}) is None

    def test_zonder_andere_projecten_botst_niets(self):
        assert composite_name_collision(EIGEN_PROJECT, EIGEN_DEPLOYMENT, {}) is None

    def test_de_cache_levert_de_andere_projecten(self, ander_project_in_de_store):
        others = other_project_deployments(EIGEN_PROJECT)

        assert others[ANDER_PROJECT] == [ANDER_DEPLOYMENT]
        assert EIGEN_PROJECT not in others


# ---------------------------------------------------------------------------
# De schrijfguard: het knooppunt dat elke schrijfweg passeert
# ---------------------------------------------------------------------------


class TestSchrijfguard:
    async def test_een_nieuw_project_met_het_botsende_paar_wordt_geweigerd(self, ander_project_in_de_store):
        with pytest.raises(ProjectIntegrityError) as excinfo:
            await validate_project_structure(_project(EIGEN_PROJECT, [EIGEN_DEPLOYMENT]))

        assert "amt_odc_prd_staging" in str(excinfo.value)

    async def test_een_nieuwe_deployment_met_het_botsende_paar_wordt_geweigerd(self, ander_project_in_de_store):
        """Hernoemen en toevoegen komen hier op hetzelfde uit: het paar is nieuw."""
        vorige = _project(EIGEN_PROJECT, ["acceptatie"])

        with pytest.raises(ProjectIntegrityError):
            await validate_project_structure(_project(EIGEN_PROJECT, ["acceptatie", EIGEN_DEPLOYMENT]), previous=vorige)

    async def test_een_al_opgeslagen_paar_blijft_opslaan(self, ander_project_in_de_store):
        """Een botsing van voor deze guard mag geen van beide projecten vastzetten."""
        botsend = _project(EIGEN_PROJECT, [EIGEN_DEPLOYMENT])

        await validate_project_structure(botsend, previous=botsend)

    async def test_een_vrije_naam_passeert(self, ander_project_in_de_store):
        await validate_project_structure(_project(EIGEN_PROJECT, ["staging"]))

    async def test_upsert_deployment_geeft_success_false(self, ander_project_in_de_store, tmp_path):
        """De weg van de API: ``:upsert-deployment`` schrijft via ``save_and_commit_project``.

        De store is hier een stand-in die alleen de echte ``_validate`` draait, zodat de
        toets op de keten zit (upsert -> schrijfpad -> validatie) en niet op de guard zelf.
        """
        store = GitProjectStore(working_dir=str(tmp_path))
        project_data = _project(EIGEN_PROJECT, ["acceptatie"])
        project_data["repositories"] = [{"name": "main-repo", "url": "https://example.com/app.git"}]
        project_data["components"] = [{"name": "web", "type": "single"}]

        with (
            patch("opi.manager.project_manager.KubectlConnector"),
            patch("opi.handlers.sops.SopsHandler"),
            patch("opi.generation.manifests.ManifestGenerator"),
            patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
            patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
            patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
            patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
            patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
            patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
            patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
        ):
            from opi.manager.project_manager import ProjectManager

            manager = ProjectManager()

        manager.get_contents = AsyncMock(return_value=project_data)
        manager.get_name = AsyncMock(return_value=EIGEN_PROJECT)
        manager.get_deployments = AsyncMock(return_value=project_data["deployments"])
        manager._validate_component_references = MagicMock(return_value={"success": True, "error": None})

        async def alleen_valideren(name, data, **kwargs):
            await store._validate(data, enforce=kwargs.get("enforce_validation", True))

        schrijver = MagicMock()
        schrijver.save = AsyncMock(side_effect=alleen_valideren)

        with (
            patch("opi.manager.project_manager.get_project_store", return_value=schrijver),
            patch("opi.manager.project_manager.ensure_domain_requests"),
        ):
            result = await manager.upsert_deployment(
                deployment_name=EIGEN_DEPLOYMENT,
                components=[SimpleNamespace(reference="web", image="nginx:1")],
            )

        assert result["success"] is False
        assert "amt_odc_prd_staging" in result["error"]

    async def test_de_git_weg_weigert_hem_ook(self, ander_project_in_de_store, tmp_path):
        """``GitProjectStore._validate`` draait voor elke commit, ook voor een hand-edit."""
        store = GitProjectStore(working_dir=str(tmp_path))

        with pytest.raises(ProjectIntegrityError):
            await store._validate(_project(EIGEN_PROJECT, [EIGEN_DEPLOYMENT]), enforce=True)


# ---------------------------------------------------------------------------
# De provisioningguard: de tweede laag
# ---------------------------------------------------------------------------


class TestProvisioningguard:
    async def test_de_databasestap_zet_geen_wachtwoord_van_een_vreemde_rol(self, ander_project_in_de_store):
        postgres = MagicMock()
        postgres.create_user = AsyncMock(return_value={"status": "exists"})
        postgres.update_user_password = AsyncMock()

        project_manager = MagicMock()
        project_manager.get_name = AsyncMock(return_value=EIGEN_PROJECT)
        project_manager.get_progress_manager.return_value = None
        db_manager = DatabaseManager(
            project_manager, db_host="localhost", admin_username="postgres", admin_password="postgres"
        )
        db_manager._postgres_connector = postgres

        project_data = _project(EIGEN_PROJECT, [EIGEN_DEPLOYMENT])
        deployment = project_data["deployments"][0]

        with (
            patch.object(DatabaseManager, "_deployment_uses_postgresql", AsyncMock(return_value=True)),
            pytest.raises(ValueError, match="amt_odc_prd_staging"),
        ):
            await db_manager.create_resources_for_deployment(project_data, deployment)

        postgres.create_user.assert_not_awaited()
        postgres.update_user_password.assert_not_awaited()

    async def test_de_bucketstap_zet_geen_secret_key_van_een_vreemde_gebruiker(self, ander_project_in_de_store):
        connector = MagicMock()
        connector.configure_alias = AsyncMock(return_value=True)
        connector.create_user = AsyncMock(return_value={"status": "exists"})

        project_manager = MagicMock()
        project_manager.get_name = AsyncMock(return_value=EIGEN_PROJECT)
        project_manager.get_progress_manager.return_value = None
        minio_manager = MinioManager(project_manager)

        project_data = _project(EIGEN_PROJECT, [EIGEN_DEPLOYMENT])
        deployment = project_data["deployments"][0]

        with (
            patch("opi.manager.minio_manager.create_minio_connector", lambda: connector),
            patch.object(MinioManager, "_deployment_uses_minio", AsyncMock(return_value=True)),
            pytest.raises(ValueError, match="amt_odc_prd_staging"),
        ):
            await minio_manager.create_resources_for_deployment(project_data, deployment)

        connector.create_user.assert_not_awaited()


# ---------------------------------------------------------------------------
# De negatieve kant op een echte populatie
# ---------------------------------------------------------------------------

#: De productieprojecten staan in een checkout buiten deze repo. Zet
#: ``ZAD_PROJECTS_AUDIT_DIR`` om een andere verzameling te meten.
_AUDIT_DIR = Path(
    os.environ.get(
        "ZAD_PROJECTS_AUDIT_DIR",
        Path.home() / "IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github/projects",
    )
)


def test_geen_bestaand_project_wordt_door_de_guard_geweigerd():
    """Alleen lezen: elk paar uit de populatie tegen elk ander paar.

    De guard mag geen enkele combinatie weigeren die vandaag al bestaat, anders zet
    hij bij de eerste save een bestaand project vast.
    """
    if not _AUDIT_DIR.is_dir():
        pytest.skip(f"geen projectpopulatie op {_AUDIT_DIR} (zet ZAD_PROJECTS_AUDIT_DIR)")

    paren: dict[str, list[str]] = {}
    for path in sorted(_AUDIT_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        name = data.get("name")
        if not isinstance(name, str):
            continue
        paren[name] = [
            dep["name"]
            for dep in (data.get("deployments") or [])
            if isinstance(dep, dict) and isinstance(dep.get("name"), str)
        ]

    assert paren, f"geen projectbestanden gevonden in {_AUDIT_DIR}"

    botsingen = [
        refusal
        for project_name, deployments in paren.items()
        for deployment_name in deployments
        if (
            refusal := composite_name_collision(
                project_name, deployment_name, {k: v for k, v in paren.items() if k != project_name}
            )
        )
    ]

    assert botsingen == [], f"{len(botsingen)} botsing(en) in de bestaande populatie: {botsingen[:3]}"
