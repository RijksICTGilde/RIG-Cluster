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
import json
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.api.task_models import TASK_RESULT_MODELS
from opi.api.v2.router import AddDatabaseSchemaRequest, v2_router
from opi.core.async_task_service import TaskType
from opi.core.project_schema import ProjectIntegrityError, validate_project_schema
from opi.core.task_rollout import DEFERRABLE_TASK_TYPES
from opi.forms.editables.validators import SchemaPostfixValidator
from opi.manager.project_validation import validate_project_structure, validate_service_configs
from opi.services.postgres_scope import get_postgres_schemas, schema_is_marked
from opi.services.project import Project
from opi.services.services_enums import ServiceType
from opi.utils.naming import SCHEMA_POSTFIX_MAX_LENGTH, SCHEMA_POSTFIX_PATTERN, generate_database_schema
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


def test_the_list_begins_with_the_default_schema() -> None:
    """The schema most callers mean, and the one a naive list leaves out.

    It is nowhere in the project file -- it follows from the project and deployment name --
    so a list built from the `schemas:` block alone omits exactly the entry a caller is
    most likely looking for.
    """
    listed = _list(_project([{"postfix": "rapportage"}]))

    default = listed.schemas[0]
    assert default.is_default is True
    assert default.postfix == ""
    assert default.variable_name == "DATABASE_SCHEMA"
    assert default.aliases == ["APP_DATABASE_SCHEMA"]
    assert default.deployments[0].schema_name == "demo_deployment_1"
    assert default.marked_for_deletion is False
    assert default.description, "the default row says what it is"


def test_the_list_gives_the_full_name_per_deployment_and_the_variable() -> None:
    """The reason this is a route instead of a pointer at the config.

    A caller reading the config sees `postfix: rapportage` and cannot get from there to
    the schema its queries have to name, nor to the variable its container receives,
    without knowing naming rules that live in the platform.
    """
    listed = _list(_project([{"postfix": "rapportage"}]))

    assert listed.project == "demo"
    entry = listed.schemas[1]
    assert entry.postfix == "rapportage"
    assert entry.is_default is False
    assert entry.variable_name == "DATABASE_SCHEMA_RAPPORTAGE"
    assert entry.aliases == ["APP_DATABASE_SCHEMA_RAPPORTAGE"]
    assert [d.deployment for d in entry.deployments] == ["deployment-1"]
    assert entry.deployments[0].schema_name == "demo_deployment_1_rapportage"


def test_the_names_are_computed_by_the_platform_not_pasted_together() -> None:
    """The two kinds part company at PostgreSQL's 63-character limit, and that is the point.

    A caller that joins project, deployment and postfix with underscores gets a name that
    does not exist for exactly the long names where it matters: the default is silently
    truncated there, an extra schema does not get created at all.
    """
    long_deployment = "d" * 60
    data = _project([{"postfix": "rapportage"}])
    data["deployments"] = [{"name": long_deployment, "cluster": "local", "namespace": "demo", "components": []}]

    listed = _list(data)

    default_name = listed.schemas[0].deployments[0].schema_name
    assert default_name == generate_database_schema("demo", long_deployment)
    assert len(default_name) == 63, "the default is truncated rather than refused"
    assert listed.schemas[1].deployments[0].schema_name is None, (
        "an extra schema that does not fit has no name, and saying one anyway would be a lie"
    )


def test_the_list_shows_a_marked_schema_as_marked_instead_of_hiding_it() -> None:
    """Leaving it out would read as "gone", and it is not gone: the data is still there."""
    listed = _list(_project([{"postfix": "rapportage", "marked-for-deletion": True}]))

    assert [entry.postfix for entry in listed.schemas] == ["", "rapportage"]
    assert listed.schemas[1].marked_for_deletion is True


def test_a_project_without_extra_schemas_still_lists_the_default() -> None:
    listed = _list(_project())

    assert len(listed.schemas) == 1
    assert listed.schemas[0].is_default is True


def test_a_project_without_the_database_service_has_no_schemas_at_all() -> None:
    """No database means no schemas -- not even the derived default one.

    The default row is computed from project and deployment name, so it comes out looking
    real for any project at all. Returning it here would invent a schema name, a variable
    and a description for a database that does not exist, and would contradict the write
    routes on the same sub-resource, which answer ``not_found`` for the same project.

    404 rather than an empty list: an empty list reads as "this database has no extra
    schemas", and there is no database.
    """
    with pytest.raises(HTTPException) as excinfo:
        _list(_project(uses_database=False))

    assert excinfo.value.status_code == 404
    assert _SERVICE in str(excinfo.value.detail)


# ---------------------------------------------------------------------------
# What the spec promises is what the routes do
# ---------------------------------------------------------------------------


