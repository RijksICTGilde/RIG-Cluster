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
    # Backfill: de uitkomst van de oude meting als projectbrede rij, zodat de teller op het
    # omschakelmoment hetzelfde leest. De taaktypes staan er letterlijk, want de constante
    # waar de oude meting op leunde bestaat niet meer.
    op.execute(
        """
        INSERT INTO project_reconciliation (project_name, deployment_name, reconciled_at)
        SELECT project_name, NULL, max(coalesce(started_at, completed_at))
        FROM async_tasks
        WHERE status = 'completed'
          AND task_type IN ('refresh_project', 'delete_component')
          AND (payload ->> 'rollout') IS DISTINCT FROM 'false'
        GROUP BY project_name
        HAVING max(coalesce(started_at, completed_at)) IS NOT NULL
        ON CONFLICT (project_name, COALESCE(deployment_name, '')) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS project_reconciliation;")
