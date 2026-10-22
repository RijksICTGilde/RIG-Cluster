"""Elke resource die een platform-Application rendert, staat op de whitelist van zijn AppProject.

Een kind die niet op ``namespaceResourceWhitelist`` staat, maakt ArgoCD de HELE sync
ongeldig ("resource batch:CronJob is not permitted in project zad-platform"), niet alleen
die ene resource. Zo stond na RC-301 de hele infrastructuur van de sandbox op OutOfSync,
en zou ``production-infrastructure`` bij de release niets meer naar productie syncen: de
CronJob ``rig-db-platform-backup`` stond op geen enkele whitelist.

De render loopt over een kopie zonder ksops-generators en zonder resources die niet in git
staan: de secrets zijn gitignored, en hun kind (Secret) staat toch al op de lijst.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OVERLAYS = _REPO_ROOT / "bootstrap/rig-system/kustomize/overlays"
_PROJECT = "zad-platform"

#: De in-cluster infrastructure-repo van de sandbox is een kopie van ``infrastructure/``.
_REPO_PREFIX = {"RIG-Cluster.git": "", "zad-argo-infrastructure.git": "infrastructure/"}


def _kustomize() -> str:
    found = shutil.which("kustomize")
    if found is None:
        pytest.skip("kustomize niet gevonden op PATH")
    return found


def _platform_sources() -> list[tuple[str, str]]:
    """``(overlay, pad in deze repo)`` van elke Application op ``zad-platform``."""
    found: list[tuple[str, str]] = []
    for path in sorted(_OVERLAYS.glob("*/argocd-application-*.yaml")):
        app = yaml.safe_load(path.read_text())
        if not isinstance(app, dict) or app.get("spec", {}).get("project") != _PROJECT:
            continue
        spec = app["spec"]
        for source in spec.get("sources") or [spec["source"]]:
            repo = source["repoURL"].rsplit("/", 1)[-1]
            if repo in _REPO_PREFIX and source.get("path"):
                found.append((path.parent.name, _REPO_PREFIX[repo] + source["path"]))
    return found


_SOURCES = _platform_sources()


def _whitelist(overlay: str) -> set[tuple[str, str]]:
    spec = yaml.safe_load((_OVERLAYS / overlay / f"argocd-appproject-{_PROJECT}.yaml").read_text())["spec"]
    entries = (spec.get("namespaceResourceWhitelist") or []) + (spec.get("clusterResourceWhitelist") or [])
    return {(entry.get("group", ""), entry["kind"]) for entry in entries}


def _copy_without_secrets(tmp_path: Path) -> Path:
    copy = tmp_path / "repo"
    shutil.copytree(
        _REPO_ROOT / "infrastructure", copy / "infrastructure", symlinks=True, ignore=shutil.ignore_patterns(".git")
    )
    for kustomization in copy.rglob("kustomization.yaml"):
        data: dict[str, Any] = yaml.safe_load(kustomization.read_text()) or {}
        here = kustomization.parent
        generators = [
            g
            for g in data.get("generators") or []
            if (here / g).exists() and "kind: ksops" not in (here / g).read_text()
        ]
        resources = [r for r in data.get("resources") or [] if "://" in r or (here / r).exists()]
        if generators != (data.get("generators") or []) or resources != (data.get("resources") or []):
            data["generators"] = generators
            data["resources"] = resources
            kept = {k: v for k, v in data.items() if v != []}
            # Kustomize weigert een kustomization zonder inhoud; een lege lijst telt wel.
            kept.setdefault("resources", [])
            kustomization.write_text(yaml.safe_dump(kept))
    return copy


def test_de_wandeling_vindt_de_platform_applications() -> None:
    """Een lege parametrize slaat over in plaats van te falen."""
    assert {overlay for overlay, _ in _SOURCES} >= {"odcn-production", "odcn-staging", "sandboxed-local"}, _SOURCES


@pytest.mark.parametrize(("overlay", "source"), _SOURCES, ids=[f"{o}:{s}" for o, s in _SOURCES])
def test_elke_gerenderde_kind_staat_op_de_whitelist(tmp_path: Path, overlay: str, source: str) -> None:
    copy = _copy_without_secrets(tmp_path)
    proc = subprocess.run(
        [_kustomize(), "build", "--enable-helm", "--load-restrictor", "LoadRestrictionsNone", str(copy / source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]

    allowed = _whitelist(overlay)
    missing: dict[str, list[str]] = {}
    for document in yaml.safe_load_all(proc.stdout):
        if not isinstance(document, dict):
            continue
        api_version: str = document["apiVersion"]
        group = api_version.rsplit("/", 1)[0] if "/" in api_version else ""
        if (group, document["kind"]) not in allowed:
            missing.setdefault(f"{group}/{document['kind']}", []).append(document["metadata"]["name"])
    assert not missing, f"{overlay}: {source} rendert kinds die {_PROJECT} niet toelaat: {missing}"
