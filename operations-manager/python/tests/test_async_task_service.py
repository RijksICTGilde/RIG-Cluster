"""Real-Postgres tests for the ORM-backed AsyncTaskService (RC-5 persistence)."""

import uuid
from datetime import datetime, timedelta

import pytest
from opi.core.async_task_service import MAX_ERROR_MESSAGE_CHARS, AsyncTaskService
from opi.core.db import session_scope
from opi.services.persistence.async_tasks import AsyncTask
from opi.services.persistence.project_reconciliation import ProjectReconciliation
from opi.services.project_reconciliation_service import record_reconciliation
from sqlalchemy import func, update


def _svc(cluster: str = "c1") -> AsyncTaskService:
    return AsyncTaskService(cluster=cluster)


async def _create(svc, *, project="p1", deployment="d1", task_type="upsert_deployment", payload=None, cluster="c1"):
    return await svc.create_task(
        task_type=task_type,
        project_name=project,
        deployment_name=deployment,
        cluster=cluster,
        payload=payload if payload is not None else {"image": "nginx:1"},
    )


async def _backdate_heartbeat(task_id: str, seconds: int) -> None:
    async with session_scope() as session:
        await session.execute(
            update(AsyncTask)
            .where(AsyncTask.id == uuid.UUID(task_id))
            .values(heartbeat_at=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, float(seconds)))
        )


async def test_create_defaults_and_roundtrip(orm_db):
    svc = _svc()
    row = await _create(svc)
    assert row["status"] == "pending"
    assert row["task_id"] == row["id"]
    assert row["payload"] == {"image": "nginx:1"}
    assert row["max_attempts"] == 3
    assert row["attempt_count"] == 0
    assert row["created_at"]


async def test_create_custom_max_attempts(orm_db):
    svc = _svc()
    row = await _create(svc, payload={"a": 1})
    assert row["max_attempts"] == 3
    row2 = await svc.create_task(
        task_type="backup", project_name="p2", deployment_name=None, cluster="c1", payload={}, max_attempts=5
    )
    assert row2["max_attempts"] == 5


async def test_create_dedup_identical_payload_returns_existing(orm_db):
    svc = _svc()
    first = await _create(svc, payload={"image": "nginx:1"})
    again = await _create(svc, payload={"image": "nginx:1"})
    assert again["task_id"] == first["task_id"]


async def test_create_different_payload_queues_new(orm_db):
    svc = _svc()
    first = await _create(svc, payload={"image": "nginx:1"})
    second = await _create(svc, payload={"image": "nginx:2"})
    assert second["task_id"] != first["task_id"]
    assert second["status"] == "pending"


async def test_claim_marks_claimed_and_sets_claimer(orm_db):
    svc = _svc()
    created = await _create(svc)
    claimed = await svc.claim_next_task(cluster="c1")
    assert claimed["task_id"] == created["task_id"]
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"]
    assert claimed["claimed_at"]


async def test_claim_empty_returns_none(orm_db):
    assert await _svc().claim_next_task(cluster="c1") is None


async def test_claim_skips_other_cluster(orm_db):
    svc = _svc()
    await _create(svc, cluster="c2")
    assert await svc.claim_next_task(cluster="c1") is None


async def test_claim_skips_inflight_same_deployment(orm_db):
    svc = _svc()
    await _create(svc, payload={"image": "nginx:1"})
    await _create(svc, payload={"image": "nginx:2"})  # queued behind, same project/deployment
    first = await svc.claim_next_task(cluster="c1")
    assert first is not None
    # Second task must not be claimed while the first is in-flight for the same deployment.
    assert await svc.claim_next_task(cluster="c1") is None


async def test_claim_respects_type_concurrency_limit(orm_db):
    svc = _svc()
    await _create(svc, project="pa", deployment="d", task_type="backup", payload={"n": 1})
    await _create(svc, project="pb", deployment="d", task_type="backup", payload={"n": 2})
    first = await svc.claim_next_task(cluster="c1", type_concurrency_limits={"backup": 1})
    assert first is not None
    # backup limit of 1 is reached by the claimed task -> the second is skipped.
    assert await svc.claim_next_task(cluster="c1", type_concurrency_limits={"backup": 1}) is None
    # Without the limit it can be claimed (different project, so no in-flight block).
    assert await svc.claim_next_task(cluster="c1") is not None


