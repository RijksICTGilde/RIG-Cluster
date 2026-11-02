"""Het AppProject van een project accepteert alleen de repo's van dat project.

Het stond op ``sourceRepos: ['*']``, dus elk project mocht van ArgoCD uit elke repo
uitrollen; de grens lag alleen in wat OPI zelf schrijft. De URL hier moet letterlijk
gelijk zijn aan de URL in de Application ernaast, userinfo incluis, want dat is wat
ArgoCD vergelijkt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from opi.core.cluster_config import get_argo_namespace, get_prefixed_namespace
from opi.core.config import settings
from opi.generation.manifests import ManifestGenerator
from opi.manager.argo_manager import ArgoManager

if TYPE_CHECKING:
    from pathlib import Path

_REPO_URL = "https://my-project@github.com/RijksICTGilde/zad-deployments.git"


@pytest.fixture
def argo_manager() -> ArgoManager:
    project_manager = MagicMock()
    project_manager._manifest_generator = ManifestGenerator()
    return ArgoManager(project_manager)


def _render(argo_manager: ArgoManager, source_repos: list[str]) -> dict:
    manifest = argo_manager.generate_appproject_manifest(
        name="my-project-my-project",
        namespace="rig-system",
        destination_namespace="rig-my-project",
        project_label="my-project",
        source_repos=source_repos,
    )
    return yaml.safe_load(manifest)


def test_de_gerenderde_appproject_draagt_precies_de_url_van_het_project(argo_manager: ArgoManager) -> None:
    assert _render(argo_manager, [_REPO_URL])["spec"]["sourceRepos"] == [_REPO_URL]


def test_een_project_met_twee_repositories_krijgt_ze_allebei(argo_manager: ArgoManager) -> None:
    tweede = "https://my-project@github.com/RijksICTGilde/ander.git"
    assert _render(argo_manager, [_REPO_URL, tweede])["spec"]["sourceRepos"] == [_REPO_URL, tweede]


def test_zonder_bronrepo_blijft_sourcerepos_een_lijst(argo_manager: ArgoManager) -> None:
    """Een leeg veld rendert ``[]``, niet een lege regel.

    Een bare sleutel laadt als ``null``. De CRD typeert het veld als array zonder
    nullable: ``kubectl apply --server-side`` weigert dat manifest, en op het pad dat
    ArgoCD vandaag loopt wordt de null gesnoeid en staat het veld er daarna niet meer.
    """
    assert _render(argo_manager, [])["spec"]["sourceRepos"] == []


def test_de_bronrepos_komen_per_namespace_en_in_de_vorm_die_argocd_vergelijkt() -> None:
    """``make_argocd_repository_url_unique`` zet de projectnaam als userinfo in de URL.

    Zonder dat staat hier een andere string dan in de Application, en weigert ArgoCD de
    bron van zijn eigen Application.
    """
    deployments = [
        {"name": "een", "namespace": "productie", "repository": "main-repo"},
        {"name": "twee", "namespace": "productie", "repository": "andere-repo"},
        {"name": "drie", "namespace": "acceptatie", "repository": "main-repo"},
    ]
    repositories = [
        {"name": "main-repo", "url": "https://github.com/RijksICTGilde/zad-deployments.git"},
        {"name": "andere-repo", "url": "https://github.com/RijksICTGilde/ander.git"},
    ]

    gevonden = ArgoManager._source_repos_per_namespace(deployments, repositories, "my-project")

    assert gevonden == {
        "productie": [
            "https://my-project@github.com/RijksICTGilde/ander.git",
            "https://my-project@github.com/RijksICTGilde/zad-deployments.git",
        ],
        "acceptatie": ["https://my-project@github.com/RijksICTGilde/zad-deployments.git"],
    }


def test_een_deployment_zonder_vindbare_repository_levert_geen_namespace_op() -> None:
    gevonden = ArgoManager._source_repos_per_namespace(
        [{"name": "een", "namespace": "productie", "repository": "weg"}], [], "my-project"
    )

    assert gevonden == {}


@pytest.mark.asyncio
async def test_create_app_projects_zet_in_elk_bestand_alleen_de_repos_van_die_namespace(
    tmp_path: Path,
) -> None:
    """De volledige weg: ``create_app_projects`` berekent zelf de bronrepo's per namespace
    en geeft precies die mee aan het bestand van die namespace. Een project met twee
    deployments in twee namespaces, op twee verschillende repo's, mag de ene namespace niet
    de repo van de andere lenen: dat zou syncen toestaan wat die namespace niet nodig heeft.
    """
    cluster = settings.CLUSTER_MANAGER
    project_manager = MagicMock()
    project_manager._manifest_generator = ManifestGenerator()
    project_manager.get_name = AsyncMock(return_value="demo")
    project_manager.get_git_connector_for_argocd = AsyncMock(
        return_value=MagicMock(get_working_dir=AsyncMock(return_value=str(tmp_path)))
    )
    project_manager.get_deployments = AsyncMock(
        return_value=[
            {"name": "een", "cluster": cluster, "namespace": "demo", "repository": "main-repo"},
            {"name": "twee", "cluster": cluster, "namespace": "staging", "repository": "andere-repo"},
        ]
    )
    project_data = {
        "repositories": [
            {"name": "main-repo", "url": "https://github.com/RijksICTGilde/zad-deployments.git"},
            {"name": "andere-repo", "url": "https://github.com/RijksICTGilde/ander.git"},
        ]
    }

    manager = ArgoManager(project_manager)
    await manager.create_app_projects(project_data)

    gerenderd = [yaml.safe_load(path.read_text()) for path in tmp_path.rglob("*-argocd-appproject.yaml")]
    assert len(gerenderd) == 2

    def _sourcerepos_voor(base_namespace: str) -> list[str]:
        prefixed = get_prefixed_namespace(cluster, base_namespace)
        [document] = [d for d in gerenderd if d["spec"]["destinations"][0]["namespace"] == prefixed]
        assert document["metadata"]["namespace"] == get_argo_namespace(cluster)
        return document["spec"]["sourceRepos"]

    assert _sourcerepos_voor("demo") == ["https://demo@github.com/RijksICTGilde/zad-deployments.git"]
    assert _sourcerepos_voor("staging") == ["https://demo@github.com/RijksICTGilde/ander.git"]
