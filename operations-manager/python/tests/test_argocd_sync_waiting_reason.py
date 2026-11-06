"""describe_sync_waiting: what the progress line says while ArgoCD itself is still busy."""

from opi.services.argocd_operation import describe_sync_waiting


def _application(sync: str, phase: str | None = None, message: str = "") -> dict:
    status: dict = {"sync": {"status": sync}, "health": {"status": "Healthy"}}
    if phase:
        status["operationState"] = {"phase": phase, "message": message}
    return {"status": status}


def test_stale_status_says_argocd_has_not_processed_the_change() -> None:
    assert describe_sync_waiting(_application("Synced"), fresh=False) == "ArgoCD heeft de wijziging nog niet verwerkt"


def test_running_operation_carries_argocds_message() -> None:
    application = _application("OutOfSync", "Running", "waiting for completion of hook batch/Job/migrate")
    assert (
        describe_sync_waiting(application, fresh=True)
        == "ArgoCD synchroniseert: waiting for completion of hook batch/Job/migrate"
    )


def test_running_operation_without_message() -> None:
    assert describe_sync_waiting(_application("Synced", "Running"), fresh=True) == "ArgoCD synchroniseert"


def test_terminating_operation() -> None:
    assert (
        describe_sync_waiting(_application("Synced", "Terminating"), fresh=True) == "ArgoCD breekt de lopende sync af"
    )


def test_out_of_sync_without_operation() -> None:
    assert (
        describe_sync_waiting(_application("OutOfSync", "Succeeded"), fresh=True)
        == "ArgoCD heeft nog niet gesynchroniseerd (OutOfSync)"
    )


def test_synced_leaves_the_story_to_the_pods() -> None:
    assert describe_sync_waiting(_application("Synced", "Succeeded"), fresh=True) is None