async def test_start_sets_running(orm_db):
    svc = _svc()
    created = await _create(svc)
    await svc.claim_next_task(cluster="c1")
    await svc.start_task(created["task_id"])
    assert (await svc.get_task(created["task_id"]))["status"] == "running"


async def test_update_progress_partial(orm_db):
    svc = _svc()
    created = await _create(svc)
    await svc.update_progress(created["task_id"], current_step="Building", progress_percent=42, logs=["a", "b"])
    task = await svc.get_task(created["task_id"])
    assert task["current_step"] == "Building"
    assert task["progress_percent"] == 42
    assert task["logs"] == ["a", "b"]
    assert task["heartbeat_at"]


async def test_update_progress_truncates_step(orm_db):
    svc = _svc()
    created = await _create(svc)
    await svc.update_progress(created["task_id"], current_step="x" * 300)
    assert len((await svc.get_task(created["task_id"]))["current_step"]) == 255


async def test_complete_sets_result_and_done(orm_db):
    svc = _svc()
    created = await _create(svc)
    await svc.complete_task(created["task_id"], result={"url": "https://x/"})
    task = await svc.get_task(created["task_id"])
    assert task["status"] == "completed"
    assert task["result"] == {"url": "https://x/"}
    assert task["progress_percent"] == 100
    assert task["current_step"] == "Done"
    assert task["completed_at"]


async def test_fail_retry_requeues(orm_db):
    svc = _svc()
    created = await _create(svc)
    await svc.fail_task(created["task_id"], error_message="boom", attempt_count=1, max_attempts=3)
    task = await svc.get_task(created["task_id"])
    assert task["status"] == "pending"
    assert task["attempt_count"] == 1  # incremented from 0
    assert task["claimed_by"] is None
    assert task["error_message"] == "boom"


async def test_fail_permanent(orm_db):
    svc = _svc()
    created = await _create(svc)
    await svc.fail_task(created["task_id"], error_message="fatal", attempt_count=3, max_attempts=3)
    task = await svc.get_task(created["task_id"])
    assert task["status"] == "failed"
    assert task["completed_at"]


async def test_een_gewone_foutzin_blijft_heel(orm_db):
    """Een foutmelding van 300 tekens werd afgeknipt op 255, midden in een woord, terwijl
    de subtaak dezelfde zin voluit droeg. De kolom is TEXT, dus die 255 hoorde nergens bij
    (zad-cli, punt 26)."""
    svc = _svc()
    created = await _create(svc)
    lange_zin = "e" * 300
    await svc.fail_task(created["task_id"], error_message=lange_zin, attempt_count=3, max_attempts=3)
    assert (await svc.get_task(created["task_id"]))["error_message"] == lange_zin


async def test_een_dump_wordt_alsnog_begrensd(orm_db):
    """Het vangnet blijft bestaan voor een exceptie die een dump meesleept."""
    svc = _svc()
    created = await _create(svc)
    await svc.fail_task(
        created["task_id"], error_message="e" * (MAX_ERROR_MESSAGE_CHARS + 100), attempt_count=3, max_attempts=3
    )
    msg = (await svc.get_task(created["task_id"]))["error_message"]
    assert len(msg) == MAX_ERROR_MESSAGE_CHARS
    assert msg.endswith("...")


async def test_get_task_not_found(orm_db):
    assert await _svc().get_task(str(uuid.uuid4())) is None


async def test_update_task_status_stamps_completed(orm_db):
    svc = _svc()
    created = await _create(svc)
    await svc.update_task_status(created["task_id"], status="cancelled")
    task = await svc.get_task(created["task_id"])
    assert task["status"] == "cancelled"
    assert task["completed_at"]


async def test_update_task_status_non_terminal_no_completed(orm_db):
    svc = _svc()
    created = await _create(svc)
    await svc.update_task_status(created["task_id"], status="running")
    assert (await svc.get_task(created["task_id"]))["completed_at"] is None


