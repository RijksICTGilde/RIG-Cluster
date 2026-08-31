"""Add affects_deployments to async_tasks: the deployment scope of a task, stored.

Revision ID: 005
Revises: 004
Create Date: 2026-08-31
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL betekent projectbreed, net als None in scope_of(). Bestaande rijen blijven NULL
    # en zijn daarmee maximaal blokkerend - de veilige kant voor de handvol taken die
    # tijdens een upgrade openstaan.
    op.execute("ALTER TABLE async_tasks ADD COLUMN IF NOT EXISTS affects_deployments VARCHAR(63)[];")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_async_tasks_affects ON async_tasks USING GIN (affects_deployments);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_async_tasks_affects;")
    op.execute("ALTER TABLE async_tasks DROP COLUMN IF EXISTS affects_deployments;")
