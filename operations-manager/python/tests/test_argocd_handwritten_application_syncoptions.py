"""The syncOptions on the handwritten platform Applications must all be options ArgoCD knows.

RC-226 asked this of the *generated* Application and found two that were not; that half
lives next to this one, in ``test_argocd_generated_application_syncoptions.py``, and the
reasoning for its two removals is written up there. RC-228 asked the same question of
``SyncTimeout=60s`` on the two handwritten platform Applications under ``bootstrap/``,
and the answer is the same. Those two live on odcn-production, whose
``argocd-deployment.yaml`` pins ``argocd-rig:v3.5.1-rig2``, upstream ArgoCD v3.5.1. That
version reads no such option: its only ``SyncTimeout`` is ``informerSyncTimeout``, an
internal wait on an informer cache in the API server.

Neither removal takes a bound away -- ``Timeout=300`` no more than these two -- because
a sync option was never what set one. The bound that does exist is a controller setting,
and RC-250 found that on odcn-production it was NOT on. This module used to say it was,
on the strength of ``controller.sync.timeout.seconds: "300"`` sitting in the
``extraConfig`` of ``argocd-deployment.yaml``. That block is rendered into ``argocd-cm``,
and this is a cmd-params key, which nothing reads there: the running controller carried
no ``--sync-timeout``, and in 2,5 hours of controller log there was not one
``Terminating in-progress operation due to timeout`` while a sync ran 45 minutes. So the
bound was dead for nine months, and the paragraph that leaned on it was reasoning, not a
measurement.

It is now set the only way that reaches this operator's controller, as
``--sync-timeout=600`` in ``spec.controller.extraCommandArgs``, in both overlays.
It remains a setting on the ArgoCD instance, so it covers every Application on that
cluster, the generated ones as much as these two handwritten ones.
``test_argocd_extra_config_keys.py`` guards both halves: no cmd-params key back in
``extraConfig``, and the flag present.

Once set, it does end a hanging sync, measured on the sandbox: at exactly 600 seconds the
controller logged ``Terminating in-progress operation due to timeout``, moved the phase
``Terminating -> Failed`` with message ``Operation terminated``, and started a sync on the
newest revision in the same second. It is still not the whole answer: it only fires on the
Application a sync is stuck on, and the deletion in
``features/argocd-vastgelopen-verwijdering.md`` needs ``.operation`` itself cleared, which
is the removal RC-226 makes and RC-250 extends to the deploy path.

One thing that document reads as the bound firing is not: the ``sync/terminate complete``
line on every reconcile. RC-250 measured those lines on an instance with the bound set AND
saw the same lines before it was set; they belong to the per-reconcile teardown of the sync
context that follows ``Resuming in-progress operation``, not to any bound.

Nothing but a test keeps these out. The Application CRD types ``syncOptions`` as a plain
array of strings, so ArgoCD accepts an option it has never heard of without complaint.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tests.argocd_sync_options import KNOWN_SYNC_OPTIONS

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OVERLAYS = _REPO_ROOT / "bootstrap" / "rig-system" / "kustomize" / "overlays"


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

_ODCN_ARGOCD = _OVERLAYS / "odcn-production" / "argocd-deployment.yaml"

#: Written out rather than derived from ``_OVERLAYS``, which is what the check below
#: pins: an anchor that moves with the thing it anchors holds nothing down.
_OVERLAYS_FROM_REPO_ROOT = "bootstrap/rig-system/kustomize/overlays"
_PRODUCTION_INFRA = f"{_OVERLAYS_FROM_REPO_ROOT}/odcn-production/argocd-application-production-infrastructure.yaml"
_RON_INFRA = f"{_OVERLAYS_FROM_REPO_ROOT}/odcn-production/argocd-application-ron-infrastructure.yaml"


def test_the_walk_finds_the_applications_and_reads_their_sync_options() -> None:
    """An empty parametrize skips rather than fails, so without this the checks below
    would report success over nothing at all -- which is what a moved ``_OVERLAYS`` or a
    wrong ``parents`` index leaves behind.

    The two files are named because the ``SyncTimeout`` lines came off them. The two
    cluster overlays are named next to them because naming only those two files leaves the
    other direction open: a walk that narrows to odcn-production keeps both of them and
    drops the three Applications in the other overlay without failing anything.

    Finding the files is not enough, because ``_handwritten_applications`` falls back to
    an empty list for anything it cannot read: rename the ``syncOptions`` key or lose
    ``spec.syncPolicy`` and every check below passes over nothing while the manifests
    still carry whatever they carry. So what it read off the two files is asserted here
    as well. The sibling guard needs no such line: it indexes into the rendered manifest
    and raises a ``KeyError`` instead of falling back."""
    assert _PRODUCTION_INFRA in _IDS
    assert _RON_INFRA in _IDS

    covered = {Path(path).relative_to(_OVERLAYS_FROM_REPO_ROOT).parts[0] for path in _IDS}
    assert covered == {"odcn-production", "sandboxed-local"}, f"overlays covered: {sorted(covered)}"

    for named in (_PRODUCTION_INFRA, _RON_INFRA):
        read = [options for path, options in _APPLICATIONS if path == named]
        assert read == [["CreateNamespace=false"]], f"{named} yielded {read}"


# The name says *handwritten* because the generated Application has its own version of
# this check, in the sibling module. A failure report names the test, so the name has to
# say which of the two sources went red.
@pytest.mark.parametrize(("path", "sync_options"), _APPLICATIONS, ids=_IDS)
def test_every_sync_option_on_a_handwritten_application_is_one_argocd_knows(path: str, sync_options: list[str]) -> None:
    unknown = [option for option in sync_options if option.split("=", 1)[0] not in KNOWN_SYNC_OPTIONS]
    assert unknown == [], f"{path} carries sync options ArgoCD ignores: {unknown}"


def test_the_controller_level_sync_timeout_is_still_set() -> None:
    """The docstring above argues the two ``SyncTimeout=60s`` lines can go because the
    bound they read as is set one level up. That argument holds only while that setting is
    here, so it goes red with it rather than quietly becoming untrue.

    It reads the flag and no longer the ``extraConfig`` key: the key was there and did
    nothing, so a green on it was the untrue half this check was meant to prevent."""
    spec = yaml.safe_load(_ODCN_ARGOCD.read_text())["spec"]
    assert "--sync-timeout=600" in spec["controller"]["extraCommandArgs"]
    assert "controller.sync.timeout.seconds" not in spec["extraConfig"]


@pytest.mark.parametrize(("path", "sync_options"), _APPLICATIONS, ids=_IDS)
def test_synctimeout_is_gone(path: str, sync_options: list[str]) -> None:
    """It was never a sync option, so it never bounded anything."""
    assert not [option for option in sync_options if option.startswith("SyncTimeout")], path


# Named for the *handwritten* Applications for the same reason as the check above: the
# sibling module carries ``test_delete_true_is_gone`` over the generated one.
@pytest.mark.parametrize(("path", "sync_options"), _APPLICATIONS, ids=_IDS)
def test_delete_carries_a_value_argocd_reads_on_a_handwritten_application(path: str, sync_options: list[str]) -> None:
    """``Delete`` is in ``KNOWN_SYNC_OPTIONS``, so the name check above passes it whatever
    it says after the ``=``. At application level ArgoCD reads only ``false`` and
    ``confirm`` there, which RC-226 measured and the sibling guard writes up. Any other
    value is ignored as silently as ``SyncTimeout=60s`` was."""
    wrong = []
    for option in sync_options:
        name, _, value = option.partition("=")
        if name == "Delete" and value not in {"false", "confirm"}:
            wrong.append(option)
    assert wrong == [], f"{path} carries {wrong}; at application level Delete reads only false or confirm"