def _schema_operations() -> dict[str, dict]:
    from opi.server import app

    paths = app.openapi()["paths"]
    base = f"/api/v2/projects/{{project_name}}/services/{_SERVICE}/schemas"
    return {
        "get": paths[base]["get"],
        "post": paths[base]["post"],
        "delete": paths[f"{base}/{{postfix}}"]["delete"],
    }


@pytest.mark.parametrize("method", ["get", "post", "delete"])
def test_a_status_code_named_in_the_description_is_one_the_route_can_actually_return(method: str) -> None:
    """A promised status code that never comes is worse than no promise at all.

    These routes are written for a caller that branches on the answer. If the description
    says a duplicate postfix is refused with 409 while the route only ever accepts the
    request and puts the refusal in the task result, that caller branches on a 409 it will
    never see and never reads the result that holds the actual refusal.
    """
    operation = _schema_operations()[method]
    declared = set(operation["responses"])
    promised = set(re.findall(r"\b[45]\d\d\b", operation["description"]))

    assert promised <= declared, f"{method} promises {sorted(promised - declared)} but declares {sorted(declared)}"


def test_the_add_description_points_at_the_task_result_for_every_asynchronous_refusal() -> None:
    """The refusals live at save time, so the endpoint cannot answer them with a status code.

    That is a deliberate choice -- uniqueness, the 63-character limit and colliding
    variable names have one home, the save path -- but it means a 202 does not mean the
    schema exists. The description has to say which `error_type` values come back, or the
    caller has no way to tell the three outcomes apart.
    """
    description = _schema_operations()["post"]["description"]

    for error_type in ("conflict", "not_found", "validation_error"):
        assert error_type in description
    assert "/api/tasks/{task_id}" in description


def test_the_default_schema_cannot_be_addressed_for_removal() -> None:
    """It has no postfix, so there is no path that names it -- and none that removes it."""
    routes = {
        route.path
        for route in v2_router.routes
        if "schemas" in getattr(route, "path", "") and "DELETE" in getattr(route, "methods", set())
    }
    assert routes == {"/api/v2/projects/{project_name}/services/postgresql-database/schemas/{postfix}"}

    manager, save = _wire(_project([{"postfix": "rapportage"}]))
    result = _run(manager, "remove", "")

    assert not result["success"], "an empty postfix addresses nothing"
    save.assert_not_awaited()


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


def test_adding_a_schema_answers_with_the_names_it_will_carry() -> None:
    """Adding is a real action, and a caller should not have to ask twice.

    The full name and the variable follow from the postfix and the project, so they are
    known the moment the request is accepted. Handing them back is what saves a caller
    from reconstructing them -- which is exactly the thing it cannot do safely.
    """
    from opi.api.v2.router import AddDatabaseSchemaRequest, add_database_schema_v2

    data = _project()
    store = MagicMock()
    store.get.return_value = MagicMock(data=data)
    with (
        patch("opi.api.v2.router.get_project_store", return_value=store),
        patch("opi.api.v2.router.create_async_task", new=AsyncMock(return_value={"task_id": "abc"})),
    ):
        endpoint = add_database_schema_v2.__wrapped__
        response = asyncio.run(
            endpoint(MagicMock(), "demo", AddDatabaseSchemaRequest(postfix="rapportage", description="R"))
        )

    body = json.loads(response.body)
    assert response.status_code == 202
    assert body["task_id"] == "abc"
    assert body["schema"]["postfix"] == "rapportage"
    assert body["schema"]["variable_name"] == "DATABASE_SCHEMA_RAPPORTAGE"
    assert body["schema"]["deployments"] == [
        {"deployment": "deployment-1", "schema_name": "demo_deployment_1_rapportage"}
    ]


# ---------------------------------------------------------------------------
# What the write leaves behind, and whether anything runs it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "kwargs"),
    [("add", {"description": "R"}), ("remove", {}), ("remove", {"forget": True})],
    ids=["add", "mark", "forget"],
)
def test_the_project_file_a_write_leaves_behind_is_valid(operation: str, kwargs: dict) -> None:
    """The plan's own check: adding a schema and taking it away again leaves a valid file.

    ``save_and_commit_project`` is mocked in these tests, so the validation it runs is not
    exercised by them. Here the produced project data goes through those same two
    chokepoints for real -- the JSON schema and the per-service config models -- so a write
    that would be refused on save cannot pass unnoticed here.
    """
    manager, save = _wire(_project([{"postfix": "rapportage", "description": "Rapportage"}]))

    result = _run(manager, operation, "rapportage" if operation == "remove" else "analyse", **kwargs)

    assert result["success"]
    saved = save.await_args.args[0]
    validate_project_schema(saved)
    validate_service_configs(saved)


