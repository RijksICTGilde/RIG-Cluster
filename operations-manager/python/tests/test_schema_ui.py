"""RC-17 step 3: schema-list UI validation.

The per-field postfix validator (format) and the section enforcer (uniqueness, the
63-char full-name limit and variable-name collisions -- the plan's "naming fails loudly"
invariant, at save time).
"""

from __future__ import annotations

import pytest
from opi.forms.editables.enforcers import FieldError, UniqueSchemaEnforcer
from opi.forms.editables.validators import SchemaPostfixValidator

# --- postfix validator ----------------------------------------------------------


def test_postfix_validator_accepts_valid():
    assert SchemaPostfixValidator().validate("rapportage") == []
    assert SchemaPostfixValidator().validate("audit_2024") == []


@pytest.mark.parametrize("bad", ["", "Rapportage", "1abc", "with-hyphen", "with space"])
def test_postfix_validator_rejects_invalid(bad):
    assert SchemaPostfixValidator().validate(bad) != []


# --- enforcer -------------------------------------------------------------------


def _project(schemas: list[dict], deployments: list[str] | None = None) -> dict:
    return {
        "name": "proj",
        "services": [{"name": "postgresql-database", "config": {"schemas": schemas}}],
        "deployments": [{"name": d} for d in (deployments or ["dep"])],
    }


async def _enforce(project: dict):
    return await UniqueSchemaEnforcer().enforce(project, {"project_name": "proj"})


@pytest.mark.asyncio
async def test_enforcer_accepts_unique_schemas():
    project = _project([{"postfix": "rapportage"}, {"postfix": "audit"}])
    assert await _enforce(project) is project


@pytest.mark.asyncio
async def test_enforcer_rejects_duplicate_postfix():
    project = _project([{"postfix": "rapportage"}, {"postfix": "rapportage"}])
    with pytest.raises(FieldError) as exc:
        await _enforce(project)
    assert exc.value.field_path == "services/postgresql-database/config/schemas[1]/postfix"


@pytest.mark.asyncio
async def test_enforcer_skips_marked_for_deletion():
    # A marked duplicate must not block the save: it is on its way out.
    project = _project([{"postfix": "rapportage"}, {"postfix": "rapportage", "marked-for-deletion": True}])
    assert await _enforce(project) is project


@pytest.mark.asyncio
async def test_enforcer_rejects_too_long_full_name():
    # project 'proj' + a long deployment name + postfix must exceed 63 chars.
    project = _project([{"postfix": "rapportage"}], deployments=["d" * 55])
    with pytest.raises(FieldError, match="63 tekens"):
        await _enforce(project)


@pytest.mark.asyncio
async def test_enforcer_rejects_variable_name_collision(monkeypatch):
    # The reserved-name guard: if a postfix's variable collides with a reserved database
    # variable, saving fails. (With today's base variables the DATABASE_SCHEMA_ prefix
    # cannot collide, so the guard is exercised by extending the reserved set.)
    from opi.services.catalog.postgresql_database import variables as postgres_variables

    monkeypatch.setattr(postgres_variables, "reserved_database_variable_names", lambda: {"DATABASE_SCHEMA_RAPPORTAGE"})
    project = _project([{"postfix": "rapportage"}])
    with pytest.raises(FieldError, match="botst"):
        await _enforce(project)
