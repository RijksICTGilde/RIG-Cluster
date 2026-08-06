"""Declared service actions: the contract, and the routes derived from it (RC-38).

An action is how a service says "I can do something the wizard has no field for". The
declaration is the single source: fields and their meaning, verbs and what each promises
about an id that already exists, valid field combinations, an example. Route, multipart
signature and OpenAPI documentation are generated from it.

Two things are measured here. First that a declaration is honest -- every field either
points at the shared Editable that already validates it or writes down why there is none
(the rule from RC-26: the API never restates a validation rule), and every combination
points at code that exists. Second that the generation actually produces what the
declaration promises: the routes, the verbs on them, and the field descriptions in the
OpenAPI document, which is where an API client reads what a field means.
"""

from __future__ import annotations

import importlib

import pytest
from opi.services.catalog.actions import ActionField, ActionFieldKind, ActionVerb, FieldCombination, ServiceAction
from opi.services.catalog.attachments.api import COMPONENT_ATTACHMENT_ACTION, PROJECT_ATTACHMENT_ACTION
from opi.services.catalog.attachments.editables import ATTACHMENT_ID_EDITABLE
from opi.services.catalog.base import ConfigLayer, ConfigRole
from opi.services.registry import SERVICES

_ACTIONS: list[tuple[str, ServiceAction]] = [
    (service_type.value, action) for service_type, service in SERVICES.items() for action in service.api_actions()
]
_IDS = [f"{name}:{action.action_id}:{action.layer.value}" for name, action in _ACTIONS]


def _resolve(dotted: str):
    """Resolve a dotted path to the object it names, or raise."""
    module, _, attribute = dotted.rpartition(".")
    return getattr(importlib.import_module(module), attribute)


class TestEveryDeclarationIsHonest:
    @pytest.mark.parametrize(("service_name", "action"), _ACTIONS, ids=_IDS)
    def test_a_field_reuses_an_editable_or_says_why_not(self, service_name: str, action: ServiceAction) -> None:
        # The RC-26 rule: opi/api/validation.py builds no Editables of its own, it points
        # at the shared ones. An action field that carries its own rule would be the
        # second definition of that rule, and the two drift apart silently.
        for action_field in action.fields:
            assert action_field.editable is not None or action_field.no_editable_reason, (
                f"{service_name}/{action.action_id}: field '{action_field.name}' neither reuses an "
                f"editable nor explains why it cannot"
            )

    @pytest.mark.parametrize(("service_name", "action"), _ACTIONS, ids=_IDS)
    def test_every_field_explains_itself(self, service_name: str, action: ServiceAction) -> None:
        for action_field in action.fields:
            assert action_field.description.strip(), f"{service_name}/{action.action_id}: '{action_field.name}'"

    @pytest.mark.parametrize(("service_name", "action"), _ACTIONS, ids=_IDS)
    def test_combinations_point_at_code_that_exists(self, service_name: str, action: ServiceAction) -> None:
        # A combination is documentation with a pointer, so the pointer has to resolve;
        # otherwise it becomes a claim about a check that was moved or deleted.
        for combination in action.combinations:
            assert _resolve(combination.enforced_by) is not None

    @pytest.mark.parametrize(("service_name", "action"), _ACTIONS, ids=_IDS)
    def test_the_action_carries_an_example(self, service_name: str, action: ServiceAction) -> None:
        assert action.example.strip()

    @pytest.mark.parametrize(("service_name", "action"), _ACTIONS, ids=_IDS)
    def test_roles_are_the_service_vocabulary(self, service_name: str, action: ServiceAction) -> None:
        assert action.roles
        assert all(isinstance(role, ConfigRole) for role in action.roles)


class TestTheDeclarationRefusesToBeSloppy:
    def test_a_field_without_editable_or_reason_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no editable and no reason"):
            ActionField(name="thing", description="A thing.")

    def test_a_field_without_a_description_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no description"):
            ActionField(name="thing", description="", no_editable_reason="none applies")

    def test_an_action_without_an_example_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no usage example"):
            ServiceAction(
                action_id="thing",
                layer=ConfigLayer.PROJECT,
                roles=(ConfigRole.DEFINE,),
                summary="s",
                description="d",
                fields=(),
                verbs=(ActionVerb.CREATE,),
                handler=lambda ctx: None,
            )

    def test_a_combination_over_an_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown fields"):
            ServiceAction(
                action_id="thing",
                layer=ConfigLayer.PROJECT,
                roles=(ConfigRole.DEFINE,),
                summary="s",
                description="d",
                fields=(ActionField(name="a", description="A.", no_editable_reason="n/a"),),
                verbs=(ActionVerb.CREATE,),
                combinations=(FieldCombination(when="a=1", requires=("b",), enforced_by="builtins.dict"),),
                handler=lambda ctx: None,
                example="curl ...",
            )


