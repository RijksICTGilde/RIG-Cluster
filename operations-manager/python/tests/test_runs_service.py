"""Real-Postgres tests for the ORM-backed RunsService (RC-5 persistence)."""

from opi.services.runs_service import RunKind, RunsService, RunStatus


async def _new(svc, session_id, *, project="p1", deployment="d1", cluster="c1"):
    row = await svc.create_run(
        kind=RunKind.DB_CONSOLE, session_id=session_id, cluster=cluster, project=project,
        deployment=deployment, namespace="ns", name=f"run-{session_id}", spec={"k": session_id},
        url=None, started_by=None, expires_at=None,
    )
    return row


async def test_create_defaults_and_spec_roundtrip(orm_db):
    svc = RunsService()
    row = await _new(svc, "s1")
    assert row["status"] == "starting"
    assert row["spec"] == {"k": "s1"}
    assert row["id"]
    assert row["started_at"]
    assert row["created_at"]


async def test_mark_running_sets_url_and_status(orm_db):
    svc = RunsService()
    await _new(svc, "s1")
    await svc.mark_running("s1", url="https://run/")
    latest = await svc.get_latest_run("p1", "d1", RunKind.DB_CONSOLE)
    assert latest["status"] == "running"
    assert latest["url"] == "https://run/"
    # idempotent: a second mark_running on a non-starting run is a no-op
    await svc.mark_running("s1", url="https://other/")
    assert (await svc.get_latest_run("p1", "d1", RunKind.DB_CONSOLE))["url"] == "https://run/"


async def test_list_runs_active_only_by_default(orm_db):
    svc = RunsService()
    await _new(svc, "active1")
    await _new(svc, "ended1")
    await svc.mark_ended("ended1", RunStatus.STOPPED)
    active = await svc.list_runs("p1")
    assert [r["session_id"] for r in active] == ["active1"]
    both = {r["session_id"] for r in await svc.list_runs("p1", include_ended=True)}
    assert both == {"active1", "ended1"}


async def test_list_active_runs_by_cluster(orm_db):
    svc = RunsService()
    await _new(svc, "a", cluster="c1")
    await _new(svc, "b", cluster="c2")
    await _new(svc, "c", cluster="c1")
    await svc.mark_ended("c", RunStatus.EXPIRED)
    names = sorted(r["session_id"] for r in await svc.list_active_runs("c1"))
    assert names == ["a"]