async def test_list_tasks_filters_and_total(orm_db):
    svc = _svc()
    await _create(svc, project="p1", payload={"n": 1})
    await _create(svc, project="p2", deployment="d2", payload={"n": 2})
    all_res = await svc.list_tasks()
    assert all_res["total"] == 2
    only_p1 = await svc.list_tasks(project_name="p1")
    assert only_p1["total"] == 1
    assert only_p1["tasks"][0]["project_name"] == "p1"


async def test_recover_stale_requeues_and_fails(orm_db):
    svc = _svc()
    # A stale task with retries left -> requeued.
    a = await _create(svc, project="pa", deployment="da", payload={"n": 1})
    await svc.claim_next_task(cluster="c1")
    await _backdate_heartbeat(a["task_id"], 600)
    # A stale task with no retries left -> failed.
    b = await _create(svc, project="pb", deployment="db", payload={"n": 2})
    await svc.claim_next_task(cluster="c1")
    async with session_scope() as session:
        await session.execute(
            update(AsyncTask).where(AsyncTask.id == uuid.UUID(b["task_id"])).values(attempt_count=3, max_attempts=3)
        )
    await _backdate_heartbeat(b["task_id"], 600)

    requeued = await svc.recover_stale_tasks(stale_threshold_seconds=300)
    assert requeued == 1
    assert (await svc.get_task(a["task_id"]))["status"] == "pending"
    assert (await svc.get_task(b["task_id"]))["status"] == "failed"


async def test_find_conflicting_task(orm_db):
    svc = _svc()
    a = await _create(svc, project="p1", task_type="backup", payload={"n": 1})
    await svc.claim_next_task(cluster="c1")  # claim a -> in-flight
    conflict = await svc.find_conflicting_task(
        task_id=str(uuid.uuid4()), task_type="backup", project_name="p1", deployment_name="d1"
    )
    assert conflict is not None
    assert conflict["task_id"] == a["task_id"]


async def test_find_conflicting_task_none(orm_db):
    svc = _svc()
    assert await svc.find_conflicting_task(task_id=str(uuid.uuid4()), task_type="backup", project_name="nope") is None


async def test_find_newer_active_tasks(orm_db):
    svc = _svc()
    first = await _create(svc, payload={"n": 1})
    second = await _create(svc, payload={"n": 2})
    newer = await svc.find_newer_active_tasks(task_id=first["task_id"], project_name="p1")
    assert [t["task_id"] for t in newer] == [second["task_id"]]


async def test_get_last_completed_task(orm_db):
    svc = _svc()
    created = await _create(svc)
    await svc.complete_task(created["task_id"])
    got = await svc.get_last_completed_task(task_type="upsert_deployment", project_name="p1", deployment_name="d1")
    assert got["task_id"] == created["task_id"]


async def test_get_last_completed_task_excludes_manual_when_scheduled(orm_db):
    svc = _svc()
    created = await _create(svc, payload={"trigger": "manual"})
    await svc.complete_task(created["task_id"])
    assert (
        await svc.get_last_completed_task(
            task_type="upsert_deployment", project_name="p1", deployment_name="d1", only_scheduled=True
        )
        is None
    )
    # Without the scheduled filter it is returned.
    assert (await svc.get_last_completed_task(task_type="upsert_deployment", project_name="p1", deployment_name="d1"))[
        "task_id"
    ] == created["task_id"]


async def test_cleanup_old_tasks(orm_db):
    svc = _svc()
    created = await _create(svc)
    await svc.complete_task(created["task_id"])
    async with session_scope() as session:
        await session.execute(
            update(AsyncTask)
            .where(AsyncTask.id == uuid.UUID(created["task_id"]))
            .values(completed_at=func.now() - func.make_interval(0, 0, 0, 0, 200))
        )
    deleted = await svc.cleanup_old_tasks(retention_hours=168)
    assert deleted == 1
    assert await svc.get_task(created["task_id"]) is None


