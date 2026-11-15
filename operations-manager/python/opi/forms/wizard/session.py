"""Wizard state persistence, as JSON files in ``{TEMP_DIR}/wizard-sessions/{token}.json``.

The state is stored server-side and keyed by a small UUID token, because the full wizard
state easily exceeds the ~4 KB browser cookie limit.

The full-page wizard keeps that token in the Starlette session cookie. The modal wizard
passes it through the HTMX request chain (a hidden input or a query param) instead, to avoid
the session-cookie race in which a concurrent request such as progress polling overwrites
the other's session data.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opi.core.config import settings
from opi.core.request_user import get_current_user
from opi.forms.wizard.state import WizardState

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

SESSION_KEY = "wizard_token"
"""Cookie key - holds only a short UUID, not the full state."""

# A token is a uuid.uuid4().hex, exactly 32 lowercase hex characters. Anything else is
# refused, to prevent path traversal through a user-supplied token.
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

_STORE_DIR: str | None = None

#: A state file holds a copy of a project file, encrypted secrets included, so it is for
#: its owner only. Same modes as ``services/upload_staging.py``, which stages uploads.
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _get_store_dir() -> str:
    """Return (and lazily create) the wizard session storage directory."""
    global _STORE_DIR
    if _STORE_DIR is None:
        _STORE_DIR = os.path.join(settings.TEMP_DIR, "wizard-sessions")
    os.makedirs(_STORE_DIR, mode=_DIR_MODE, exist_ok=True)
    return _STORE_DIR


def _is_valid_token(token: object) -> bool:
    """Token must match the uuid4 hex format we generate in save_wizard_state."""
    return isinstance(token, str) and bool(_TOKEN_RE.fullmatch(token))


SESSION_MAX_AGE_SECONDS = 24 * 60 * 60
"""How long an untouched session file may survive. A wizard is filled in within minutes;
a day is generous and still bounds what an abandoned session leaves behind."""

_SWEEP_INTERVAL_SECONDS = 60 * 60
_last_sweep_at: float | None = None


def purge_expired_states(max_age_seconds: float = SESSION_MAX_AGE_SECONDS) -> int:
    """Delete session files not modified for *max_age_seconds*. Returns the count.

    A state file is removed when a wizard is submitted or cancelled, but a user who closes
    the tab leaves one behind, and TEMP_DIR is a persistent volume: without this sweep they
    pile up for the lifetime of the volume, each holding a copy of a project file.
    """
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in Path(_get_store_dir()).glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            # Raced with another worker's cleanup, or unreadable; the next sweep retries.
            continue
    if removed:
        logger.info("Removed %d abandoned wizard session file(s)", removed)
    return removed


def _sweep_if_due() -> None:
    """Run the purge at most once per interval.

    Hooked to session *creation* rather than to a scheduler: that is when the store is in
    use, and a store nobody writes to cannot grow. It keeps the cleanup local to this module
    instead of adding another background task to the app lifespan.
    """
    global _last_sweep_at

    now = time.monotonic()
    if _last_sweep_at is not None and now - _last_sweep_at < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep_at = now
    purge_expired_states()


def _store_path(token: str) -> Path:
    """Resolve the on-disk path for a token; raises ``ValueError`` on a malformed one.

    A token arrives through an HTMX request param (query string or hidden input), which is
    attacker-controllable, so only a well-formed uuid4 hex string is accepted.
    """
    if not _is_valid_token(token):
        raise ValueError(f"invalid wizard token: {token!r}")
    return Path(_get_store_dir()) / f"{token}.json"


def get_wizard_state(request: Request) -> WizardState | None:
    """Load wizard state from the file-based store, or None when no wizard is in progress."""
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
    except json.JSONDecodeError, KeyError, TypeError:
        logger.warning("Invalid wizard state file (token=%s), clearing", token)
        clear_wizard_state(request)
        return None


def _write_state(path: Path, state: WizardState) -> None:
    """Write one state file, readable by its owner only."""
    path.write_text(json.dumps(state.to_dict()))
    os.chmod(path, _FILE_MODE)


def save_wizard_state(request: Request, state: WizardState) -> None:
    """Save wizard state to the file-based store."""
    token = request.session.get(SESSION_KEY)
    if token is None:
        token = uuid.uuid4().hex
        request.session[SESSION_KEY] = token
    _write_state(_store_path(token), state)


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
    """Initialize a new wizard state, save it to the store and return it.

    ``project_name`` is the project in edit mode and None while creating.
    """
    clear_wizard_state(request)
    _sweep_if_due()

    state = WizardState(
        flow_id=flow_id,
        current_step=first_step,
        active_sections=active_sections,
        project_name=project_name,
    )
    save_wizard_state(request, state)
    return state


def _load_state_by_token(token: str) -> WizardState | None:
    """Load wizard state by token directly (no session needed)."""
    if not _is_valid_token(token):
        logger.warning("Rejected malformed wizard token: %r", token)
        return None

    path = _store_path(token)
    if not path.exists():
        return None

    try:
        data: dict[str, Any] = json.loads(path.read_text())
        return WizardState.from_dict(data)
    except json.JSONDecodeError, KeyError, TypeError:
        logger.warning("Invalid state file (token=%s), removing", token)
        path.unlink(missing_ok=True)
        return None


def get_modal_state_by_token(token: str | None) -> WizardState | None:
    """Load modal wizard state by token."""
    if not token:
        return None
    return _load_state_by_token(token)


def save_modal_state_by_token(token: str | None, state: WizardState) -> None:
    """Save modal wizard state by token."""
    if not token or not _is_valid_token(token):
        return
    _write_state(_store_path(token), state)


def clear_modal_state_by_token(token: str | None) -> None:
    """Delete the state file for the given token."""
    if not token or not _is_valid_token(token):
        return
    path = _store_path(token)
    if path.exists():
        path.unlink()


def load_modal_state_for_user(request: Request, token: str | None) -> WizardState | None:
    """The state behind this token, but only for the user who started it.

    A token on its own is no proof of access: shared, copied or guessed it would
    otherwise open someone else's wizard. A state without an owner (written before the
    field existed) counts as expired.
    """
    state = get_modal_state_by_token(token)
    if state is None:
        return None

    user_email = (get_current_user(request) or {}).get("email", "").lower()
    if not state.owner_email or state.owner_email != user_email:
        logger.warning("Wizard state rejected: not started by this user")
        return None
    return state


def load_modal_state_for(request: Request, token: str | None, project_name: str) -> WizardState | None:
    """Like ``load_modal_state_for_user``, and only under the project it was started for.

    The routes that carry a project in their path use this one. The attachment routes
    have no project in their path and check the flow instead.
    """
    state = load_modal_state_for_user(request, token)
    if state is None:
        return None
    if state.project_name != project_name:
        logger.warning("Wizard state of project %s rejected under %s", state.project_name, project_name)
        return None
    return state


def init_modal_state_tokenized(
    flow_id: str,
    first_step: str,
    active_sections: list[str],
    project_name: str,
    owner_email: str,
) -> tuple[str, WizardState]:
    """Create a new modal wizard state and return (token, state).

    No session cookie is involved: the caller passes the token to the client itself, for
    instance as a hidden form field.
    """
    _sweep_if_due()
    token = uuid.uuid4().hex
    state = WizardState(
        flow_id=flow_id,
        current_step=first_step,
        active_sections=active_sections,
        project_name=project_name,
        owner_email=owner_email.lower(),
    )
    save_modal_state_by_token(token, state)
    return token, state