class TestTheVerbTable:
    """The contract decided on 6 August 2026, in code rather than in prose."""

    def test_create_refuses_an_existing_id(self) -> None:
        assert ActionVerb.CREATE.method == "POST"
        assert ActionVerb.CREATE.on_existing == "reject"
        assert ActionVerb.CREATE.on_absent == "create"

    def test_update_expects_the_id_to_be_there(self) -> None:
        assert ActionVerb.UPDATE.method == "PUT"
        assert ActionVerb.UPDATE.on_existing == "replace"
        assert ActionVerb.UPDATE.on_absent == "reject"

    def test_upsert_replaces_without_asking(self) -> None:
        assert ActionVerb.UPSERT.on_existing == "replace"
        assert ActionVerb.UPSERT.on_absent == "create"

    def test_only_create_addresses_a_collection(self) -> None:
        assert not ActionVerb.CREATE.targets_existing
        assert ActionVerb.UPDATE.targets_existing
        assert ActionVerb.UPSERT.targets_existing


class TestTheAttachmentActions:
    def test_project_level_defines_only(self) -> None:
        assert PROJECT_ATTACHMENT_ACTION.roles == (ConfigRole.DEFINE,)
        assert {f.name for f in PROJECT_ATTACHMENT_ACTION.fields} == {"attachment_id", "file"}

    def test_component_level_defines_uses_and_binds(self) -> None:
        assert COMPONENT_ATTACHMENT_ACTION.roles == (ConfigRole.DEFINE, ConfigRole.USE, ConfigRole.BIND)
        assert {f.name for f in COMPONENT_ATTACHMENT_ACTION.fields} == {
            "attachment_id",
            "file",
            "provide-as",
            "path",
            "env-name",
        }

    def test_the_id_field_runs_the_same_rule_as_the_wizard(self) -> None:
        # Not "a rule like the wizard's" -- the same object. The wizard upload endpoint
        # validates against this editable too, so an id one accepts the other accepts.
        id_field = next(f for f in PROJECT_ATTACHMENT_ACTION.fields if f.name == "attachment_id")
        assert id_field.editable is ATTACHMENT_ID_EDITABLE

    def test_the_file_field_is_the_written_exception(self) -> None:
        file_field = next(f for f in PROJECT_ATTACHMENT_ACTION.fields if f.name == "file")
        assert file_field.editable is None
        assert file_field.kind is ActionFieldKind.FILE
        assert "MAX_ATTACHMENT_BYTES" in file_field.no_editable_reason

    def test_required_fields_depend_on_the_verb(self) -> None:
        # Creating needs the id as a field; updating addresses it in the path.
        profile_create = PROJECT_ATTACHMENT_ACTION.editables_for(ActionVerb.CREATE)
        profile_update = PROJECT_ATTACHMENT_ACTION.editables_for(ActionVerb.UPDATE)
        assert profile_create["attachment_id"].required
        assert not profile_update["attachment_id"].required

    def test_the_profile_never_copies_the_rule(self) -> None:
        # Only ``required`` differs from the shared editable; the validator is the same object.
        profile = PROJECT_ATTACHMENT_ACTION.editables_for(ActionVerb.CREATE)
        assert profile["attachment_id"].validator is ATTACHMENT_ID_EDITABLE.validator

    def test_the_delivery_combinations_are_declared(self) -> None:
        assert {c.when for c in COMPONENT_ATTACHMENT_ACTION.combinations} == {
            "provide-as=file",
            "provide-as=env-var",
        }


class TestTheGeneratedRoutes:
    @pytest.fixture(scope="class")
    def routes(self) -> dict[str, set[str]]:
        from opi.server import app

        found: dict[str, set[str]] = {}
        for route in app.routes:
            path = getattr(route, "path", "")
            if ("/services/attachments/" in path and path.endswith("attachments")) or path.endswith("{attachment_id}"):
                found.setdefault(path, set()).update(getattr(route, "methods", set()))
        return found

    def test_the_project_level_upload_exists(self, routes) -> None:
        base = "/api/v2/projects/{project_name}/services/attachments/attachments"
        assert "POST" in routes[base]
        assert "PUT" in routes[base + "/{attachment_id}"]

    def test_the_component_level_upload_exists(self, routes) -> None:
        base = "/api/v2/projects/{project_name}/services/attachments/component/{component_name}/attachments"
        assert "POST" in routes[base]
        assert "PUT" in routes[base + "/{attachment_id}"]

    def test_update_and_upsert_share_one_put_route(self, routes) -> None:
        # They address the same thing and differ only in what they promise, which the
        # caller states with ?upsert=true. Two PUT routes on one path would mean one of
        # them never runs.
        path = "/api/v2/projects/{project_name}/services/attachments/attachments/{attachment_id}"
        assert routes[path] == {"PUT"}