# ---------------------------------------------------------------------------
# Deferred rollouts (RC-46): the drift the UI shows must be measured, not guessed.
# What clears it is what a processing run recorded as reconciled (RC-188).
# ---------------------------------------------------------------------------


async def _completed(svc, *, project, task_type, payload, deployment=None):
    row = await _create(svc, project=project, deployment=deployment, task_type=task_type, payload=payload)
    await svc.complete_task(row["task_id"])
    return row


async def _deferred_image(svc, deployment: str, project: str = "p1"):
    return await _completed(
        svc, project=project, deployment=deployment, task_type="update_image", payload={"rollout": False}
    )


async def test_no_deferred_rollouts_when_nothing_was_deferred(orm_db):
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web"})

    pending = await svc.get_deferred_rollouts("p1")
    assert pending == {"count": 0, "since": None, "task_types": [], "rollout_in_progress": False}


async def test_deferred_rollouts_are_counted_and_dated(orm_db):
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await _completed(svc, project="p1", task_type="configure_service", payload={"svc": "keycloak", "rollout": False})

    pending = await svc.get_deferred_rollouts("p1")
    assert pending["count"] == 2
    assert pending["since"] is not None
    assert pending["task_types"] == ["add_component", "configure_service"]


async def test_deferred_rollouts_are_scoped_to_one_project(orm_db):
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})

    assert (await svc.get_deferred_rollouts("p2"))["count"] == 0


async def test_a_project_wide_reconciliation_clears_everything_before_it(orm_db):
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await _deferred_image(svc, "d1")
    await record_reconciliation("p1", [], project_wide=True, read_seconds_ago=0)

    assert (await svc.get_deferred_rollouts("p1"))["count"] == 0


async def test_it_does_not_matter_which_task_type_processed(orm_db):
    """The mpfm-w3h case: eight full runs by tasks that were not refresh_project cleared nothing."""
    svc = _svc()
    await _deferred_image(svc, "d1")
    await _completed(svc, project="p1", task_type="refresh_project", payload={"force_clone": False})

    assert (await svc.get_deferred_rollouts("p1"))["count"] == 1

    await record_reconciliation("p1", ["d1"], project_wide=True, read_seconds_ago=0)

    assert (await svc.get_deferred_rollouts("p1"))["count"] == 0


async def test_a_change_deferred_after_the_reconciliation_still_counts(orm_db):
    svc = _svc()
    await record_reconciliation("p1", [], project_wide=True, read_seconds_ago=0)
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})

    pending = await svc.get_deferred_rollouts("p1")
    assert pending["count"] == 1
    assert pending["task_types"] == ["add_component"]


async def test_a_scoped_reconciliation_clears_only_its_own_deployment(orm_db):
    svc = _svc()
    await _deferred_image(svc, "d1")
    other = await _deferred_image(svc, "d2")
    await record_reconciliation("p1", ["d1"], project_wide=False, read_seconds_ago=0)

    pending = await svc.get_deferred_rollouts("p1")
    assert pending["count"] == 1
    assert pending["since"] == (await svc.get_task(other["task_id"]))["completed_at"]


async def test_a_scoped_reconciliation_does_not_clear_a_project_wide_change(orm_db):
    """Only the project-wide row covers a project-wide scope, as in covers()."""
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await record_reconciliation("p1", ["d1", "d2"], project_wide=False, read_seconds_ago=0)

    assert (await svc.get_deferred_rollouts("p1"))["count"] == 1


async def test_a_change_to_two_deployments_waits_until_both_are_reconciled(orm_db):
    svc = _svc()
    await _completed(
        svc,
        project="p1",
        task_type="add_component",
        payload={"name": "web", "deployment_names": ["d1", "d2"], "rollout": False},
    )
    await record_reconciliation("p1", ["d1"], project_wide=False, read_seconds_ago=0)
    assert (await svc.get_deferred_rollouts("p1"))["count"] == 1

    await record_reconciliation("p1", ["d2"], project_wide=False, read_seconds_ago=0)
    assert (await svc.get_deferred_rollouts("p1"))["count"] == 0


