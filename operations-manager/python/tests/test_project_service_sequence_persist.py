"""Project-level service-config sequences must survive the final wizard submit.

Regression for the RC-13 blocker: adding an invite (or a keycloak
``additional-clients`` entry) through the portal wrote the service config but
dropped the sequence item -- the committed file had ``active: []`` /
``additional-clients: []`` even though the form carried a filled item.

Root cause: on the final submit ``get_merged_data()`` returns devirtualized
data, so ``services`` is a mixed list and the ``_services-config`` virtual key
is gone. ``_process_sequence_json`` / ``_process_nested_sequence_json`` read the
items with the list-blind ``get_value`` at the real ``services/<name>/...`` path,
got ``None``, and overwrote the real list with ``[]``. Scalar fields survived
because they read through the list-aware ``smart_get_value``. The fix makes the
sequence reads list-aware too.

These exercise the exact final-submit shape (merged == submitted == base), which
is what ``submit_wizard`` passes to ``process_json_submission``.
"""

from typing import Any

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.flows import get_flow
from opi.forms.visualizers.wizard_sections import _CONFIG_SECTIONS_BY_ID
from opi.forms.wizard.write_set import apply_write_paths, flow_write_paths


def _service_config(services: list[Any], name: str) -> dict[str, Any] | None:
    """Config body for a named service in a mixed services list (record or legacy)."""
    for svc in services:
        if isinstance(svc, dict):
            if svc.get("name") == name or svc.get("reference") == name:
                return svc.get("config")
            if name in svc and isinstance(svc[name], dict):
                return svc[name].get("config")
    return None


@pytest.mark.asyncio
async def test_invite_active_survives_final_submit() -> None:
    """A single invite in services/invite/config/active must not be dropped."""
    section = _CONFIG_SECTIONS_BY_ID["invite-config"]
    processor = EditableFormProcessor()

    # Devirtualized merged data exactly as submit_wizard sees it.
    merged: dict[str, Any] = {
        "name": "demo",
        "services": [
            {
                "name": "invite",
                "config": {
                    "default-language": "nl",
                    "active": [{"key": "probe-invite-00aa70", "contact-email": "a@b.nl"}],
                },
            },
            {"name": "keycloak", "config": {"template": "sso-only"}},
        ],
    }

    final, errors = await processor.process_json_submission(
        merged, section.editables, merged, edit_mode=False, strip_transients=False
    )

    assert errors == {}
    cfg = _service_config(final["services"], "invite")
    assert cfg is not None
    assert cfg["active"]
    assert cfg["active"][0]["key"] == "probe-invite-00aa70"
    assert cfg["active"][0]["contact-email"] == "a@b.nl"


@pytest.mark.asyncio
async def test_postgresql_schemas_survive_final_submit() -> None:
    """An extra schema in services/postgresql-database/config/schemas (RC-17) must not
    be dropped -- the same project-level sequence pattern as the RC-13 regression."""
    section = _CONFIG_SECTIONS_BY_ID["postgresql-schemas-config"]
    processor = EditableFormProcessor()

    merged: dict[str, Any] = {
        "name": "demo",
        "services": [
            {
                "name": "postgresql-database",
                "config": {"schemas": [{"postfix": "rapportage", "description": "Rapportage"}]},
            },
        ],
        "deployments": [{"name": "dep"}],
    }

    final, errors = await processor.process_json_submission(
        merged, section.editables, merged, edit_mode=False, strip_transients=False
    )

    assert errors == {}
    cfg = _service_config(final["services"], "postgresql-database")
    assert cfg is not None
    assert cfg["schemas"]
    assert cfg["schemas"][0]["postfix"] == "rapportage"
    assert cfg["schemas"][0]["description"] == "Rapportage"


@pytest.mark.asyncio
async def test_keycloak_additional_clients_survive_final_submit() -> None:
    """A keycloak additional-clients entry (nested sequence) must not be dropped."""
    section = _CONFIG_SECTIONS_BY_ID["keycloak-config"]
    processor = EditableFormProcessor()

    merged: dict[str, Any] = {
        "name": "demo",
        "services": [
            {
                "name": "keycloak",
                "config": {
                    "template": "sso-only",
                    "additional-clients": [{"name": "myclient", "redirect-uris": ["https://x/*"]}],
                },
            },
        ],
    }

    final, errors = await processor.process_json_submission(
        merged, section.editables, merged, edit_mode=False, strip_transients=False
    )

    assert errors == {}
    cfg = _service_config(final["services"], "keycloak")
    assert cfg is not None
    clients = cfg["additional-clients"]
    assert clients
    assert clients[0]["name"] == "myclient"
    assert clients[0]["redirect-uris"] == ["https://x/*"]


