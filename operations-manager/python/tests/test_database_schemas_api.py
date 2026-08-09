"""Extra database schemas as their own sub-resource (RC-59).

RC-17 decided that a schema leaving the list must not take its data with it: removing it
*marks* it, the schema and its contents stay in PostgreSQL, and only its variable stops
being offered. That safety lived in a checkbox on a form. A client that reads the service
config, drops one schema and writes the whole block back loses it, and nothing in the
request schema says so -- which is precisely the position an agent is in.

These routes move the decision to where it cannot be skipped, so what is tested here is
which of the three outcomes a call produces:

* adding leaves the rest of the config alone;
* removing marks and keeps the entry;
* forgetting takes the entry out, and has to be asked for.

Git is mocked at ``save_and_commit_project``: the subject is the mutation, not the commit.
The cross-cutting rules (a duplicate postfix, the 63-character limit, a colliding variable
name) are deliberately NOT re-tested here -- they are enforced at save time and
``tests/forms`` covers them; this proves the route passes them on rather than repeating
them.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.services.postgres_scope import get_postgres_schemas, schema_is_marked
from opi.services.project import Project
from opi.services.services_enums import ServiceType
from pydantic import ValidationError

_SERVICE = ServiceType.POSTGRESQL_DATABASE.value


def _manager():
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.handlers.sops.SopsHandler"),
        patch("opi.generation.manifests.ManifestGenerator"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        from opi.manager.project_manager import ProjectManager

        return ProjectManager()


def _project(schemas: list[dict] | None = None, *, uses_database: bool = True) -> dict:
    services: list = ["publish-on-web"]
    if uses_database:
        config: dict = {"scope": "shared"}
        if schemas is not None:
            config["schemas"] = schemas
        services.append({"name": _SERVICE, "config": config})
    return {
        "schema-version": 2,
        "name": "demo",
        "services": services,
        "components": [{"name": "backend", "type": "single", "services": [_SERVICE]}],
        "deployments": [
            {
                "name": "deployment-1",
                "cluster": "local",
                "namespace": "demo",
                "components": [{"reference": "backend"}],
            }
        ],
    }


def _wire(project_data: dict):
    manager = _manager()
    manager.get_contents = AsyncMock(return_value=project_data)
    manager.get_name = AsyncMock(return_value="demo")
    save = AsyncMock()
    manager.save_and_commit_project = save
    return manager, save


def _run(manager, operation: str, postfix: str, **kwargs) -> dict:
    return asyncio.run(manager.manage_database_schemas(operation, postfix, **kwargs))


def _saved_schemas(save: AsyncMock) -> list[dict]:
    saved = save.await_args.args[0]
    return list(Project(saved).service_config(_SERVICE).get("schemas") or [])


# ---------------------------------------------------------------------------
# Adding
# ---------------------------------------------------------------------------


def test_adding_a_schema_appends_it_and_leaves_the_rest_of_the_config_alone() -> None:
    """The point of a sub-resource: one entry in, nothing else touched."""
    data = _project([{"postfix": "rapportage", "description": "Bestaand"}])
    manager, save = _wire(data)

    result = _run(manager, "add", "analyse", description="Voor de analisten")

    assert result["success"]
    assert result["changed"]
    assert result["created"]
    schemas = _saved_schemas(save)
    assert [entry["postfix"] for entry in schemas] == ["rapportage", "analyse"]
    assert schemas[1]["description"] == "Voor de analisten"
    saved = save.await_args.args[0]
    assert Project(saved).service_config(_SERVICE)["scope"] == "shared", "the rest of the config must survive"


def test_adding_the_first_schema_to_a_service_without_a_schema_list() -> None:
    manager, save = _wire(_project())

    result = _run(manager, "add", "rapportage")

    assert result["success"]
    assert [entry["postfix"] for entry in _saved_schemas(save)] == ["rapportage"]


def test_adding_a_postfix_that_is_already_there_is_a_conflict() -> None:
    manager, save = _wire(_project([{"postfix": "rapportage"}]))

    result = _run(manager, "add", "rapportage")

    assert not result["success"]
    assert result["error_type"] == "conflict"
    save.assert_not_awaited()


def test_adding_a_marked_postfix_brings_it_back() -> None:
    """Marking exists so the data can come back; adding the same postfix is how."""
    manager, save = _wire(_project([{"postfix": "rapportage", "marked-for-deletion": True}]))

    result = _run(manager, "add", "rapportage", description="Weer in gebruik")

    assert result["success"]
    assert result["restored"]
    assert not result["created"]
    entry = _saved_schemas(save)[0]
    assert not schema_is_marked(entry)
    assert entry["description"] == "Weer in gebruik"


def test_adding_to_a_project_without_the_database_service_is_a_not_found() -> None:
    manager, save = _wire(_project(uses_database=False))

    result = _run(manager, "add", "rapportage")

    assert not result["success"]
    assert result["error_type"] == "not_found"
    save.assert_not_awaited()


# ---------------------------------------------------------------------------
# Removing marks; forgetting is a second, explicit thing
# ---------------------------------------------------------------------------


def test_removing_marks_the_schema_and_keeps_the_entry() -> None:
    """The safety RC-17 designed, now the default of the endpoint instead of a checkbox."""
    manager, save = _wire(_project([{"postfix": "rapportage", "description": "Rapportage"}]))

    result = _run(manager, "remove", "rapportage")

    assert result["success"]
    assert result["changed"]
    assert result["marked"]
    assert not result["forgotten"]
    schemas = _saved_schemas(save)
    assert len(schemas) == 1, "removing must not drop the entry"
    assert schema_is_marked(schemas[0])
    assert schemas[0]["description"] == "Rapportage", "what the schema was for is still recorded"


def test_a_marked_schema_stops_being_offered_but_is_still_listed() -> None:
    """The two readers of the list disagree on purpose, and both are right."""
    data = _project([{"postfix": "rapportage", "marked-for-deletion": True}])

    assert get_postgres_schemas(data) == [], "provisioning stops managing a marked schema"
    assert [entry["postfix"] for entry in get_postgres_schemas(data, include_marked=True)] == ["rapportage"]


def test_forgetting_takes_the_entry_out_of_the_file() -> None:
    manager, save = _wire(_project([{"postfix": "rapportage"}, {"postfix": "analyse"}]))

    result = _run(manager, "remove", "rapportage", forget=True)

    assert result["success"]
    assert result["forgotten"]
    assert not result["marked"]
    assert [entry["postfix"] for entry in _saved_schemas(save)] == ["analyse"]


def test_removing_an_already_marked_schema_changes_nothing() -> None:
    manager, save = _wire(_project([{"postfix": "rapportage", "marked-for-deletion": True}]))

    result = _run(manager, "remove", "rapportage")

    assert result["success"]
    assert result["changed"] is False
    # A commit that changes nothing is not written.
    save.assert_not_awaited()


def test_removing_a_schema_that_is_not_there_is_a_not_found() -> None:
    manager, save = _wire(_project([{"postfix": "rapportage"}]))

    result = _run(manager, "remove", "analyse")

    assert not result["success"]
    assert result["error_type"] == "not_found"
    save.assert_not_awaited()


def test_a_save_time_refusal_comes_back_as_a_validation_error() -> None:
    """Uniqueness, the 63-character limit and colliding variables stay where they are."""
    manager, save = _wire(_project())
    save.side_effect = ProjectIntegrityError("Schema-postfix 'db' botst met een bestaande variabele.")

    result = _run(manager, "add", "db")

    assert not result["success"]
    assert result["error_type"] == "validation_error"
    assert "botst" in result["error"], "the caller gets the message of the check that refused"


def test_an_unknown_operation_is_refused_before_anything_is_written() -> None:
    manager, save = _wire(_project())

    result = _run(manager, "toggle", "rapportage")

    assert not result["success"]
    assert result["error_type"] == "invalid_request"
    save.assert_not_awaited()


# ---------------------------------------------------------------------------
# The list gives the facts a caller cannot work out itself
# ---------------------------------------------------------------------------


def _list(project_data: dict):
    from opi.api.v2.router import list_database_schemas_v2

    store = MagicMock()
    store.get.return_value = MagicMock(data=project_data)
    with patch("opi.api.v2.router.get_project_store", return_value=store):
        # validate_api_token wraps the endpoint; __wrapped__ is the endpoint itself, and
        # the auth guard is the subject of tests/integration, not of this one.
        endpoint = getattr(list_database_schemas_v2, "__wrapped__", list_database_schemas_v2)
        return asyncio.run(endpoint(MagicMock(), "demo"))


def test_the_list_gives_the_full_name_per_deployment_and_the_variable() -> None:
    """The reason this is a route instead of a pointer at the config.

    A caller reading the config sees `postfix: rapportage` and cannot get from there to
    the schema its queries have to name, nor to the variable its container receives,
    without knowing naming rules that live in the platform.
    """
    listed = _list(_project([{"postfix": "rapportage"}]))

    assert listed.project == "demo"
    entry = listed.schemas[0]
    assert entry.postfix == "rapportage"
    assert entry.variable_name == "DATABASE_SCHEMA_RAPPORTAGE"
    assert [d.deployment for d in entry.deployments] == ["deployment-1"]
    assert entry.deployments[0].schema_name == "demo_deployment_1_rapportage"


def test_the_list_shows_a_marked_schema_as_marked_instead_of_hiding_it() -> None:
    """Leaving it out would read as "gone", and it is not gone: the data is still there."""
    listed = _list(_project([{"postfix": "rapportage", "marked-for-deletion": True}]))

    assert [entry.postfix for entry in listed.schemas] == ["rapportage"]
    assert listed.schemas[0].marked_for_deletion is True


def test_a_project_without_schemas_lists_none() -> None:
    assert _list(_project()).schemas == []


def test_the_request_model_carries_the_same_postfix_rule_as_the_stored_model() -> None:
    """One definition of a valid postfix, so a 422 here and a refusal at save agree."""
    from opi.api.v2.router import AddDatabaseSchemaRequest
    from opi.services.catalog.postgresql_database.config_model import SchemaEntry

    assert [repr(m) for m in AddDatabaseSchemaRequest.model_fields["postfix"].metadata] == [
        repr(m) for m in SchemaEntry.model_fields["postfix"].metadata
    ]
    with pytest.raises(ValidationError, match="postfix"):
        AddDatabaseSchemaRequest(postfix="Rapportage")
    with pytest.raises(ValidationError, match="postfix"):
        AddDatabaseSchemaRequest(postfix="1e-schema")
