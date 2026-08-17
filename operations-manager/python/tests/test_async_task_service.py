"""Real-Postgres tests for the ORM-backed AsyncTaskService (RC-5 persistence)."""

import uuid

from opi.core.async_task_service import MAX_ERROR_MESSAGE_CHARS, AsyncTaskService
from opi.core.db import session_scope
from opi.services.persistence.async_tasks import AsyncTask
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
# ---------------------------------------------------------------------------


async def _completed(svc, *, project, task_type, payload):
    row = await _create(svc, project=project, deployment=None, task_type=task_type, payload=payload)
    await svc.complete_task(row["task_id"])
    return row


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


async def test_a_refresh_clears_everything_before_it(orm_db):
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await _completed(svc, project="p1", task_type="refresh_project", payload={"force_clone": False})

    assert (await svc.get_deferred_rollouts("p1"))["count"] == 0


async def test_a_change_deferred_after_the_refresh_still_counts(orm_db):
    svc = _svc()
    await _completed(svc, project="p1", task_type="refresh_project", payload={"force_clone": False})
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})

    pending = await svc.get_deferred_rollouts("p1")
    assert pending["count"] == 1
    assert pending["task_types"] == ["add_component"]


async def test_a_partial_rollout_does_not_clear_the_drift(orm_db):
    """refresh_deployment reconciles ONE deployment, so the rest of the file stays ahead."""
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await _completed(svc, project="p1", task_type="refresh_deployment", payload={"deployment_name": "dev"})

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


async def test_no_rollout_in_progress_once_it_finished(orm_db):
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    await _completed(svc, project="p1", task_type="refresh_project", payload={"force_clone": False})

    pending = await svc.get_deferred_rollouts("p1")
    assert pending["count"] == 0
    assert pending["rollout_in_progress"] is False


async def test_a_running_task_that_rolls_nothing_out_is_not_reported(orm_db):
    """Anders zou een slaapstand of een kloon een uitrol aankondigen die niet gebeurt."""
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    other = await _create(svc, project="p1", deployment="d1", task_type="sleep_deployment", payload={"a": 1})
    await svc.start_task(other["task_id"])

    assert (await svc.get_deferred_rollouts("p1"))["rollout_in_progress"] is False


async def test_a_running_rollout_in_another_project_is_not_reported(orm_db):
    svc = _svc()
    await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    refresh = await _create(svc, project="p2", deployment=None, task_type="refresh_project", payload={"f": False})
    await svc.start_task(refresh["task_id"])

    assert (await svc.get_deferred_rollouts("p1"))["rollout_in_progress"] is False


async def test_cleanup_keeps_a_deferred_rollout_that_was_never_rolled_out(orm_db):
    """Drift that disappears after a week is exactly the silent drift this must surface."""
    svc = _svc()
    row = await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    async with session_scope() as session:
        await session.execute(
            update(AsyncTask)
            .where(AsyncTask.id == uuid.UUID(row["task_id"]))
            .values(completed_at=func.now() - func.make_interval(0, 0, 0, 0, 200))
        )

    assert await svc.cleanup_old_tasks(retention_hours=168) == 0
    assert (await svc.get_deferred_rollouts("p1"))["count"] == 1


async def test_cleanup_removes_a_deferred_rollout_once_it_was_rolled_out(orm_db):
    svc = _svc()
    deferred = await _completed(svc, project="p1", task_type="add_component", payload={"name": "web", "rollout": False})
    rolled = await _completed(svc, project="p1", task_type="refresh_project", payload={"force_clone": False})
    # Both beyond the retention window, but the refresh strictly after the deferred change.
    async with session_scope() as session:
        await session.execute(
            update(AsyncTask)
            .where(AsyncTask.id == uuid.UUID(deferred["task_id"]))
            .values(completed_at=func.now() - func.make_interval(0, 0, 0, 0, 200))
        )
        await session.execute(
            update(AsyncTask)
            .where(AsyncTask.id == uuid.UUID(rolled["task_id"]))
            .values(completed_at=func.now() - func.make_interval(0, 0, 0, 0, 190))
        )

    assert await svc.cleanup_old_tasks(retention_hours=168) == 2
