"""Add details_purged_at to async_tasks: wanneer de werkgegevens zijn leeggemaakt (RC-282).

Revision ID: 007
Revises: 006
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Bestaande rijen blijven NULL en komen daardoor eenmalig door de leegmaakpas; een
    # backfill met een tijdstempel zou ze juist overslaan en hun werkgegevens laten staan.
    # Geen index: beide statements lopen op het bestaande idx_async_tasks_completed. De
    # leegmaakpas filtert daarbij de al leeggemaakte rijen weg, eens per uur. EXPLAIN op de PR.
    op.execute("ALTER TABLE async_tasks ADD COLUMN IF NOT EXISTS details_purged_at TIMESTAMPTZ;")


def downgrade() -> None:
    op.execute("ALTER TABLE async_tasks DROP COLUMN IF EXISTS details_purged_at;")
