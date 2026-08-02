"""RC-17 step 1: the postgresql-database `scope` field and the placement helper.

Covers the discriminated-union config model (shared is the default; a dedicated-only
field on a shared database fails loudly) and `opi/services/postgres_scope.py` (which
service means "dedicated", and reading the CNPG config from either source).
"""

from __future__ import annotations

import pytest
from opi.services.catalog.postgresql_database.config_model import PostgresqlDatabaseProjectConfig
from opi.services.postgres_scope import (
    get_dedicated_postgres_config,
    postgres_scope,
    project_uses_dedicated_postgres,
)
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from pydantic import ValidationError

PG = ServiceType.POSTGRESQL_DATABASE.value
NS_PG = ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value


# --- config model ---------------------------------------------------------------


def test_absent_scope_defaults_to_shared():
    cfg = PostgresqlDatabaseProjectConfig.model_validate({})
    assert cfg.root.scope == "shared"


def test_explicit_shared():
    cfg = PostgresqlDatabaseProjectConfig.model_validate({"scope": "shared"})
    assert cfg.root.scope == "shared"


def test_project_scope_carries_cnpg_defaults():
    cfg = PostgresqlDatabaseProjectConfig.model_validate({"scope": "project"})
    dumped = cfg.root.model_dump()
    assert dumped["scope"] == "project"
    assert dumped["instances"] == 1
    assert dumped["storage"] == "10Gi"
    assert dumped["image"].startswith("ghcr.io/cloudnative-pg/postgresql")


def test_project_scope_overrides_cnpg_field():
    cfg = PostgresqlDatabaseProjectConfig.model_validate({"scope": "project", "storage": "20Gi", "instances": 3})
    assert cfg.root.storage == "20Gi"
    assert cfg.root.instances == 3


def test_dedicated_only_field_on_shared_fails_loudly():
    # The whole point of the discriminated union: storage belongs to a dedicated
    # cluster, so it must be rejected on a shared database, not silently ignored.
    with pytest.raises(ValidationError) as exc:
        PostgresqlDatabaseProjectConfig.model_validate({"scope": "shared", "storage": "20Gi"})
    assert "storage" in str(exc.value)


def test_dedicated_only_field_without_scope_still_fails():
    # No scope -> defaults to shared -> still rejects the dedicated-only field.
    with pytest.raises(ValidationError):
        PostgresqlDatabaseProjectConfig.model_validate({"instances": 2})


def test_unknown_scope_value_rejected():
    with pytest.raises(ValidationError):
        PostgresqlDatabaseProjectConfig.model_validate({"scope": "deployment"})


def test_provider_validate_config_uses_the_project_model():
    provider = get_service(ServiceType.POSTGRESQL_DATABASE)
    assert provider.validate_config({"scope": "project", "storage": "5Gi"}).root.storage == "5Gi"
    assert provider.validate_config(None).root.scope == "shared"


# --- placement helper -----------------------------------------------------------


def _project(services: list) -> dict:
    return {"name": "proj", "services": services}


def test_scope_none_when_no_postgres():
    assert postgres_scope(_project(["publish-on-web"])) is None
    assert project_uses_dedicated_postgres(_project(["publish-on-web"])) is False


def test_bare_postgresql_database_is_shared():
    assert postgres_scope(_project(["postgresql-database"])) == "shared"
    assert project_uses_dedicated_postgres(_project(["postgresql-database"])) is False


def test_postgresql_database_scope_project_is_dedicated():
    proj = _project([{"name": PG, "config": {"scope": "project", "storage": "5Gi"}}])
    assert postgres_scope(proj) == "project"
    assert project_uses_dedicated_postgres(proj) is True


def test_postgresql_database_scope_shared_is_shared():
    proj = _project([{"name": PG, "config": {"scope": "shared"}}])
    assert postgres_scope(proj) == "shared"
    assert project_uses_dedicated_postgres(proj) is False


def test_namespace_postgresql_database_is_dedicated():
    proj = _project([{"name": NS_PG, "config": {"storage": "5Gi"}}])
    assert postgres_scope(proj) == "project"
    assert project_uses_dedicated_postgres(proj) is True


def test_get_dedicated_config_from_scope_project():
    proj = _project([{"name": PG, "config": {"scope": "project", "storage": "7Gi", "instances": 2}}])
    config = get_dedicated_postgres_config(proj)
    assert config["storage"] == "7Gi"
    assert config["instances"] == 2
    # scope is dropped so the result matches the namespace config field set exactly.
    assert "scope" not in config


def test_get_dedicated_config_from_namespace_service():
    proj = _project([{"name": NS_PG, "config": {"storage": "7Gi", "instances": 2}}])
    config = get_dedicated_postgres_config(proj)
    assert config["storage"] == "7Gi"
    assert config["instances"] == 2


def test_both_dedicated_sources_yield_the_same_field_set():
    # Same field set regardless of which service asked for a dedicated cluster.
    from_scope = get_dedicated_postgres_config(_project([{"name": PG, "config": {"scope": "project"}}]))
    from_namespace = get_dedicated_postgres_config(_project([{"name": NS_PG, "config": {}}]))
    assert set(from_scope) == set(from_namespace)