@pytest.mark.parametrize(
    ("deployment_name", "offset_us", "waiting"),
    [
        (None, 0, 0),
        (None, -1, 1),
        ("d1", 0, 0),
        ("d1", -1, 1),
    ],
    ids=[
        "project-wide-at-completion",
        "project-wide-just-before",
        "deployment-at-completion",
        "deployment-just-before",
    ],
)
async def test_a_run_that_read_the_file_at_the_moment_the_change_completed_saw_it(
    orm_db, deployment_name, offset_us, waiting
):
    """The read rule's boundary: waiting means reconciled strictly BEFORE completion."""
    svc = _svc()
    row = await _deferred_image(svc, "d1")
    completed_at = datetime.fromisoformat((await svc.get_task(row["task_id"]))["completed_at"])
    async with session_scope() as session:
        session.add(
            ProjectReconciliation(
                project_name="p1",
                deployment_name=deployment_name,
                reconciled_at=completed_at + timedelta(microseconds=offset_us),
            )
        )

    assert (await svc.get_deferred_rollouts("p1"))["count"] == waiting


async def test_a_reconciliation_of_another_project_clears_nothing(orm_db):
    svc = _svc()
    await _deferred_image(svc, "d1")
    await record_reconciliation("p2", ["d1"], project_wide=True, read_seconds_ago=0)

    assert (await svc.get_deferred_rollouts("p1"))["count"] == 1


async def test_a_running_rollout_is_reported_while_the_count_still_stands(orm_db):
    """De teller loopt pas terug als de uitrol klaar is, dus de melding moet het weten."""
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    refresh = await _create(svc, project="p1", deployment=None, task_type="refresh_project", payload={"f": False})
    await svc.start_task(refresh["task_id"])

    pending = await svc.get_deferred_rollouts("p1")
    assert pending["count"] == 1
    assert pending["rollout_in_progress"] is True


async def test_a_queued_rollout_already_counts_as_in_progress(orm_db):
    """Nog niet opgepakt is voor de lezer net zo goed onderweg."""
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await _create(svc, project="p1", deployment=None, task_type="refresh_project", payload={"f": False})

    assert (await svc.get_deferred_rollouts("p1"))["rollout_in_progress"] is True


async def test_any_processing_task_that_covers_the_drift_is_a_rollout_in_progress(orm_db):
    """Not only a refresh: update_component processes the whole project too."""
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await _create(svc, project="p1", deployment=None, task_type="update_component", payload={"name": "web"})

    assert (await svc.get_deferred_rollouts("p1"))["rollout_in_progress"] is True


async def test_a_restore_is_a_rollout_in_progress_on_its_deployment(orm_db):
    """A restore processes its target deployment again, so it clears drift there."""
    svc = _svc()
    await _deferred_image(svc, "d1")
    await _create(svc, project="p1", deployment="d1", task_type="restore", payload={"target_deployment": "d1"})

    assert (await svc.get_deferred_rollouts("p1"))["rollout_in_progress"] is True


async def test_a_scoped_processing_task_does_not_cover_project_wide_drift(orm_db):
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await _create(svc, project="p1", deployment="d1", task_type="refresh_deployment", payload={"f": 1})

    assert (await svc.get_deferred_rollouts("p1"))["rollout_in_progress"] is False


async def test_a_scoped_processing_task_covers_drift_on_its_own_deployment(orm_db):
    svc = _svc()
    await _deferred_image(svc, "d1")
    await _create(svc, project="p1", deployment="d1", task_type="refresh_deployment", payload={"f": 1})

    assert (await svc.get_deferred_rollouts("p1"))["rollout_in_progress"] is True


