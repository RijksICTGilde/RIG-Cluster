"""SQL schema for the async_tasks table.

The ASYNC_TASKS_TABLE_SQL constant is used by the Alembic baseline migration
(opi/migrations/versions/001_baseline.py) to create the table.
"""

ASYNC_TASKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS async_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_type VARCHAR(64) NOT NULL,
    project_name VARCHAR(63) NOT NULL,
    deployment_name VARCHAR(63),
    cluster VARCHAR(63) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    payload JSONB NOT NULL DEFAULT '{}',
    result JSONB,
    error_message TEXT,
    current_step VARCHAR(255) DEFAULT 'Queued',
    progress_percent SMALLINT DEFAULT 0,
    subtasks JSONB DEFAULT '[]',
    logs TEXT[],
    events JSONB,
    web_addresses JSONB,
    claimed_by VARCHAR(255),
    claimed_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_by VARCHAR(255),
    attempt_count SMALLINT NOT NULL DEFAULT 0,
    max_attempts SMALLINT NOT NULL DEFAULT 3,
    -- De deployments die deze taak raakt. NULL betekent projectbreed, net als None
    -- in scope_of(), dat de enige schrijver van deze kolom is.
    affects_deployments VARCHAR(63)[]
);

CREATE INDEX IF NOT EXISTS idx_async_tasks_pending
    ON async_tasks(status, created_at) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_async_tasks_heartbeat
    ON async_tasks(status, heartbeat_at) WHERE status IN ('claimed', 'running');

CREATE INDEX IF NOT EXISTS idx_async_tasks_project
    ON async_tasks(project_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_async_tasks_deployment
    ON async_tasks(project_name, deployment_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_async_tasks_completed
    ON async_tasks(status, completed_at) WHERE status IN ('completed', 'failed', 'cancelled');

CREATE INDEX IF NOT EXISTS idx_async_tasks_affects
    ON async_tasks USING GIN (affects_deployments);
"""
