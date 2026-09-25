"""The syncOptions on a generated Application must all be options ArgoCD knows (RC-226).

Two of them were not. ``Timeout=300`` does not exist as a sync option, so it reads as an
upper bound on the waiting that is not there, which is worse than no line at all. And
``Delete`` only takes ``false`` or ``confirm`` at application level, so ``Delete=true``
was silently ignored. ArgoCD accepts both without complaint: nothing but a test keeps
them out once they are gone.
"""

from unittest.mock import MagicMock

import yaml
from opi.generation.manifests import ManifestGenerator
from opi.manager.argo_manager import ArgoManager

#: Everything ArgoCD accepts at application level, per its sync-options documentation.
KNOWN_SYNC_OPTIONS = {
    "ApplyOutOfSyncOnly",
    "CreateNamespace",
    "Delete",
    "FailOnSharedResource",
    "PruneLast",
    "PrunePropagationPolicy",
    "Replace",
    "RespectIgnoreDifferences",
    "ServerSideApply",
    "SkipDryRunOnMissingResource",
    "Validate",
}


def _rendered_application() -> dict:
    project_manager = MagicMock()
    project_manager._manifest_generator = ManifestGenerator()
    manifest = ArgoManager(project_manager).generate_application_manifest(
        name="mpfm-w3h-pr-310",
        namespace="argocd",
        argo_project="mpfm-w3h",
        repo_url="https://forgejo.example/zad-deployments.git",
        target_revision="main",
        repo_path="odcn-production/mpfm-w3h/pr-310",
        destination_namespace="rig-prd-mpfm-w3h",
        project_label="mpfm-w3h",
    )
    return yaml.safe_load(manifest)


def _sync_options() -> list[str]:
    return _rendered_application()["spec"]["syncPolicy"]["syncOptions"]


def test_every_sync_option_is_one_argocd_knows() -> None:
    unknown = [option for option in _sync_options() if option.split("=", 1)[0] not in KNOWN_SYNC_OPTIONS]
    assert unknown == []


def test_timeout_is_gone() -> None:
    """It was never a sync option, so it never bounded anything."""
    assert not [option for option in _sync_options() if option.startswith("Timeout")]


def test_delete_true_is_gone() -> None:
    """``Delete`` takes false or confirm; true was ignored."""
    assert "Delete=true" not in _sync_options()


def test_the_options_that_do_work_are_untouched() -> None:
    """PruneLast stays: it arrived with the initial commit and no reason for it was ever
    recorded, so RC-226 leaves it in place (points 1 and 2 solve the case with it on)."""
    assert _sync_options() == [
        "CreateNamespace=false",
        "PrunePropagationPolicy=foreground",
        "PruneLast=true",
        "SkipDryRunOnMissingResource=true",
    ]


def test_the_cascade_finalizer_is_still_there() -> None:
    """The finalizer is what performs the cascade; without it a delete leaves the
    resources behind, which is exactly the damage RC-226 is about."""
    assert _rendered_application()["metadata"]["finalizers"] == ["resources-finalizer.argocd.argoproj.io"]
