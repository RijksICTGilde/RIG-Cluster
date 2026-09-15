"""Add project_reconciliation: what a run really reconciled, per deployment (RC-188).

Revision ID: 006
Revises: 005
Create Date: 2026-09-15
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS project_reconciliation (
            project_name VARCHAR(63) NOT NULL,
            deployment_name VARCHAR(63),
            reconciled_at TIMESTAMPTZ NOT NULL
        );
        """
    )
    # NULL is projectbreed en een gewone PK laat geen NULL toe, dus uniek op de COALESCE.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_project_reconciliation_scope "
        "ON project_reconciliation (project_name, COALESCE(deployment_name, ''));"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS project_reconciliation;")