@pytest.mark.parametrize(
    ("waiting_scopes", "open_scope", "in_progress"),
    [
        ([["d1"], ["d2"]], ["d2"], False),
        ([["d1"], ["d2"]], ["d1", "d2"], True),
        ([None, ["d1"]], ["d1"], False),
        ([["d1"], None], ["d1"], False),
        ([None, ["d1"]], None, True),
    ],
    ids=[
        "covers-only-the-newest-change",
        "covers-the-union",
        "project-wide-change-then-scoped",
        "scoped-change-then-project-wide",
        "project-wide-covers-all",
    ],
)
async def test_an_open_task_is_in_progress_only_when_it_covers_every_waiting_change(
    orm_db, waiting_scopes, open_scope, in_progress
):
    """The open scope must cover the union of all waiting scopes, whatever their order."""

    def payload(scope: list[str] | None, **extra: object) -> dict:
        return {"name": "web", **({"deployment_names": scope} if scope else {}), **extra}

    svc = _svc()
    for scope in waiting_scopes:
        await _completed(svc, project="p1", task_type="add_component", payload=payload(scope, rollout=False))
    await _create(svc, project="p1", deployment=None, task_type="add_component", payload=payload(open_scope))

    pending = await svc.get_deferred_rollouts("p1")
    assert pending["count"] == len(waiting_scopes)
    assert pending["rollout_in_progress"] is in_progress


async def test_an_open_task_that_defers_its_own_rollout_is_not_in_progress(orm_db):
    svc = _svc()
    await _deferred_image(svc, "d1")
    await _create(svc, project="p1", deployment=None, task_type="update_component", payload={"rollout": False})

    assert (await svc.get_deferred_rollouts("p1"))["rollout_in_progress"] is False


async def test_no_rollout_in_progress_once_it_finished(orm_db):
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await _completed(svc, project="p1", task_type="refresh_project", payload={"force_clone": False})
    await record_reconciliation("p1", [], project_wide=True, read_seconds_ago=0)

    pending = await svc.get_deferred_rollouts("p1")
    assert pending["count"] == 0
    assert pending["rollout_in_progress"] is False


async def test_a_running_task_that_rolls_nothing_out_is_not_reported(orm_db):
    """Anders zou een slaapstand of een kloon een uitrol aankondigen die niet gebeurt."""
    svc = _svc()
    # Drift op dezelfde deployment: dan dekt de scope hem, en zegt alleen het taaktype nee.
    await _deferred_image(svc, "d1")
    other = await _create(svc, project="p1", deployment="d1", task_type="sleep_deployment", payload={"a": 1})
    await svc.start_task(other["task_id"])

    assert (await svc.get_deferred_rollouts("p1"))["rollout_in_progress"] is False


async def test_a_running_rollout_in_another_project_is_not_reported(orm_db):
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    refresh = await _create(svc, project="p2", deployment=None, task_type="refresh_project", payload={"f": False})
    await svc.start_task(refresh["task_id"])

    assert (await svc.get_deferred_rollouts("p1"))["rollout_in_progress"] is False


async def _age(task_id: str, hours: int) -> None:
    async with session_scope() as session:
        await session.execute(
            update(AsyncTask)
            .where(AsyncTask.id == uuid.UUID(task_id))
            .values(completed_at=func.now() - func.make_interval(0, 0, 0, 0, hours))
        )


async def test_cleanup_keeps_a_deferred_rollout_that_was_never_rolled_out(orm_db):
    """Drift that disappears after a week is exactly the silent drift this must surface."""
    svc = _svc()
    row = await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await _age(row["task_id"], 200)

    assert await svc.cleanup_old_tasks(retention_hours=168) == 0
    assert (await svc.get_deferred_rollouts("p1"))["count"] == 1


async def test_cleanup_removes_a_deferred_rollout_once_it_was_reconciled(orm_db):
    svc = _svc()
    deferred = await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await _age(deferred["task_id"], 200)
    await record_reconciliation("p1", [], project_wide=True, read_seconds_ago=0)

    assert await svc.cleanup_old_tasks(retention_hours=168) == 1


async def test_cleanup_keeps_a_deferred_rollout_whose_deployment_was_not_reconciled(orm_db):
    svc = _svc()
    kept = await _deferred_image(svc, "d2")
    cleared = await _deferred_image(svc, "d1")
    await _age(kept["task_id"], 200)
    await _age(cleared["task_id"], 200)
    await record_reconciliation("p1", ["d1"], project_wide=False, read_seconds_ago=0)

    assert await svc.cleanup_old_tasks(retention_hours=168) == 1
    assert await svc.get_task(kept["task_id"]) is not None