class TestTheOpenApiDocument:
    """What a client generating from the spec actually gets to read."""

    @pytest.fixture(scope="class")
    def spec(self) -> dict:
        from opi.server import app

        return app.openapi()

    def test_every_field_of_the_upload_carries_its_meaning(self, spec) -> None:
        path = "/api/v2/projects/{project_name}/services/attachments/component/{component_name}/attachments"
        operation = spec["paths"][path]["post"]
        ref = next(iter(operation["requestBody"]["content"].values()))["schema"]["$ref"]
        schema = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
        assert set(schema["properties"]) == {"attachment_id", "file", "provide-as", "path", "env-name"}
        for name, prop in schema["properties"].items():
            assert prop.get("description"), f"field '{name}' reaches the spec without a description"

    def test_the_description_states_the_verb_contract_and_an_example(self, spec) -> None:
        path = "/api/v2/projects/{project_name}/services/attachments/attachments/{attachment_id}"
        description = spec["paths"][path]["put"]["description"]
        assert "upsert" in description
        assert "curl" in description

    def test_the_field_combinations_reach_the_spec(self, spec) -> None:
        path = "/api/v2/projects/{project_name}/services/attachments/component/{component_name}/attachments"
        description = spec["paths"][path]["post"]["description"]
        assert "provide-as=file" in description
        assert "path" in description

    def test_the_upsert_flag_is_documented_on_the_put(self, spec) -> None:
        path = "/api/v2/projects/{project_name}/services/attachments/attachments/{attachment_id}"
        parameters = {p["name"]: p for p in spec["paths"][path]["put"]["parameters"]}
        assert parameters["upsert"]["schema"]["default"] is False
        assert parameters["upsert"]["description"]


def _is_action_route(path: str) -> bool:
    """Whether a path is a declared-action route rather than a config route.

    An action route ends in the action's own segment (optionally addressing one item);
    a config route ends in ``/config/...`` and takes JSON.
    """
    segments = path.split("/")
    last = segments[-1]
    tail = segments[-2] if last.startswith("{") else last
    return "/services/" in path and tail == "attachments"


class TestTheRequestBodyHasAName:
    """The body of an action route is a model with a name, not one FastAPI invents (RC-45).

    Loose multipart fields make FastAPI name the body after the route's unique id, which
    gives four schemas of around a hundred characters that differ only in layer and verb.
    They sit in the same schema list as the service's own models and read as duplicates of
    each other. The name is the action, the layer and the verb; nothing else distinguishes
    them anyway.
    """

    @pytest.fixture(scope="class")
    def spec(self) -> dict:
        from opi.server import app

        return app.openapi()

    @pytest.fixture(scope="class")
    def action_bodies(self, spec) -> dict[str, dict]:
        """Every action route's body: "METHOD path" -> its requestBody."""
        return {
            f"{method.upper()} {path}": operation["requestBody"]
            for path, item in spec["paths"].items()
            if "/services/" in path
            for method, operation in item.items()
            if method in ("post", "put") and "requestBody" in operation and _is_action_route(path)
        }

    def test_no_action_body_carries_a_generated_name(self, action_bodies) -> None:
        generated = {
            key: ref
            for key, body in action_bodies.items()
            for ref in [next(iter(body["content"].values()))["schema"]["$ref"].rsplit("/", 1)[-1]]
            if ref.startswith("Body_")
        }
        assert not generated, f"these bodies are named after their route instead of after what they are: {generated}"

    def test_the_upload_bodies_are_named_after_action_layer_and_verb(self, spec) -> None:
        names = {
            f"{method.upper()} {path}": next(iter(operation["requestBody"]["content"].values()))["schema"][
                "$ref"
            ].rsplit("/", 1)[-1]
            for path, item in spec["paths"].items()
            for method, operation in item.items()
            if _is_action_route(path) and method in ("post", "put")
        }
        assert set(names.values()) == {
            "AttachmentsProjectCreateRequest",
            "AttachmentsProjectUpdateRequest",
            "AttachmentsComponentCreateRequest",
            "AttachmentsComponentUpdateRequest",
        }

    def test_the_upload_still_promises_multipart(self, action_bodies) -> None:
        # The body became a model; what the route accepts must not have moved with it.
        # A form model defaults to urlencoded, and an upload cannot travel that way.
        for key, body in action_bodies.items():
            assert list(body["content"]) == ["multipart/form-data"], key
