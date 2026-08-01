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
from opi.forms.visualizers.wizard_sections import _CONFIG_SECTIONS_BY_ID


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
