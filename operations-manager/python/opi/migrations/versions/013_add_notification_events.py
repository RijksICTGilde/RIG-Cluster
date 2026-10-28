"""Add notification_events: de gebeurtenissentabel, met de audit als eerste schrijver.

Het datamodel komt uit ``features/futures/gebeurtenissen-vastleggen-en-melden.md`` punt 7.
De tabel is alleen-invoegen; de enige toegestane wijziging is de pseudonimisering van
``actor`` en ``origin`` door de bewaarlus (punt 6, ``opi/services/audit.py``).

Nummer 013 en volgt op 012 (otp_herlogins), de head op main toen dit geschreven werd.

Revision ID: 013
Revises: 012
Create Date: 2026-10-28
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS notification_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source VARCHAR(255) NOT NULL,
    specversion VARCHAR(8) NOT NULL DEFAULT '1.0',
    type VARCHAR(128) NOT NULL,
    subject VARCHAR(255),
    time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    category VARCHAR(32) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    cluster VARCHAR(63) NOT NULL,
    project VARCHAR(63),
    deployment VARCHAR(63),
    component VARCHAR(63),
    actor VARCHAR(255),
    actor_kind VARCHAR(16) NOT NULL,
    origin VARCHAR(64),
    summary VARCHAR(255) NOT NULL,
    flow_id VARCHAR(64),
    task_id UUID,
    thread_key VARCHAR(255),
    dedup_key VARCHAR(160) NOT NULL,
    data JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_notification_events_dedup
    ON notification_events (dedup_key, time);
CREATE INDEX IF NOT EXISTS idx_notification_events_project
    ON notification_events (project, time DESC);
CREATE INDEX IF NOT EXISTS idx_notification_events_thread
    ON notification_events (thread_key, time DESC);
CREATE INDEX IF NOT EXISTS idx_notification_events_actor_sweep
    ON notification_events (time) WHERE actor IS NOT NULL OR origin IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notification_events_retention
    ON notification_events (time);
"""

DROP_SQL = "DROP TABLE IF EXISTS notification_events;"


def upgrade() -> None:
    op.execute(CREATE_SQL)


def downgrade() -> None:
    op.execute(DROP_SQL)
