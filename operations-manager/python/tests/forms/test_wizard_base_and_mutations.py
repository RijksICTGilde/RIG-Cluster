"""LEVEL 2 (wizard save/round-trip) for the wizard itself: base plus mutations.

The template for this file is ``tests/test_service_health_check.py``; the levels are
described in ``instructions/wizard-tests.md``. Level 2 is the fast, browserless seam:
a POST goes through ``EditableFormProcessor`` (and, for a flow, the router's
reconciliation) and must land at the right yaml path in the right shape.

WHAT IT GUARDS
--------------
The four bugs of 6 August 2026, each of which was only visible in a browser and each
of which asked the same question -- what is the base, what is the mutation, and how do
you tell "not touched" from "emptied":

1. a LOCKED service disappearing on save (a disabled checkbox posts nothing);
   plus the same failure for a service the picker never offers at all;
2. a reader that only finds the service config under the virtual key;
3. an empty field writing an empty list instead of nothing;
4. a step that goes wrong only in the EDIT flow and not in the create wizard.

Run: uv run pytest tests/forms/test_wizard_base_and_mutations.py -q
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.service_path import check_requirements, smart_get_value
from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE
from opi.forms.visualizers.wizard_sections import SERVICES_EDIT_SECTION, SERVICES_SECTION
from opi.forms.wizard.mutation import (
    apply_selection_mutation,
    apply_services_mutation,
    offered_selection_values,
)

#: Chosen because keycloak REQUIRES it: the card is locked, the checkbox is disabled,
#: and a disabled checkbox posts nothing.
LOCKED_BY_DEPENDENCY = "publish-on-web"

#: A service that is real, lives in project files, and is NOT in the picker
#: (``hidden=True``). The form cannot have offered it, so it cannot have been unticked.
NEVER_OFFERED = "namespace-postgresql-database"


def _names(services: list[Any]) -> list[str]:
    from opi.services.services import service_entry_name

    return [name for entry in services if (name := service_entry_name(entry)) is not None]


async def _submit_services(section: Any, base: dict[str, Any], post: dict[str, Any]) -> list[Any]:
    """One services-step submit, exactly as the routers run it.

    Processor first (it converts and writes the selection), then the router's
    reconciliation with the base. Both routers call ``apply_services_mutation``;
    testing through it is what makes this a flow test rather than a field test.
    """
    processor = EditableFormProcessor()
    result, errors = await processor.process_json_submission(
        copy.deepcopy(post), section.editables, copy.deepcopy(base)
    )
    assert not errors, f"submit reported errors: {errors}"
    apply_services_mutation(section.editables, base, result)
    return result["services"]


# ---------------------------------------------------------------------------
# BUG 1 - a locked service must survive a save, without a hidden input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_locked_service_survives_a_save_that_does_not_post_it() -> None:
    """keycloak requires publish-on-web, so its card is locked and posts nothing.

    The server puts it back because it knows the requirement -- which is why the
    lock no longer needs a hidden input travelling next to the checkbox.
    """
    base = {"services": [LOCKED_BY_DEPENDENCY, "keycloak"]}
    post = {"services": ["keycloak"]}  # the locked card sent nothing

    services = await _submit_services(SERVICES_SECTION, base, post)

    assert LOCKED_BY_DEPENDENCY in _names(services)


@pytest.mark.asyncio
async def test_a_service_the_form_never_offered_survives_a_save() -> None:
    """A hidden service is in the project but not in the picker.

    It cannot have been unticked, so absence says nothing about it -- and its
    config must come through untouched.
    """
    base = {
        "services": [
            LOCKED_BY_DEPENDENCY,
            {"name": NEVER_OFFERED, "config": {"instances": 1, "storage": "1Gi"}},
        ]
    }
    post = {"services": [LOCKED_BY_DEPENDENCY]}

    services = await _submit_services(SERVICES_SECTION, base, post)

    assert NEVER_OFFERED in _names(services)
    kept = next(e for e in services if isinstance(e, dict) and e.get("name") == NEVER_OFFERED)
    assert kept["config"] == {"instances": 1, "storage": "1Gi"}


@pytest.mark.asyncio
async def test_unticking_an_offered_service_still_removes_it() -> None:
    """The rule must not turn every save into "nothing is ever removed"."""
    base = {"services": [LOCKED_BY_DEPENDENCY, "redis"]}
    post = {"services": [LOCKED_BY_DEPENDENCY]}

    services = await _submit_services(SERVICES_SECTION, base, post)

    assert "redis" not in _names(services)


def test_offered_is_what_the_picker_shows() -> None:
    offered = offered_selection_values(SERVICES_SECTION.editables, "services")
    assert offered is not None
    assert LOCKED_BY_DEPENDENCY in offered
    assert NEVER_OFFERED not in offered, "a hidden service must not count as offered"


def test_a_section_without_the_selection_offers_nothing_at_all() -> None:
    """None, not an empty set: a step that does not show the field says nothing
    about it, and nothing may be removed on its say-so."""
    assert offered_selection_values([COMPONENTS_SEQUENCE], "services") is None


def test_a_step_that_does_not_show_the_selection_leaves_it_alone() -> None:
    """A processed submission is a copy of the merged project data, so ``services``
    is in the result of EVERY flow -- also one about environment variables. A step
    that never showed the selection must not add a service to it either."""
    base = {"services": ["keycloak"]}  # inconsistent on purpose: keycloak requires publish-on-web
    submitted_yaml = {"services": ["keycloak"], "components": [{"name": "web"}]}

    apply_services_mutation([COMPONENTS_SEQUENCE], base, submitted_yaml)

    assert submitted_yaml["services"] == ["keycloak"]


def test_apply_selection_mutation_keeps_base_entries_whole() -> None:
    base = [{"keycloak": {"config": {"template": "sso-only"}}}]
    result = apply_selection_mutation(base, submitted=[], offered=set())
    assert result == base
    assert result[0] is not base[0], "the base must not be handed out by reference"


# ---------------------------------------------------------------------------
# BUG 2 - a reader must find the config under the virtual key too
# ---------------------------------------------------------------------------


def test_a_reader_finds_the_config_when_only_the_virtual_key_exists() -> None:
    """Mid-wizard there is no ``services`` list yet: the config lives under
    ``_services-config`` alone. A reader asking for the real path (the path the
    project file uses) must still find it -- the invite realm-role picker came up
    empty for exactly this reason."""
    wizard_state = {"_services-config": [{"keycloak": {"config": {"realm-roles": ["beheerder"]}}}]}

    assert smart_get_value(wizard_state, "services/keycloak/config/realm-roles") == ["beheerder"]


def test_a_reader_finds_the_config_in_a_saved_project_too() -> None:
    """The same reader, the same path, the other shape. One way to the config."""
    project = {"services": [{"name": "keycloak", "config": {"realm-roles": ["beheerder"]}}]}

    assert smart_get_value(project, "services/keycloak/config/realm-roles") == ["beheerder"]


def test_a_requirement_is_met_when_only_the_virtual_key_carries_it() -> None:
    """``smart_path_exists`` is the same reader wearing a different hat -- it decides
    whether a service's ``requires`` is satisfied. It lacked the fallback, so a
    requirement written as a real path (the only way to write it) read as unmet
    mid-wizard while the user was looking straight at the value they had entered."""
    wizard_state = {"_services-config": [{"keycloak": {"config": {"restrict-access": {"realm-role": "beheerder"}}}}]}

    assert check_requirements(["services/keycloak/config/restrict-access"], wizard_state) == []


def test_an_unmet_requirement_is_still_unmet() -> None:
    wizard_state = {"_services-config": [{"keycloak": {"config": {}}}]}

    assert check_requirements(["services/keycloak/config/restrict-access"], wizard_state) == [
        "services/keycloak/config/restrict-access"
    ]


def test_the_pair_of_keys_is_declared_exactly_once() -> None:
    """Service packages DECLARE virtualization with this pair and the path resolver
    RESOLVES it with the same pair; letting the two drift is a silent failure."""
    from opi.forms.editables.editable import SERVICE_VIRTUALIZE
    from opi.forms.editables.service_path import _SERVICE_ROOTS
    from opi.forms.wizard.state import _SERVICE_LIST_KEYS
    from opi.services.catalog.keycloak.editables import KEYCLOAK_TEMPLATE_EDITABLE

    assert SERVICE_VIRTUALIZE == ("services", "_services-config")
    assert _SERVICE_ROOTS is SERVICE_VIRTUALIZE
    assert _SERVICE_LIST_KEYS is SERVICE_VIRTUALIZE
    assert KEYCLOAK_TEMPLATE_EDITABLE.virtualize is SERVICE_VIRTUALIZE


# ---------------------------------------------------------------------------
# BUG 3 - an empty field writes nothing, not an empty list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_start_command_writes_no_key_at_all() -> None:
    """``command: []`` fails the schema (minItems 1). An untouched optional field
    must leave the key out entirely."""
    post = {
        "components": [
            {
                "name": "web",
                "image": "nginx",
                "ports": {"inbound": ["8080"]},
                "path": "/",
                "command": "",
            }
        ]
    }
    processor = EditableFormProcessor()
    result, errors = await processor.process_json_submission(
        copy.deepcopy(post), [COMPONENTS_SEQUENCE], copy.deepcopy(post), strip_transients=True
    )

    assert not errors, f"submit reported errors: {errors}"
    assert "command" not in result["components"][0]


@pytest.mark.asyncio
async def test_a_filled_start_command_becomes_a_list_of_arguments() -> None:
    post = {
        "components": [
            {
                "name": "web",
                "image": "nginx",
                "ports": {"inbound": ["8080"]},
                "path": "/",
                "command": 'sh -c "sleep 1"',
            }
        ]
    }
    processor = EditableFormProcessor()
    result, errors = await processor.process_json_submission(
        copy.deepcopy(post), [COMPONENTS_SEQUENCE], copy.deepcopy(post), strip_transients=True
    )

    assert not errors, f"submit reported errors: {errors}"
    assert result["components"][0]["command"] == ["sh", "-c", "sleep 1"]


# ---------------------------------------------------------------------------
# BUG 4 - the edit flow must behave like the create wizard
#
# The create wizard's services step is called "services" and the edit flow's is
# called "services-edit". Behaviour hung off that name, so a rule held in one flow
# and not in the other. Run the SAME submission through both sections and demand
# the same answer; a future rule that hangs off a name fails here.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("section", [SERVICES_SECTION, SERVICES_EDIT_SECTION], ids=["create", "edit"])
async def test_both_flows_reconcile_the_selection_the_same_way(section: Any) -> None:
    base = {
        "services": [
            LOCKED_BY_DEPENDENCY,
            "keycloak",
            {"name": NEVER_OFFERED, "config": {"instances": 1}},
            "redis",
        ]
    }
    post = {"services": ["keycloak"]}

    services = await _submit_services(section, base, post)

    assert sorted(_names(services)) == sorted([LOCKED_BY_DEPENDENCY, "keycloak", NEVER_OFFERED])
