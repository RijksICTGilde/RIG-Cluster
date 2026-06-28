"""SQL schema for the `runs` registry table.

A "run" is a user-launched, label-tracked, TTL-reaped Kubernetes workload that
OPI provisions on request and monitors (vs async_tasks, which the worker
executes in-process). It is the administration/history record for such
workloads. The first kind is the ephemeral database console; ad-hoc job pods
(image + command, with logs) are a planned second kind, hence the generic
`kind` + `spec` columns.

Live status always comes from the cluster (real pod state); this table records
the lifecycle milestones (created -> running -> ended) and the audit trail.

The RUNS_TABLE_SQL constant is executed by the Alembic migration
(opi/migrations/versions/004_add_runs.py).
"""

RUNS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind VARCHAR(32) NOT NULL,
    session_id VARCHAR(32) NOT NULL,
    cluster VARCHAR(63) NOT NULL,
    project VARCHAR(63) NOT NULL,
    deployment VARCHAR(63),
    namespace VARCHAR(63) NOT NULL,
    name VARCHAR(63) NOT NULL,
    spec JSONB NOT NULL DEFAULT '{}',
    status VARCHAR(20) NOT NULL DEFAULT 'starting',
    url TEXT,
    started_by VARCHAR(255),
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    ended_by VARCHAR(255),
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One live row per session/bundle; teardown sets a terminal status (it is not deleted).
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);

-- Active runs on this cluster (used by the reaper for TTL-driven teardown).
CREATE INDEX IF NOT EXISTS idx_runs_active
    ON runs(cluster, status) WHERE status IN ('starting', 'running');

-- Per-project history (admin/list views), newest first.
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project, started_at DESC);
"""
