"""Regression tests for the wizard-edit privilege-escalation fix.

The full-page wizard edit flow used to only check
``is_user_authorized_for_project`` (true for any role, including a plain
member) and then mass-merged the form output over the stored project with
``existing_data.update(data)``. A member could therefore enter the wizard,
rewrite the ``users`` list to make themselves owner (or drop the owners),
and overwrite the ``config`` block (api-key, AGE keys). This is a project
takeover / secret-exfiltration path.

These tests pin the fix:

- a plain ``member`` is rejected from the wizard edit GET entry and from
  the mutating save path;
- an ``owner`` (and a global ``admin``) passes;
- a member-style payload that tries to rewrite ``users``/``config`` does
  not mutate those protected keys, even if the role check were bypassed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from opi.services.project_service import ProjectService, ProjectUser
from opi.web.project_edit_security import (
    PROTECTED_PROJECT_KEYS,
    merge_preserving_protected_keys,
    require_project_edit_access,
)
from opi.web.router_wizard import _save_existing_project

PROJECT_NAME = "takeover-target"

OWNER_EMAIL = "owner@example.com"
MEMBER_EMAIL = "member@example.com"
ADMIN_EMAIL = "global-admin@example.com"

STORED_DATA: dict[str, Any] = {
    "name": PROJECT_NAME,
    "display-name": "Takeover Target",
    "clusters": ["odcn-production"],
    "users": [
        {"email": OWNER_EMAIL, "role": "owner"},
        {"email": MEMBER_EMAIL, "role": "member"},
    ],
    "config": {
        "api-key": "super-secret-api-key",
        "age-public-key": "age1publicpublicpublic",
        "age-private-key": "AGE-SECRET-KEY-PRIVATEPRIVATE",
    },
    "components": [{"name": "frontend", "image": "nginx:latest"}],
}


@pytest.fixture
def project_service() -> ProjectService:
    """Singleton ProjectService seeded with one project, cleaned up after."""
    service = ProjectService()
    saved_projects = dict(service._projects)
    saved_admins = set(service._admin_emails)
    service._projects.clear()
    service._admin_emails.clear()
    service._admin_emails.add(ADMIN_EMAIL)
    service.register(
        project_name=PROJECT_NAME,
        api_key=STORED_DATA["config"]["api-key"],
        filename=f"{PROJECT_NAME}.yaml",
        users=[ProjectUser(email=OWNER_EMAIL, role="owner"), ProjectUser(email=MEMBER_EMAIL, role="member")],
        data=dict(STORED_DATA),
    )
    yield service
    service._projects.clear()
    service._projects.update(saved_projects)
    service._admin_emails.clear()
    service._admin_emails.update(saved_admins)


def _request_for(email: str) -> Any:
    """Minimal fake request whose state.user yields the given email."""
    return SimpleNamespace(state=SimpleNamespace(user={"email": email}))


# ---------------------------------------------------------------------------
# Role gate (mirrors detail-edit, reused by wizard_edit_page)
# ---------------------------------------------------------------------------


def test_member_rejected_from_wizard_edit_gate(project_service: ProjectService) -> None:
    """A plain member must be denied entry to the wizard edit flow."""
    with (
        patch("opi.web.router_detail_edit.get_current_user", return_value={"email": MEMBER_EMAIL}),
        pytest.raises(HTTPException) as exc,
    ):
        require_project_edit_access(_request_for(MEMBER_EMAIL), PROJECT_NAME)
    assert exc.value.status_code == 403


def test_owner_passes_wizard_edit_gate(project_service: ProjectService) -> None:
    """An owner is allowed into the wizard edit flow."""
    with patch("opi.web.router_detail_edit.get_current_user", return_value={"email": OWNER_EMAIL}):
        project, user_email = require_project_edit_access(_request_for(OWNER_EMAIL), PROJECT_NAME)
    assert project.name == PROJECT_NAME
    assert user_email == OWNER_EMAIL


def test_global_admin_passes_wizard_edit_gate(project_service: ProjectService) -> None:
    """A global admin is allowed into the wizard edit flow."""
    with patch("opi.web.router_detail_edit.get_current_user", return_value={"email": ADMIN_EMAIL}):
        project, _ = require_project_edit_access(_request_for(ADMIN_EMAIL), PROJECT_NAME)
    assert project.name == PROJECT_NAME


# ---------------------------------------------------------------------------
# Mutating save path: TOCTOU re-check + protected-key merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_rejected_on_save(project_service: ProjectService) -> None:
    """The mutating save must re-check the role and reject a member.

    This guards against the TOCTOU case where the GET-time check passed
    (or was bypassed) but the POST is performed by a non-owner.
    """
    malicious_payload = {
        "display-name": "Pwned",
        "users": [{"email": MEMBER_EMAIL, "role": "owner"}],
    }
    with (
        patch("opi.web.router_detail_edit.get_current_user", return_value={"email": MEMBER_EMAIL}),
        pytest.raises(HTTPException) as exc,
    ):
        await _save_existing_project(_request_for(MEMBER_EMAIL), PROJECT_NAME, malicious_payload)
    assert exc.value.status_code == 403

    # Stored project must be untouched.
    stored = project_service.get_project(PROJECT_NAME)
    assert stored is not None
    assert stored.data["users"] == STORED_DATA["users"]
    assert stored.data["display-name"] == "Takeover Target"


@pytest.mark.asyncio
async def test_owner_save_cannot_overwrite_protected_keys(project_service: ProjectService) -> None:
    """Even an authorized owner cannot rewrite users/config/name/clusters.

    The wizard exposes those as editable fields; the merge must always
    re-derive them from the stored project. Non-protected fields still
    update normally.
    """
    captured: dict[str, Any] = {}

    def fake_save(file_path: str, project_data: dict[str, Any]) -> None:
        captured["file_path"] = file_path
        captured["data"] = project_data

    payload = {
        "display-name": "Renamed By Owner",
        "name": "evil-rename",
        "clusters": ["attacker-cluster"],
        "users": [{"email": MEMBER_EMAIL, "role": "owner"}],
        "config": {"api-key": "attacker-key", "age-private-key": "AGE-SECRET-KEY-STOLEN"},
        "components": [{"name": "frontend", "image": "nginx:1.27"}],
    }

    with (
        patch("opi.web.router_detail_edit.get_current_user", return_value={"email": OWNER_EMAIL}),
        patch("opi.handlers.project_file_handler.save_project_file", side_effect=fake_save),
        patch.object(project_service, "load_project_from_data", return_value=True),
        patch("opi.web.router_wizard.clear_wizard_state"),
    ):
        response = await _save_existing_project(_request_for(OWNER_EMAIL), PROJECT_NAME, payload)

    assert response.status_code == 200
    assert "data" in captured, "save_project_file was not called; the save path did not complete"
    saved = captured["data"]

    # Protected keys re-derived from the stored project, NOT the payload.
    assert saved["users"] == STORED_DATA["users"]
    assert saved["config"] == STORED_DATA["config"]
    assert saved["name"] == PROJECT_NAME
    assert saved["clusters"] == STORED_DATA["clusters"]

    # Non-protected fields still applied.
    assert saved["display-name"] == "Renamed By Owner"
    assert saved["components"] == [{"name": "frontend", "image": "nginx:1.27"}]


@pytest.mark.asyncio
async def test_owner_save_protected_keys_survive_deletion_attack(project_service: ProjectService) -> None:
    """Omitting a protected key from the payload must not drop it.

    The deletion attack: submit a payload that does not contain ``users``
    (or ``config``) at all, hoping the merge silently leaves them out so
    an owner/admin disappears. The merge must re-derive the stored value
    even when the key is absent from the form output.
    """
    captured: dict[str, Any] = {}

    def fake_save(file_path: str, project_data: dict[str, Any]) -> None:
        captured["data"] = project_data

    # No users/config/name/clusters in the payload at all.
    payload = {"display-name": "Slimmed Down"}

    with (
        patch("opi.web.router_detail_edit.get_current_user", return_value={"email": OWNER_EMAIL}),
        patch("opi.handlers.project_file_handler.save_project_file", side_effect=fake_save),
        patch.object(project_service, "load_project_from_data", return_value=True),
        patch("opi.web.router_wizard.clear_wizard_state"),
    ):
        response = await _save_existing_project(_request_for(OWNER_EMAIL), PROJECT_NAME, payload)

    assert response.status_code == 200
    saved = captured["data"]

    # Protected keys are still present and unchanged despite being absent
    # from the submitted payload.
    assert saved["users"] == STORED_DATA["users"]
    assert saved["config"] == STORED_DATA["config"]
    assert saved["name"] == PROJECT_NAME
    assert saved["clusters"] == STORED_DATA["clusters"]
    assert saved["display-name"] == "Slimmed Down"


# ---------------------------------------------------------------------------
# Route-level gate: the GET handler wires through require_project_edit_access
#
# The helper-level tests above prove the gate function itself rejects a
# member. This test pins the route -> gate wiring so a future refactor
# cannot silently drop the gate call from `wizard_edit_page` without a test
# failure. The route does light setup (get_flow / get_current_user /
# get_templates) before calling the gate, all mocked here.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wizard_edit_page_invokes_role_gate(project_service: ProjectService) -> None:
    """`wizard_edit_page` must invoke ``require_project_edit_access``; if the
    gate raises, the route propagates it.
    """
    from unittest.mock import MagicMock

    from opi.web.router_wizard import wizard_edit_page

    sentinel = HTTPException(status_code=403, detail="gate-fired-from-test")

    with (
        patch(
            "opi.web.project_edit_security.require_project_edit_access",
            side_effect=sentinel,
        ) as gate,
        patch("opi.web.router_wizard.get_current_user", return_value={"email": MEMBER_EMAIL}),
        patch("opi.web.router_wizard.get_flow", return_value=MagicMock()),
        patch("opi.web.router_wizard.get_templates", return_value=MagicMock()),
        pytest.raises(HTTPException) as exc,
    ):
        await wizard_edit_page(  # @requires_sso just delegates; calling the wrapper is fine
            request=_request_for(MEMBER_EMAIL),
            flow_id="edit-project",
            project_name=PROJECT_NAME,
        )

    assert exc.value.status_code == 403
    gate.assert_called_once()


@pytest.mark.asyncio
async def test_owner_save_blocks_nested_config_secret_exfiltration(project_service: ProjectService) -> None:
    """A payload rewriting only a nested ``config`` secret must not stick.

    The whole ``config`` block is re-derived from storage, so neither the
    api-key nor the AGE private key can be replaced via form output even
    when the rest of ``config`` is left intact in the payload.
    """
    captured: dict[str, Any] = {}

    def fake_save(file_path: str, project_data: dict[str, Any]) -> None:
        captured["data"] = project_data

    payload = {
        "display-name": "Same Name",
        "config": {
            "api-key": "attacker-key",
            "age-public-key": "age1publicpublicpublic",
            "age-private-key": "AGE-SECRET-KEY-STOLEN",
        },
    }

    with (
        patch("opi.web.router_detail_edit.get_current_user", return_value={"email": OWNER_EMAIL}),
        patch("opi.handlers.project_file_handler.save_project_file", side_effect=fake_save),
        patch.object(project_service, "load_project_from_data", return_value=True),
        patch("opi.web.router_wizard.clear_wizard_state"),
    ):
        await _save_existing_project(_request_for(OWNER_EMAIL), PROJECT_NAME, payload)

    saved = captured["data"]
    assert saved["config"]["api-key"] == "super-secret-api-key"
    assert saved["config"]["age-private-key"] == "AGE-SECRET-KEY-PRIVATEPRIVATE"


# ---------------------------------------------------------------------------
# Central helpers: merge_preserving_protected_keys
# ---------------------------------------------------------------------------


def test_merge_preserves_all_protected_keys() -> None:
    """The merge helper re-derives every protected key from existing_data."""
    existing = {
        "users": [{"email": "a@x", "role": "owner"}],
        "config": {"api-key": "real"},
        "name": "real-name",
        "clusters": ["odcn"],
        "display-name": "Real",
    }
    submitted = {
        "users": [{"email": "attacker@x", "role": "owner"}],
        "config": {"api-key": "stolen"},
        "name": "attacker-name",
        "clusters": ["attacker"],
        "display-name": "Updated",
    }
    merged = merge_preserving_protected_keys(existing, submitted)

    assert merged["users"] == existing["users"]
    assert merged["config"] == existing["config"]
    assert merged["name"] == existing["name"]
    assert merged["clusters"] == existing["clusters"]
    assert merged["display-name"] == "Updated"


def test_merge_drops_protected_keys_absent_from_existing() -> None:
    """A submitted protected key cannot introduce itself when it was absent."""
    existing = {"display-name": "Just a name"}
    submitted = {"users": [{"email": "attacker@x", "role": "owner"}]}
    merged = merge_preserving_protected_keys(existing, submitted)
    assert "users" not in merged


def test_merge_returns_new_dict() -> None:
    """The helper must not mutate either input dict."""
    existing = {"users": [{"email": "owner@x"}]}
    submitted = {"users": [{"email": "attacker@x"}], "display-name": "X"}
    merged = merge_preserving_protected_keys(existing, submitted)

    assert merged is not existing
    assert merged is not submitted
    assert existing == {"users": [{"email": "owner@x"}]}
    assert submitted == {"users": [{"email": "attacker@x"}], "display-name": "X"}


def test_protected_keys_constant_covers_the_documented_set() -> None:
    """Lock the protected key set so a silent removal here surfaces in review."""
    assert set(PROTECTED_PROJECT_KEYS) == {"users", "config", "name", "clusters"}


# ---------------------------------------------------------------------------
# Modal save (router_detail_edit._modal_do_submit): TOCTOU re-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modal_do_submit_invokes_role_gate_for_project_edit(project_service: ProjectService) -> None:
    """`_modal_do_submit` must re-check admin/owner on project-edit flows.

    The route handlers above already gate, but a session-replay between
    GET-time gate and final mutation must still fail. Pinning the call here
    guards against accidental removal of the in-handler re-check.
    """
    from opi.web.router_detail_edit import _modal_do_submit

    sentinel = HTTPException(status_code=403, detail="gate-fired-from-test")

    # Patch the binding inside router_detail_edit (top-level import in that
    # module created a local name); patching the source module would miss it.
    with (
        patch(
            "opi.web.router_detail_edit.require_project_edit_access",
            side_effect=sentinel,
        ) as gate,
        pytest.raises(HTTPException) as exc,
    ):
        await _modal_do_submit(
            request=_request_for(MEMBER_EMAIL),
            wizard_token="any-token",
            project_name=PROJECT_NAME,
            flow_id="modal-edit-identity",  # not a backup-restore flow
        )

    assert exc.value.status_code == 403
    gate.assert_called_once()


@pytest.mark.asyncio
async def test_modal_do_submit_skips_edit_gate_on_backup_restore_flow(project_service: ProjectService) -> None:
    """Backup/restore flows use a separate member-level gate, not edit-gate."""
    from opi.web.router_detail_edit import _modal_do_submit

    with (
        patch(
            "opi.web.router_detail_edit.require_project_edit_access",
            side_effect=AssertionError("edit-gate must not run on backup flows"),
        ),
        # State lookup returns None -> raises 400 before any data mutation.
        # We only care that the edit-gate was NOT called.
        patch("opi.web.router_detail_edit.get_modal_state_by_token", return_value=None),
        pytest.raises(HTTPException) as exc,
    ):
        await _modal_do_submit(
            request=_request_for(MEMBER_EMAIL),
            wizard_token="any-token",
            project_name=PROJECT_NAME,
            flow_id="modal-backup",
        )
    # Confirm the expected pass-through error rather than an AssertionError leaking.
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Wizard step submit (router_wizard.submit_step): edit-mode role gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_step_gates_in_edit_mode(project_service: ProjectService) -> None:
    """In edit-mode (state.project_name set), each step submit must re-gate.

    A member who somehow seeded an edit-mode wizard state must be rejected
    before any step data is processed. The final-save handler already gates,
    but step submits accumulate validated data and a member should fail fast.
    """
    from unittest.mock import MagicMock

    from opi.web.router_wizard import submit_step

    state = MagicMock()
    state.flow_id = "edit-project"
    state.project_name = PROJECT_NAME

    sentinel = HTTPException(status_code=403, detail="gate-fired-from-test")

    with (
        patch("opi.web.router_wizard.get_wizard_state", return_value=state),
        patch(
            "opi.web.project_edit_security.require_project_edit_access",
            side_effect=sentinel,
        ) as gate,
        pytest.raises(HTTPException) as exc,
    ):
        await submit_step(
            request=_request_for(MEMBER_EMAIL),
            flow_id="edit-project",
            section_id="identity-edit",
        )

    assert exc.value.status_code == 403
    gate.assert_called_once()


@pytest.mark.asyncio
async def test_submit_step_skips_gate_in_create_mode(project_service: ProjectService) -> None:
    """Create-mode (state.project_name is None) has no project to gate against."""
    from unittest.mock import MagicMock

    from opi.web.router_wizard import submit_step

    state = MagicMock()
    state.flow_id = "create-project"
    state.project_name = None

    with (
        patch("opi.web.router_wizard.get_wizard_state", return_value=state),
        patch(
            "opi.web.project_edit_security.require_project_edit_access",
            side_effect=AssertionError("gate must not run in create-mode"),
        ),
    ):
        # Body parse fails downstream (no real request body). What matters
        # is which exception we get: anything except AssertionError means the
        # gate was correctly skipped in create-mode.
        with pytest.raises(Exception, match=r"^(?!.*gate must not run).*") as exc:
            await submit_step(
                request=_request_for(MEMBER_EMAIL),
                flow_id="create-project",
                section_id="identity",
            )
        assert not isinstance(exc.value, AssertionError)
