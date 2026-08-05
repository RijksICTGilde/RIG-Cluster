"""The V2 API stops reporting a switched-off deployment as Healthy (RC-31 step 4).

The same untruth as on the two pages, in the place clients read: every component off means
``replicas: 0``, Argo calls that Healthy because nothing is failing, and the API passed the
verdict straight through.

This is a deliberate change in public behaviour -- a client filtering on ``Healthy`` gets a
different answer than before -- and it is on the PR/release note for that reason.
"""

from __future__ import annotations

from opi.api.v2.models import DeploymentStatus
from opi.api.v2.router import _collapse_argo_status, _extract_live_status


def _argo(health: str, sync: str = "Synced") -> dict:
    return {"status": {"health": {"status": health}, "sync": {"status": sync}}}


class TestCollapsingArgoStatus:
    def test_a_switched_off_deployment_is_not_healthy(self) -> None:
        assert _collapse_argo_status("Synced", "Healthy", fully_disabled=True) == DeploymentStatus.Disabled

    def test_a_running_deployment_is_untouched(self) -> None:
        assert _collapse_argo_status("Synced", "Healthy") == DeploymentStatus.Healthy

    def test_a_real_failure_outranks_being_switched_off(self) -> None:
        """Turning components off must never be a way to make a failure disappear."""
        assert _collapse_argo_status("Synced", "Degraded", fully_disabled=True) == DeploymentStatus.Degraded
        assert _collapse_argo_status("Synced", "Missing", fully_disabled=True) == DeploymentStatus.Missing

    def test_drift_and_progress_keep_their_own_verdict(self) -> None:
        assert _collapse_argo_status("OutOfSync", "Healthy", fully_disabled=True) == DeploymentStatus.OutOfSync
        assert _collapse_argo_status("Synced", "Progressing", fully_disabled=True) == DeploymentStatus.Progressing

    def test_an_unknown_verdict_stays_unknown(self) -> None:
        """Only Healthy is replaced: Unknown is genuinely unknown, and claiming to know
        would be the same mistake in the other direction."""
        assert _collapse_argo_status("Synced", None, fully_disabled=True) == DeploymentStatus.Unknown


class TestTheStatusThatReachesAResponse:
    def test_the_flag_travels_through_the_extraction(self) -> None:
        live = _extract_live_status(_argo("Healthy"), fully_disabled=True)

        assert live.status == DeploymentStatus.Disabled

    def test_without_the_flag_nothing_changes(self) -> None:
        assert _extract_live_status(_argo("Healthy")).status == DeploymentStatus.Healthy

    def test_no_application_yet_is_still_pending(self) -> None:
        assert _extract_live_status(None, fully_disabled=True).status == DeploymentStatus.Pending
