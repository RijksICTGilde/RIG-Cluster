"""``repositories[].path`` blijft binnen de repo: geweigerd bij de poort, geankerd bij de schrijver."""

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.project_schema import (
    SCHEMA_PATH,
    ProjectSchemaError,
    validate_declared_project_schema,
    validate_project_schema,
)
from opi.forms.models.project_file import REPOSITORY_PATH_PATTERN, RepositoryModel
from opi.manager.argo_manager import ArgoManager
from opi.manager.project_manager import ProjectManager
from opi.manager.pvc_manager import PVCManager
from opi.utils.naming import (
    RepositoryPathError,
    generate_deployment_manifest_path,
    generate_infrastructure_manifest_path,
    generate_project_level_manifest_path,
)
from pydantic import ValidationError
from ruamel.yaml import YAML

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

EXAMPLE_PROJECT = Path(__file__).resolve().parents[3] / "projects" / "simple-example.yaml"

GEWEIGERD = ["../../../../tmp/uit-de-repo", "/etc", "map\\..\\..", "..", "sub/../..", "a..b", "map\n"]
TOEGESTAAN = ["", ".", "sub/map", "./sub", "sub/map/", ".verborgen", "v1.2"]

#: Uitkomsten die de repo verlaten. Geen backslash: die is op posix een gewoon teken.
ONTSNAPT = ["../../../../tmp/uit-de-repo", "/etc", "..", "../..", "sub/../../..", "./../x", "//etc"]

PAD_FUNCTIES = [
    lambda repo_path: generate_project_level_manifest_path("odcn-production", "demo", repo_path),
    lambda repo_path: generate_infrastructure_manifest_path("odcn-production", "demo", repo_path),
    lambda repo_path: generate_deployment_manifest_path("odcn-production", "demo", "deployment-1", repo_path),
]


def _project(path: str) -> dict:
    return {
        "schema-version": 2.9,
        "name": "demo",
        "clusters": ["odcn-production"],
        "users": [{"email": "admin@rijksoverheid.nl", "role": "admin"}],
        "repositories": [{"name": "main-repo", "url": "https://example.org/demo.git", "branch": "main", "path": path}],
    }


class TestHetSchemaWeigertBijDePoort:
    @pytest.mark.parametrize("path", GEWEIGERD)
    def test_weigert(self, path: str) -> None:
        with pytest.raises(ProjectSchemaError) as exc:
            validate_project_schema(_project(path))
        assert exc.value.field_path == "repositories/0/path"

    def test_de_melding_is_leesbaar(self) -> None:
        with pytest.raises(ProjectSchemaError) as exc:
            validate_project_schema(_project("../../../../tmp/uit-de-repo"))
        assert "Veld 'repositories/0/path'" in str(exc.value)
        assert "zonder '..'" in str(exc.value)

    def test_een_patroonveld_zonder_beschrijving_houdt_de_kale_melding(self) -> None:
        project = _project(".")
        project["name"] = "Demo"
        with pytest.raises(ProjectSchemaError) as exc:
            validate_project_schema(project)
        pattern = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["properties"]["name"]["pattern"]
        assert str(exc.value).endswith(f"does not match {pattern!r}")

    @pytest.mark.parametrize("path", TOEGESTAAN)
    def test_laat_door(self, path: str) -> None:
        validate_project_schema(_project(path))

    def test_ook_een_ouder_bestand_uit_git_wordt_geweigerd(self) -> None:
        """De git-monitor valideert tegen de gedeclareerde versie; de regel geldt daar ook."""
        project = _project("../buiten")
        project["schema-version"] = 2.8
        with pytest.raises(ProjectSchemaError) as exc:
            validate_declared_project_schema(project)
        assert exc.value.field_path == "repositories/0/path"

    def test_een_bestaand_projectbestand_met_punt_valideert_ongewijzigd(self) -> None:
        with EXAMPLE_PROJECT.open(encoding="utf-8") as project_file:
            project_data = YAML(typ="safe").load(project_file)
        assert {repo.get("path") for repo in project_data["repositories"]} >= {".", "infra"}
        before = copy.deepcopy(project_data)
        validate_project_schema(project_data)
        assert project_data == before


