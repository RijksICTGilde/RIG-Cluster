"""Session integration for wizard state persistence.

Wizard state is stored server-side as JSON files, keyed by a small
token that lives in the Starlette session cookie.  This avoids the
~4 KB browser cookie size limit which the full wizard state can
easily exceed, and survives pod restarts during development (unlike
an in-memory dict).

Files are stored in ``{TEMP_DIR}/wizard-sessions/{token}.json``.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opi.core.config import settings
from opi.forms.wizard.state import WizardState

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

SESSION_KEY = "wizard_token"
"""Cookie key — holds only a short UUID, not the full state."""

_STORE_DIR: str | None = None


def _get_store_dir() -> str:
    """Return (and lazily create) the wizard session storage directory."""
    global _STORE_DIR
    if _STORE_DIR is None:
        _STORE_DIR = os.path.join(settings.TEMP_DIR, "wizard-sessions")
    os.makedirs(_STORE_DIR, exist_ok=True)
    return _STORE_DIR


def _store_path(token: str) -> Path:
    return Path(_get_store_dir()) / f"{token}.json"


def get_wizard_state(request: Request) -> WizardState | None:
    """Load wizard state from the file-based store.

    Returns None if no wizard is in progress.
    """
    token = request.session.get(SESSION_KEY)
    if token is None:
        return None

    path = _store_path(token)
    if not path.exists():
        # Token in cookie but file gone (e.g. /tmp cleaned)
        request.session.pop(SESSION_KEY, None)
        return None

    try:
        data: dict[str, Any] = json.loads(path.read_text())
        return WizardState.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("Invalid wizard state file (token=%s), clearing", token)
        clear_wizard_state(request)
        return None


def save_wizard_state(request: Request, state: WizardState) -> None:
    """Save wizard state to the file-based store."""
    token = request.session.get(SESSION_KEY)
    if token is None:
        token = uuid.uuid4().hex
        request.session[SESSION_KEY] = token
    _store_path(token).write_text(json.dumps(state.to_dict()))


def clear_wizard_state(request: Request) -> None:
    """Remove wizard state from both the store and the session cookie."""
    token = request.session.pop(SESSION_KEY, None)
    if token is not None:
        path = _store_path(token)
        if path.exists():
            path.unlink()


def init_wizard_state(
    request: Request,
    flow_id: str,
    first_step: str,
    active_sections: list[str],
    project_name: str | None = None,
) -> WizardState:
    """Initialize a new wizard state and save it to the store.

    Args:
        request: The current request (for session access).
        flow_id: FormFlow ID (e.g., "create-project").
        first_step: Section_id of the first step.
        active_sections: Initial ordered list of active section_ids.
        project_name: Project name for edit mode, None for create.

    Returns:
        The newly created WizardState.
    """
    # Clear any existing wizard state first
    clear_wizard_state(request)

    state = WizardState(
        flow_id=flow_id,
        current_step=first_step,
        active_sections=active_sections,
        project_name=project_name,
    )
    save_wizard_state(request, state)
    return state
