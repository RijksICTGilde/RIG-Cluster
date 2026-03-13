"""Baseline: create async_tasks and subdomain_registry tables.

Revision ID: 001
Revises: None
Create Date: 2026-03-02
"""

from collections.abc import Sequence

from alembic import op
from opi.connectors.subdomain import SUBDOMAIN_REGISTRY_TABLE_SQL
from opi.core.async_task_schema import ASYNC_TASKS_TABLE_SQL

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(ASYNC_TASKS_TABLE_SQL)
    op.execute(SUBDOMAIN_REGISTRY_TABLE_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS async_tasks;")
    op.execute("DROP TABLE IF EXISTS subdomain_registry;")
