"""The syncOptions on the Applications in this repo must all be options ArgoCD knows.

RC-226 found two that were not, on the generated Application. ``Timeout=300`` does not
exist as a sync option, so it reads as an upper bound on the waiting that is not there,
which is worse than no line at all. And ``Delete`` only takes ``false`` or ``confirm`` at
application level, so ``Delete=true`` was silently ignored.

RC-228 asked the same question of ``SyncTimeout=60s`` on the two handwritten platform
Applications, and the answer is the same. Those two live on odcn-production, whose
``argocd-deployment.yaml`` pins ``argocd-rig:v3.5.1-rig2``, upstream ArgoCD v3.5.1, and
v3.5.1 reads no such option: its only ``SyncTimeout`` is the resync timeout of the cluster
cache, a controller setting.

Nothing but a test keeps these out. The Application CRD types ``syncOptions`` as a plain
array of strings, so ArgoCD accepts an option it has never heard of without complaint.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OVERLAYS = _REPO_ROOT / "bootstrap" / "rig-system" / "kustomize" / "overlays"

#: Every option ArgoCD v3.5.1 reads from ``spec.syncPolicy.syncOptions``, measured on the
#: ``SyncOptions.HasOption`` and ``SyncOptions.GetOptionValue`` calls in ``controller/``.
#: ``Force=true`` is deliberately not here: the only reader of it is
#: ``HasAnnotationOption`` in gitops-engine, so it works as a resource annotation and does
#: nothing on an Application.
KNOWN_SYNC_OPTIONS = {
    "ApplyOutOfSyncOnly",
    "ClientSideApplyMigration",
    "CreateNamespace",
    "Delete",
    "FailOnSharedResource",
    "Prune",
    "PruneLast",
    "PrunePropagationPolicy",
    "Replace",
    "RespectIgnoreDifferences",
    "ServerSideApply",
    "SkipDryRunOnMissingResource",
    "Validate",
}


def _handwritten_applications() -> list[tuple[str, list[str]]]:
    """Every Application manifest under the rig-system overlays, with its syncOptions.

    Read from a glob rather than a list, so an overlay added later is covered without
    anyone remembering to add it here.
    """
    found: list[tuple[str, list[str]]] = []
    for path in sorted(_OVERLAYS.glob("*/argocd-application-*.yaml")):
        for document in yaml.safe_load_all(path.read_text()):
            if not document or document.get("kind") != "Application":
                continue
            sync_policy = document.get("spec", {}).get("syncPolicy") or {}
            found.append((str(path.relative_to(_REPO_ROOT)), sync_policy.get("syncOptions") or []))
    return found


_APPLICATIONS = _handwritten_applications()
_IDS = [path for path, _ in _APPLICATIONS]


def test_the_glob_finds_the_applications_this_guard_is_about() -> None:
    """Without this the tests below pass on an empty list, which is how a moved file or a
    renamed overlay would turn the whole guard off."""
    assert (
        "bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-application-production-infrastructure.yaml"
        in _IDS
    )
    assert "bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-application-ron-infrastructure.yaml" in _IDS


@pytest.mark.parametrize(("path", "sync_options"), _APPLICATIONS, ids=_IDS)
def test_every_sync_option_is_one_argocd_knows(path: str, sync_options: list[str]) -> None:
    unknown = [option for option in sync_options if option.split("=", 1)[0] not in KNOWN_SYNC_OPTIONS]
    assert unknown == [], f"{path} carries sync options ArgoCD ignores: {unknown}"


@pytest.mark.parametrize(("path", "sync_options"), _APPLICATIONS, ids=_IDS)
def test_synctimeout_is_gone(path: str, sync_options: list[str]) -> None:
    """It bounded nothing, and reading like it did is the damage RC-226 documented."""
    assert not [option for option in sync_options if option.startswith("SyncTimeout")], path
