"""Regression tests for the new-project template.

New projects must be created at ``LATEST_SCHEMA_VERSION_VALUE``. If the template emits an
older version, ``process_project`` migrates the just-created file on its first read
and commits that migration back, which races the create push and fails the whole
provisioning task with a ``ConflictError`` (no database/keycloak/minio/... ever gets
provisioned). This guards that the create path emits the current schema.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.forms.editables import template as template_module
from opi.forms.editables.template import load_project_template
from opi.services.schema_migration import LATEST_SCHEMA_VERSION_VALUE, migrate_to_latest
from opi.utils.age import decrypt_password_smart_sync, ensure_base64_age_sync
from opi.utils.sops import generate_sops_key_pair


@pytest.fixture
def platform_keys(monkeypatch: Any) -> tuple[str, str]:
    """A real AGE keypair, plus a repository password that the env carries plain."""
    private_key, public_key = generate_sops_key_pair()
    monkeypatch.setattr(template_module.settings, "SOPS_AGE_PRIVATE_KEY", private_key)
    monkeypatch.setattr(template_module.settings, "SOPS_AGE_PUBLIC_KEY", public_key)
    monkeypatch.setattr(template_module.settings, "PROJECT_REPO_PASSWORD", "plain:repo-secret")
    return private_key, public_key


def test_template_stamps_latest_schema_version(platform_keys: tuple[str, str]) -> None:
    """The loaded template is stamped with the current schema version."""
    template = load_project_template()
    assert template["schema-version"] == LATEST_SCHEMA_VERSION_VALUE


def test_template_needs_no_migration(platform_keys: tuple[str, str]) -> None:
    """A freshly loaded template does not migrate - so no auto-migrate commit fires
    on create. ``migrate_project_schema`` reports whether it changed anything."""
    template = load_project_template()
    _, was_migrated = migrate_to_latest(template)
    assert not was_migrated, "new-project template should already be at the latest schema"


def test_template_never_carries_a_plain_repository_password(platform_keys: tuple[str, str]) -> None:
    """The create wizard writes the template into the project file, so a plain env value must not survive it."""
    private_key, _ = platform_keys
    password = load_project_template()["repositories"][0]["password"]
    assert password.startswith("base64+age:")
    assert decrypt_password_smart_sync(password, private_key) == "repo-secret"


def test_template_keeps_an_already_encrypted_repository_password(
    platform_keys: tuple[str, str], monkeypatch: Any
) -> None:
    """A base64+age env value goes in byte-identical, so existing project files render the same."""
    private_key, public_key = platform_keys
    encrypted = ensure_base64_age_sync("plain:repo-secret", private_key, public_key)
    monkeypatch.setattr(template_module.settings, "PROJECT_REPO_PASSWORD", encrypted)
    assert load_project_template()["repositories"][0]["password"] == encrypted
