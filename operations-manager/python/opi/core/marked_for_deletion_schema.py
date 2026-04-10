"""SQL schema for the marked_for_deletion table.

The MARKED_FOR_DELETION_TABLE_SQL constant is used by the Alembic migration
(opi/migrations/versions/002_add_marked_for_deletion.py) to create the table.

This table tracks persistent data resources (databases, buckets, PVCs) that have been
removed from project YAML files but should not be immediately deleted. Resources are
held for a configurable grace period before the reconciliation job purges them.
"""

MARKED_FOR_DELETION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS marked_for_deletion (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    resource_type VARCHAR(50) NOT NULL,
    resource_name VARCHAR(255) NOT NULL,
    project_name VARCHAR(255) NOT NULL,
    deployment_name VARCHAR(255) NOT NULL,
    cluster VARCHAR(100) NOT NULL,
    marked_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_marked_for_deletion_project
    ON marked_for_deletion(project_name);

CREATE INDEX IF NOT EXISTS idx_marked_for_deletion_marked_at
    ON marked_for_deletion(marked_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_marked_for_deletion_unique
    ON marked_for_deletion(resource_type, resource_name, cluster);
"""
