"""``NullProgress``: the stand-in for an entrance that answers inside the request.

The use cases call these methods with the exact signatures ``configure_service`` and
``set_component_values`` use (a bare ``add_task(name)``, a keyword ``subject=``); a
mismatch there is a ``TypeError`` the moment a synchronous entrance (the env-vars window)
runs for real, not something a mock of the use case would ever catch.
"""

from __future__ import annotations

from opi.manager.service.progress import NullProgress


def test_every_report_is_accepted_and_keeps_none_of_it() -> None:
    progress = NullProgress()

    task_id = progress.add_task("stap-een", subject="iets")
    assert progress.complete_task(task_id) is None
    assert progress.fail_task(task_id, "foutmelding") is None
    assert progress.fail_project("foutmelding") is None
    assert progress.update_current_step("volgende stap") is None


def test_add_task_without_a_subject_still_returns_a_usable_id() -> None:
    """The use cases call ``add_task(name)`` with no subject on their first step."""
    progress = NullProgress()

    task_id = progress.add_task("project-naam-check")

    assert isinstance(task_id, str)
