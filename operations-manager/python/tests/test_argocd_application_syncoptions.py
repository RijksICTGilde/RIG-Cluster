"""The syncOptions on the Applications in this repo must all be options ArgoCD knows.

RC-226 found two that were not, on the generated Application. ``Timeout=300`` does not
exist as a sync option, so it reads as an upper bound on the waiting that is not there,
which is worse than no line at all. And ``Delete`` only takes ``false`` or ``confirm`` at
application level, so ``Delete=true`` was silently ignored.

RC-228 asked the same question of ``SyncTimeout=60s`` on the two handwritten platform
Applications, and the answer is the same. Those two live on odcn-production, whose
``argocd-deployment.yaml`` pins ``argocd-rig:v3.5.1-rig2``, upstream ArgoCD v3.5.1. That
version reads no such option: its only ``SyncTimeout`` is ``informerSyncTimeout``, an
internal wait on an informer cache in the API server.

Neither removal takes a bound away -- ``Timeout=300`` no more than these two -- because
a sync option was never what set one. The bound that does exist is a controller
setting, and on odcn-production it is already on: ``argocd-deployment.yaml`` there sets
``controller.sync.timeout.seconds: "300"`` in the ``extraConfig`` the operator renders
into ``argocd-cm`` (measured in that ConfigMap on rig-prd-operations). It is a setting
on the ArgoCD instance, so it covers every Application on that cluster, the generated
ones as much as these two handwritten ones. The place where an upper bound on a sync
belongs is filled, which is why these lines should stay out rather than come back: they
only add the impression of a second one. The local and sandboxed-local overlays leave
it unset; if a bound is wanted there, that ``extraConfig`` is where it goes, not
``syncOptions``.

That setting is not a way out of a sync that hangs, and it should not be written up as
one. The deletion that hung in issue #184 ran fourteen days on ``Running`` with the
300s in place: the controller logged ``sync/terminate complete`` on every reconcile, so
the bound did fire, every round. What it fires is a request to terminate, and that is
all it is -- the operation stays until ``.operation`` itself is cleared, which is the
removal RC-226 makes.

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

    Found by reading every yaml in the tree and keeping what declares ``kind: Application``,
    rather than by matching a filename. An overlay or a manifest added later is then covered
    without anyone remembering to add it here, and one that is renamed or moved into a
    subdirectory stays covered instead of dropping out of the parametrize unnoticed.
    """
    found: list[tuple[str, list[str]]] = []
    for path in sorted(_OVERLAYS.rglob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text()):
            if not document or document.get("kind") != "Application":
                continue
            sync_policy = document.get("spec", {}).get("syncPolicy") or {}
            found.append((str(path.relative_to(_REPO_ROOT)), sync_policy.get("syncOptions") or []))
    return found


_APPLICATIONS = _handwritten_applications()
_IDS = [path for path, _ in _APPLICATIONS]

#: The overlay that carries both Applications the ``SyncTimeout`` lines came off.
_ODCN_ARGOCD = _OVERLAYS / "odcn-production" / "argocd-deployment.yaml"


def test_the_glob_finds_the_applications_this_guard_is_about() -> None:
    """An empty parametrize skips rather than fails, so without this the two tests below
    would report success over nothing at all -- which is what a moved ``_OVERLAYS`` or a
    wrong ``parents`` index leaves behind.

    The two files are named because the ``SyncTimeout`` lines came off them. The three
    cluster overlays are named next to them because naming only those two files leaves the
    other direction open: a walk that narrows to odcn-production keeps both of them and
    drops the five Applications in the other overlays without failing anything."""
    assert (
        "bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-application-production-infrastructure.yaml"
        in _IDS
    )
    assert "bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-application-ron-infrastructure.yaml" in _IDS

    overlays_from_repo_root = _OVERLAYS.relative_to(_REPO_ROOT)
    covered = {Path(path).relative_to(overlays_from_repo_root).parts[0] for path in _IDS}
    assert covered == {"local", "odcn-production", "sandboxed-local"}, f"overlays covered: {sorted(covered)}"


# The name says *handwritten* because the RC-226 half of this file lands in this same
# module with its own version of this check over the *generated* Application. Two test
# functions of one name in one module is not a union: the later definition shadows the
# earlier one, and pytest reports neither a failure nor a warning.
@pytest.mark.parametrize(("path", "sync_options"), _APPLICATIONS, ids=_IDS)
def test_every_sync_option_on_a_handwritten_application_is_one_argocd_knows(path: str, sync_options: list[str]) -> None:
    unknown = [option for option in sync_options if option.split("=", 1)[0] not in KNOWN_SYNC_OPTIONS]
    assert unknown == [], f"{path} carries sync options ArgoCD ignores: {unknown}"


def test_the_controller_level_sync_timeout_is_still_set() -> None:
    """The docstring above argues the two ``SyncTimeout=60s`` lines can go because the
    bound they read as is already set one level up. That argument holds only while this
    setting is here, so it goes red with it rather than quietly becoming untrue."""
    extra_config = yaml.safe_load(_ODCN_ARGOCD.read_text())["spec"]["extraConfig"]
    assert extra_config.get("controller.sync.timeout.seconds") == "300"


@pytest.mark.parametrize(("path", "sync_options"), _APPLICATIONS, ids=_IDS)
def test_synctimeout_is_gone(path: str, sync_options: list[str]) -> None:
    """It was never a sync option, so it never bounded anything."""
    assert not [option for option in sync_options if option.startswith("SyncTimeout")], path


# Named for the *handwritten* Applications for the same reason as the check above: the
# RC-226 half carries ``test_delete_true_is_gone`` over the generated one.
@pytest.mark.parametrize(("path", "sync_options"), _APPLICATIONS, ids=_IDS)
def test_delete_carries_a_value_argocd_reads_on_a_handwritten_application(path: str, sync_options: list[str]) -> None:
    """``Delete`` is in ``KNOWN_SYNC_OPTIONS``, so the name check above passes it whatever
    it says after the ``=``. The docstring at the top of this module writes down that
    application level reads only ``false`` and ``confirm`` there, and a line that says
    anything else is ignored exactly as silently as ``SyncTimeout=60s`` was."""
    wrong = []
    for option in sync_options:
        name, _, value = option.partition("=")
        if name == "Delete" and value not in {"false", "confirm"}:
            wrong.append(option)
    assert wrong == [], f"{path} carries {wrong}; at application level Delete reads only false or confirm"