def test_the_schema_task_type_is_wired_to_a_handler_everywhere_it_runs() -> None:
    """A task nobody handles is accepted with a 202 and then sits in the queue forever.

    Two processes run tasks -- the API server in combined mode and the standalone worker --
    and each registers its own handlers. Registering in one and forgetting the other is a
    failure that only shows on whichever deployment happens to run the other one.
    """
    assert TaskType.MANAGE_DATABASE_SCHEMAS in TASK_RESULT_MODELS

    for module in ("opi/server.py", "opi/worker_main.py"):
        source = pathlib.Path(module).read_text(encoding="utf-8")
        assert "handle_manage_database_schemas" in source, f"{module} does not import the handler"
        assert "TaskType.MANAGE_DATABASE_SCHEMAS, handle_manage_database_schemas" in source, (
            f"{module} imports the handler but never registers it"
        )


def test_a_schema_write_may_be_saved_without_rolling_out() -> None:
    """It only writes to the project file, so a later refresh reconciles it faithfully."""
    assert "manage_database_schemas" in DEFERRABLE_TASK_TYPES


# ---------------------------------------------------------------------------
# The naming rule itself (RC-59 feedback)
# ---------------------------------------------------------------------------


def test_the_postfix_rule_has_one_definition_that_all_three_roads_read() -> None:
    """The model, the form validator and the API endpoint apply the same rule.

    Three roads lead to a postfix now that an API can add one: the wizard, the config
    model and `POST .../schemas`. Three copies of "what a valid postfix looks like" agree
    on the day they are written and not much longer.
    """
    from opi.api.v2.router import AddDatabaseSchemaRequest
    from opi.services.catalog.postgresql_database.config_model import SchemaEntry

    for model in (SchemaEntry, AddDatabaseSchemaRequest):
        metadata = repr(model.model_fields["postfix"].metadata)
        assert SCHEMA_POSTFIX_PATTERN in metadata, f"{model.__name__} does not carry the shared pattern"
        assert str(SCHEMA_POSTFIX_MAX_LENGTH) in metadata, f"{model.__name__} does not carry the shared max length"


def test_a_postfix_that_is_far_too_long_fails_as_what_it_is() -> None:
    """It fails for its length, not as a complaint about a composed name nobody wrote.

    The composed check stays: how much room a postfix has depends on the project and
    deployment names, so no fixed maximum can promise a fit. This only makes the ordinary
    mistake legible.
    """
    long_postfix = "r" * (SCHEMA_POSTFIX_MAX_LENGTH + 1)

    assert SchemaPostfixValidator().validate(long_postfix) == [
        f"Een postfix mag hoogstens {SCHEMA_POSTFIX_MAX_LENGTH} tekens lang zijn"
    ]
    with pytest.raises(ValidationError, match="at most"):
        AddDatabaseSchemaRequest(postfix=long_postfix)


def test_an_uppercase_postfix_is_refused_and_never_quietly_lowercased() -> None:
    """`Rapportage` is a 422, not a schema that silently ends up called `rapportage`.

    Normalising would store something other than what was asked for, and the caller would
    find out from the schema name in their database rather than from the response.
    """
    with pytest.raises(ValidationError) as excinfo:
        AddDatabaseSchemaRequest(postfix="Rapportage")

    assert SCHEMA_POSTFIX_PATTERN in str(excinfo.value), "the reason names the rule that was broken"
    assert SchemaPostfixValidator().validate("Rapportage"), "the form road refuses it too"


def test_adding_a_deployment_is_refused_when_it_breaks_an_existing_schema_name() -> None:
    """The real hole: a postfix that fits today stops fitting when a deployment arrives.

    The enforcer that owns this rule only ran when the schema LIST was being saved, and
    only against the deployments that existed then. Nothing on the add-a-deployment road
    looked at schemas, so the failure surfaced at rollout instead -- as a ValueError deep
    in name generation, long after the change that caused it.
    """
    data = _project([{"postfix": "rapportage"}])
    data["deployments"].append({"name": "d" * 55, "cluster": "local", "namespace": "demo", "components": []})

    with pytest.raises(ProjectIntegrityError, match="rapportage"):
        asyncio.run(validate_project_structure(data))


def test_a_project_whose_schemas_all_fit_still_validates() -> None:
    asyncio.run(validate_project_structure(_project([{"postfix": "rapportage"}])))


def test_a_schema_on_its_way_out_does_not_block_a_new_deployment() -> None:
    """A marked schema is going away; refusing a save over it would trap the project."""
    data = _project([{"postfix": "r" * 30, "marked-for-deletion": True}])
    data["deployments"].append({"name": "d" * 55, "cluster": "local", "namespace": "demo", "components": []})

    asyncio.run(validate_project_structure(data))