def test_modal_edit_keeps_virtualized_service_key() -> None:
    """The detail-edit modal must write the real key behind an edited virtual key.

    Regression for the second RC-13 blocker: the modal save reverted ``services`` to the
    git baseline because the step produced only the virtual ``_services-config`` key (the
    added invite / edited keycloak client vanished, the existing one survived). The write
    set is derived from the invite editables, whose paths are the real ``services/invite/...``
    ones, so the edit lands where the fold put it.
    """
    flow = get_flow("modal-edit-invite-config")
    # What get_merged_data hands over: devirtualized, with the template context alongside.
    merged = {
        "name": "demo",
        "config": {"age-public-key": "..."},
        "services": [{"name": "invite", "config": {"active": [{"key": "x"}]}}],
    }
    existing = {
        "name": "demo",
        "config": {"age-public-key": "the-real-one"},
        "services": [{"name": "invite", "config": {"active": []}}],
    }

    result = apply_write_paths(existing, merged, flow_write_paths(list(flow.sections)))

    assert _service_config(result["services"], "invite")["active"] == [{"key": "x"}]
    assert result["config"] == {"age-public-key": "the-real-one"}, "template context was written over"


def test_write_set_leaves_context_the_flow_does_not_declare() -> None:
    """A team edit writes users and nothing else, however much context travels along."""
    flow = get_flow("modal-edit-team")
    merged = {"users": [{"email": "a@b.nl", "role": "admin"}], "config": {"k": "from-the-session"}}
    existing = {"users": [{"email": "old@b.nl", "role": "admin"}], "config": {"k": "on-disk"}}

    result = apply_write_paths(existing, merged, flow_write_paths(list(flow.sections)))

    assert result["users"] == [{"email": "a@b.nl", "role": "admin"}]
    assert result["config"] == {"k": "on-disk"}


def test_single_section_modal_does_not_stash_away_its_own_step() -> None:
    """A single-section service-config modal must keep its just-submitted step data.

    Regression for the third RC-13 blocker: after storing the invite step, the modal submit
    re-resolved active sections with ``resolve_active_section_ids``, whose visibility lambda
    (``_config_selected``) reads the real ``services`` list -- absent from the modal's
    virtual-key-only step_data. So the invite section resolved as INACTIVE and
    ``stash_inactive_sections`` moved its data to the stash, leaving ``_modal_do_submit`` with
    empty step_data (every real key then popped as template-only -> whole save reverted).

    The fix mirrors ``modal_wizard_init``: single-section flows treat their one section as
    active. This test proves the resolver-based path drops the data and the single-section
    path keeps it.
    """
    from opi.forms.visualizers.flows import get_flow
    from opi.forms.wizard.resolver import resolve_active_section_ids
    from opi.forms.wizard.state import WizardState

    flow = get_flow("modal-edit-invite-config")
    sid = flow.sections[0].section_id
    stored = {"_services-config": [{"name": "invite", "config": {"active": [{"key": "k"}]}}]}

    # The resolver-based path (the old bug): visibility can't see `services`, so it stashes.
    state_bug = WizardState(flow_id=flow.flow_id, current_step=sid, active_sections=[sid])
    state_bug.step_data = {sid: dict(stored)}
    resolved = resolve_active_section_ids(flow, state_bug.step_data)
    state_bug.stash_inactive_sections(resolved)
    assert sid not in state_bug.step_data, "precondition: resolver wrongly deems the section inactive"

    # The single-section bypass (the fix): the section stays active, data is preserved.
    state_fix = WizardState(flow_id=flow.flow_id, current_step=sid, active_sections=[sid])
    state_fix.step_data = {sid: dict(stored)}
    active_ids = (
        [flow.sections[0].section_id]
        if len(flow.sections) == 1
        else resolve_active_section_ids(flow, state_fix.step_data)
    )
    state_fix.stash_inactive_sections(active_ids)
    assert sid in state_fix.step_data
    assert state_fix.step_data[sid] == stored