class TestHetFormuliermodelVolgtHetzelfdePatroon:
    @pytest.mark.parametrize("path", GEWEIGERD)
    def test_weigert(self, path: str) -> None:
        with pytest.raises(ValidationError):
            RepositoryModel(name="main-repo", url="https://example.org/demo.git", path=path)

    @pytest.mark.parametrize("path", TOEGESTAAN)
    def test_laat_door(self, path: str) -> None:
        assert RepositoryModel(name="main-repo", url="https://example.org/demo.git", path=path).path == path

    def test_de_standaard_is_geldig(self) -> None:
        assert RepositoryModel(name="main-repo", url="https://example.org/demo.git").path == "."

    def test_het_schema_draagt_hetzelfde_patroon(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        assert schema["$defs"]["repository"]["properties"]["path"]["pattern"] == REPOSITORY_PATH_PATTERN


class TestDePadfunctiesAnkerenInDeRepo:
    """Rechtstreeks, want bestaande projectbestanden komen niet opnieuw langs het schema."""

    @pytest.mark.parametrize("repo_path", ONTSNAPT)
    @pytest.mark.parametrize("functie", PAD_FUNCTIES)
    def test_weigert_een_uitkomst_buiten_de_repo(self, functie: Callable[[str], str], repo_path: str) -> None:
        with pytest.raises(RepositoryPathError):
            functie(repo_path)

    @pytest.mark.parametrize("functie", PAD_FUNCTIES)
    def test_laat_de_vorm_van_vandaag_ongemoeid(self, functie: Callable[[str], str]) -> None:
        bare = functie("")
        assert not bare.startswith(("/", "."))
        assert functie(".") == f"./{bare}"
        assert functie("sub/map") == f"sub/map/{bare}"
        assert functie("a/../b") == f"a/../b/{bare}"


@pytest.fixture
def lege_werkmap(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path
    assert list(tmp_path.rglob("*")) == []


class TestDeSchrijversGaanLangsHetAnker:
    """De managers bouwden het pad eerder met de hand; een ontsnapping mag daar niets schrijven."""

    @pytest.mark.asyncio
    async def test_deploymentmanifesten(self, lege_werkmap: Path) -> None:
        manager = ProjectManager.__new__(ProjectManager)
        manager.get_contents = AsyncMock(return_value={})
        manager.get_name = AsyncMock(return_value="demo")
        manager.get_repository_path = AsyncMock(return_value="../../../../tmp/uit-de-repo")
        git_connector = MagicMock()
        git_connector.get_working_dir = AsyncMock(return_value=str(lege_werkmap))
        deployment = {"name": "deployment-1", "cluster": "odcn-production", "namespace": "demo", "repository": "r"}

        with pytest.raises(RepositoryPathError):
            await manager._process_deployment_manifests(deployment, git_connector)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ontsnapping", ["../buiten", "absoluut"])
    async def test_infrastructuurmanifesten(self, tmp_path: Path, ontsnapping: str) -> None:
        """De oude ``os.path.join`` liet een absoluut pad de werkmap zelfs helemaal wegduwen."""
        werkmap = tmp_path / "repo"
        werkmap.mkdir()
        repo_path = str(tmp_path / "buiten") if ontsnapping == "absoluut" else ontsnapping
        manager = ProjectManager.__new__(ProjectManager)
        manager._ensure_database_manager = AsyncMock(
            return_value=MagicMock(_get_database_cluster_config=MagicMock(return_value={"database_config": {}}))
        )
        manager.get_progress_manager = MagicMock(return_value=None)
        manager._kubectl_connector = MagicMock(get_secret=AsyncMock(return_value=None))
        manager.get_git_connector_for_deployment = AsyncMock(
            return_value=MagicMock(get_working_dir=AsyncMock(return_value=str(werkmap)))
        )
        project_data = {
            "name": "demo",
            "repositories": [{"name": "r", "url": "https://example.org/demo.git", "path": repo_path}],
        }

        with (
            patch("opi.core.cluster_config.get_infrastructure_namespace", return_value="rig-prd-demo-infra"),
            patch("opi.core.cluster_config.get_storage_class_name", return_value="standard"),
            patch("opi.generation.manifests.render_template", return_value="kind: Secret\n"),
            pytest.raises(RuntimeError) as exc,
        ):
            await manager._create_infrastructure_resources(project_data, "odcn-production")
        assert isinstance(exc.value.__cause__, RepositoryPathError)
        assert [p.relative_to(tmp_path) for p in tmp_path.rglob("*")] == [Path("repo")]

    @pytest.mark.asyncio
    async def test_pvc_hernoemen(self, lege_werkmap: Path) -> None:
        project_manager = MagicMock()
        project_manager.get_git_connector_for_deployment = AsyncMock(
            return_value=MagicMock(get_working_dir=AsyncMock(return_value=str(lege_werkmap)))
        )
        project_manager.get_repository_path = AsyncMock(return_value="/etc")
        project_manager._project_file_handler.extract_deployment_components.return_value = []
        project_data = {"repositories": [{"name": "r", "path": "/etc"}]}

        with pytest.raises(RepositoryPathError):
            await PVCManager(project_manager).handle_service_removal(
                "demo", "deployment-1", {"cluster": "odcn-production"}, project_data
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("repo_path", "verwacht"), [("..", None), (".", "./odcn-production/demo/deployment-1")])
    async def test_argocd_applicatie(self, lege_werkmap: Path, repo_path: str, verwacht: str | None) -> None:
        project_manager = MagicMock()
        project_manager.get_name = AsyncMock(return_value="demo")
        project_manager.get_git_connector_for_argocd = AsyncMock(
            return_value=MagicMock(get_working_dir=AsyncMock(return_value=str(lege_werkmap)))
        )
        project_manager.get_deployments = AsyncMock(
            return_value=[
                {"name": "deployment-1", "cluster": "odcn-production", "namespace": "demo", "repository": "r"}
            ]
        )
        manager = ArgoManager(project_manager)
        project_data = {"repositories": [{"name": "r", "url": "https://example.org/demo.git", "path": repo_path}]}

        with patch.object(manager, "generate_application_manifest", side_effect=RuntimeError("gestopt")) as gen:
            assert await manager.create_applications(project_data) is False
        if verwacht is None:
            gen.assert_not_called()
        else:
            assert gen.call_args.kwargs["repo_path"] == verwacht

    @pytest.mark.asyncio
    async def test_argocd_infrastructuur(self, lege_werkmap: Path) -> None:
        project_manager = MagicMock()
        project_manager.get_name = AsyncMock(return_value="demo")
        project_manager.get_git_connector_for_argocd = AsyncMock(
            return_value=MagicMock(get_working_dir=AsyncMock(return_value=str(lege_werkmap)))
        )
        manager = ArgoManager(project_manager)
        project_data = {"repositories": [{"name": "r", "url": "https://example.org/demo.git", "path": ".."}]}

        with patch(
            "opi.manager.argo_manager.generate_infrastructure_manifest_path",
            wraps=generate_infrastructure_manifest_path,
        ) as padfunctie:
            assert await manager.create_infrastructure_application(project_data, {}, "odcn-production") is False
        padfunctie.assert_called_once_with("odcn-production", "demo", "..")
