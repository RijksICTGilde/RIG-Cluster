"""Service config typed in one wizard step must survive to the next and back.

Reported on 5 August: change the keycloak template, click next, and the invite step does
not follow it; go back and the OLD value is selected again. Both came from one cause.

The wizard keeps service SELECTION and service CONFIG apart in its state: ``services``
holds the chosen names, ``_services-config`` holds the configuration. Every reader,
however, addressed ``services/<name>/config/...`` and ``is_service_config_path`` only
recognised the ``services`` root, so a config lookup during the wizard returned None. The
field then fell back to its own default, which is why the "old value" reappeared -- it was
never the old value, it was the default. Clicking next from there submitted that default
over the user's choice, so the change was genuinely lost.

The virtual root appears in two shapes and both must keep working: a plain dict keyed by
service name in a form SUBMISSION, and the services-list format in wizard STATE.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.service_path import (
    is_service_config_path,
    smart_delete_value,
    smart_get_value,
    smart_path_exists,
    smart_set_value,
)
from opi.forms.visualizers.bridge import editable_to_form_field, should_render_editable
from opi.forms.visualizers.flows import get_flow
from opi.forms.wizard.state import WizardState
from opi.services.catalog.invite.visualizers import INVITE_AUTH_METHODS
from opi.services.catalog.keycloak.visualizers import KEYCLOAK_TEMPLATE
from opi.web.router_wizard import _extract_section_data

RECORD = [{"name": "keycloak", "config": {"template": "sso-only"}}, "invite"]
MAPPING = [{"keycloak": {"config": {"template": "sso-only"}}}]
SUBMITTED = {"keycloak": {"config": {"template": "sso-only"}}}


# --- the path layer ----------------------------------------------------------


@pytest.mark.parametrize("root", ["services", "_services-config"])
@pytest.mark.parametrize(("shape", "value"), [("record", RECORD), ("mapping", MAPPING)])
def test_config_is_readable_under_both_roots_and_both_entry_shapes(root: str, shape: str, value: list[Any]) -> None:
    data = {root: value}
    assert smart_get_value(data, f"{root}/keycloak/config/template") == "sso-only", shape


@pytest.mark.parametrize("root", ["services", "_services-config"])
def test_both_roots_are_recognised_as_service_config_paths(root: str) -> None:
    assert is_service_config_path(f"{root}/keycloak/config/template")


def test_a_dict_shaped_root_is_read_as_a_plain_dict() -> None:
    """That is the shape of a form submission. Treating it as a services list would
    report the value as missing."""
    data = {"_services-config": SUBMITTED}
    assert smart_get_value(data, "_services-config/keycloak/config/template") == "sso-only"


def test_writing_into_a_dict_shaped_root_does_not_wipe_the_submission() -> None:
    """The list machinery starts by replacing a non-list root with []. Doing that to a
    submission would discard everything the user just entered."""
    data: dict[str, Any] = {"_services-config": {"keycloak": {"config": {"template": "sso-only"}}}}
    smart_set_value(data, "_services-config/keycloak/config/template", "sso-support")

    assert isinstance(data["_services-config"], dict)
    assert data["_services-config"]["keycloak"]["config"]["template"] == "sso-support"


def test_writing_into_a_list_shaped_root_still_uses_the_services_list() -> None:
    data: dict[str, Any] = {"_services-config": [{"name": "keycloak", "config": {"template": "sso-only"}}]}
    smart_set_value(data, "_services-config/keycloak/config/template", "sso-support")

    assert isinstance(data["_services-config"], list)
    assert smart_get_value(data, "_services-config/keycloak/config/template") == "sso-support"


@pytest.mark.parametrize("root", ["services", "_services-config"])
def test_exists_and_delete_follow_the_same_root(root: str) -> None:
    data: dict[str, Any] = {root: [{"name": "keycloak", "config": {"template": "sso-only"}}]}
    path = f"{root}/keycloak/config/template"

    assert smart_path_exists(data, path)
    smart_delete_value(data, path)
    assert not smart_path_exists(data, path)


# --- the reported bug, end to end -------------------------------------------


async def _change_template(before: str, after: str) -> tuple[Any, bool]:
    """Run the reported sequence: a stored template, a step submit that changes it.

    Returns what the keycloak step shows on the way back, and whether the invite step
    offers the auth methods.
    """
    section = next(s for s in get_flow("create-project").sections if s.section_id == "keycloak-config")

    state = WizardState(flow_id="create-project", current_step="keycloak-config")
    state.active_sections = ["services", "keycloak-config", "invite-config"]
    state.store_step_data("services", {"services": ["keycloak", "invite"]})
    state.store_step_data("keycloak-config", {"_services-config": [{"keycloak": {"config": {"template": before}}}]})

    processor = EditableFormProcessor()
    submitted = {"_services-config": {"keycloak": {"config": {"template": after}}}}
    result, errors = await processor.process_json_submission(submitted, section.editables, state.get_merged_data())
    assert errors == {}, errors
    state.store_step_data("keycloak-config", _extract_section_data(section.editables, result))

    merged = state.get_merged_data()
    return (
        editable_to_form_field(KEYCLOAK_TEMPLATE, merged).value,
        should_render_editable(INVITE_AUTH_METHODS, merged, index=0),
    )


@pytest.mark.parametrize(("before", "after"), [("sso-support", "sso-only"), ("sso-only", "sso-support")])
async def test_going_back_shows_the_value_the_user_chose(before: str, after: str) -> None:
    """Not the previous one, and not the field's default -- which is what made this look
    like "my change was not saved" while it was actually the default taking over."""
    shown, _ = await _change_template(before, after)
    assert shown == after


async def test_the_invite_step_follows_a_template_change_forward() -> None:
    """sso-support allows local accounts next to SSO, so there is a choice to offer."""
    _, offers_choice = await _change_template("sso-only", "sso-support")
    assert offers_choice is True


async def test_the_invite_step_hides_the_choice_when_the_template_leaves_none() -> None:
    """sso-only sets registrationAllowed/loginWithEmailAllowed to false in the blueprint,
    so SSO is the only way in and the field would be a choice that changes nothing."""
    _, offers_choice = await _change_template("sso-support", "sso-only")
    assert offers_choice is False


# --- readers find service config wherever the wizard put it -------------------


class TestReadersFindConfigUnderEitherRoot:
    """Any reader asking for the real path must find the value in wizard state too.

    Options providers ask for ``services/keycloak/config/...`` because that is where the
    value lives in the project file. In wizard state that config sits under the virtual
    root while ``services`` holds only the chosen names, so those providers returned
    empty lists: the invite realm-role picker rendered a select with nothing in it.

    Fixed in the read itself rather than per provider, so a reader added later is right
    without knowing any of this.
    """

    @staticmethod
    def _keycloak() -> dict[str, Any]:
        return {
            "name": "keycloak",
            "config": {"restrict-access": {"realm-role": "allowed-user"}, "realm-roles": [{"name": "developer"}]},
        }

    def _wizard_state(self) -> dict[str, Any]:
        return {"services": ["keycloak", "invite"], "_services-config": [self._keycloak()]}

    def _project_file(self) -> dict[str, Any]:
        return {"services": [self._keycloak()]}

    def test_the_realm_role_picker_is_filled_in_the_wizard(self) -> None:
        from opi.forms.visualizers.providers import InviteRealmRoleOptionsProvider

        options = InviteRealmRoleOptionsProvider(yaml_data=self._wizard_state()).get_options()

        assert [o["value"] for o in options] == ["", "developer", "allowed-user"]

    def test_it_gives_the_same_answer_on_the_project_page(self) -> None:
        """The two contexts must not disagree; that difference is the whole bug."""
        from opi.forms.visualizers.providers import InviteRealmRoleOptionsProvider

        in_wizard = InviteRealmRoleOptionsProvider(yaml_data=self._wizard_state()).get_options()
        on_page = InviteRealmRoleOptionsProvider(yaml_data=self._project_file()).get_options()

        assert in_wizard == on_page

    def test_a_write_never_wanders_to_the_other_root(self) -> None:
        """Reads fall back, writes must not: an edit landing under a root the form does
        not read back would look saved and be gone on the next render."""
        data: dict[str, Any] = {"_services-config": [self._keycloak()]}
        smart_set_value(data, "services/keycloak/config/template", "sso-only")

        assert (
            smart_get_value({"services": data.get("services", [])}, "services/keycloak/config/template") == "sso-only"
        )
        assert "template" not in data["_services-config"][0]["config"]
