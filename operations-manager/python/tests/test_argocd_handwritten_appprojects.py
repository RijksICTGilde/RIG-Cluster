"""Geen handgeschreven Application op ``default``, en geen AppProject dat alles toelaat.

ArgoCD maakt het AppProject ``default`` zelf aan zodra het ontbreekt, met
``sourceRepos: ['*']``, alle bestemmingen en alle resource-soorten. Zolang de drie
platform-Applications daarop stonden was dat een open deur naast de begrensde projecten
die OPI per project schrijft. Sinds RC-301 staat ``default`` leeg in git en heeft elke
Application een eigen, begrensd project.

Niets dan een test houdt dat vast: een Application zonder ``spec.project`` valt bij
ArgoCD terug op ``default``, en een ``'*'`` terugzetten in een AppProject kost een regel.
De wandeling leest elke yaml onder ``bootstrap/`` in plaats van een lijst met bestanden,
zodat een manifest dat later bijkomt meteen meetelt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOOTSTRAP = _REPO_ROOT / "bootstrap"


def _documents(kind: str) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(_BOOTSTRAP.rglob("*.yaml")):
        found.extend(
            (str(path.relative_to(_REPO_ROOT)), document)
            for document in yaml.safe_load_all(path.read_text())
            if isinstance(document, dict) and document.get("kind") == kind
        )
    return found


_APPLICATIONS = _documents("Application")
_APPPROJECTS = _documents("AppProject")
_APPLICATION_IDS = [path for path, _ in _APPLICATIONS]
_APPPROJECT_IDS = [path for path, _ in _APPPROJECTS]


#: Uitgeschreven en niet uit ``_BOOTSTRAP`` afgeleid: een anker dat meebeweegt met wat
#: het vastzet, houdt niets vast.
_OVERLAYS_VANAF_REPO_ROOT = "bootstrap/rig-system/kustomize/overlays"
_APP_OF_APPS = f"{_OVERLAYS_VANAF_REPO_ROOT}/odcn-production/argocd-application-user-applications.yaml"


def test_de_wandeling_vindt_de_handgeschreven_manifests() -> None:
    """Een lege parametrize slaat over in plaats van te falen, dus zonder deze toets
    zouden de controles hieronder succes melden over niets: precies wat een verplaatste
    ``bootstrap/`` of een verkeerde ``parents``-index achterlaat.

    Het app-of-apps wordt met naam genoemd omdat het de app is die alles van de projecten
    synct. De drie overlays staan ernaast omdat een walk die op odcn-production uitkomt
    dat ene bestand houdt en de andere Applications stil laat vallen."""
    assert _APP_OF_APPS in _APPLICATION_IDS, _APPLICATION_IDS
    assert {Path(path).parent.name for path in _APPLICATION_IDS} == {
        "odcn-production",
        "odcn-staging",
        "sandboxed-local",
    }
    assert "default" in {document["metadata"]["name"] for _, document in _APPPROJECTS}


@pytest.mark.parametrize(("path", "document"), _APPLICATIONS, ids=_APPLICATION_IDS)
def test_geen_handgeschreven_application_staat_op_default(path: str, document: dict[str, Any]) -> None:
    """Ook een ontbrekende ``spec.project`` is fout: ArgoCD leest dat als ``default``."""
    assert document["spec"].get("project") not in (None, "", "default"), path


@pytest.mark.parametrize(("path", "document"), _APPPROJECTS, ids=_APPPROJECT_IDS)
def test_geen_handgeschreven_appproject_laat_alles_toe(path: str, document: dict[str, Any]) -> None:
    spec = document["spec"]
    ruim = [repo for repo in spec["sourceRepos"] if "*" in repo]
    for destination in spec["destinations"]:
        ruim += [value for value in destination.values() if "*" in value]
    assert ruim == [], f"{path} laat {ruim} toe"


def test_het_appproject_default_laat_niets_toe() -> None:
    """De kern van RC-301. Verdwijnt dit bestand, dan maakt ArgoCD ``default`` weer
    helemaal open aan, zonder dat er iets rood wordt."""
    gevonden = [document for _, document in _APPPROJECTS if document["metadata"]["name"] == "default"]

    assert len(gevonden) == 1
    spec = gevonden[0]["spec"]
    assert spec["sourceRepos"] == []
    assert spec["destinations"] == []
    assert spec["clusterResourceWhitelist"] == []
    # Met `[]` en niet afwezig: afwezig (nil) is in ArgoCD juist alles toestaan.
    assert spec["namespaceResourceWhitelist"] == []
