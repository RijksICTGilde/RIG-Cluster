"""Regression tests for the new-project template.

New projects must be created at ``LATEST_SCHEMA_VERSION``. If the template emits an
older version, ``process_project`` migrates the just-created file on its first read
and commits that migration back, which races the create push and fails the whole
provisioning task with a ``ConflictError`` (no database/keycloak/minio/... ever gets
provisioned). This guards that the create path emits the current schema.
"""

from __future__ import annotations

from opi.forms.editables.template import load_project_template
from opi.services.schema_migration import LATEST_SCHEMA_VERSION, migrate_to_latest


def test_template_stamps_latest_schema_version() -> None:
    """The loaded template is stamped with the current schema version."""
    template = load_project_template()
    assert template["schema-version"] == LATEST_SCHEMA_VERSION


def test_template_needs_no_migration() -> None:
    """A freshly loaded template does not migrate - so no auto-migrate commit fires
    on create. ``migrate_project_schema`` reports whether it changed anything."""
    template = load_project_template()
    _, was_migrated = migrate_to_latest(template)
    assert not was_migrated, "new-project template should already be at the latest schema"
