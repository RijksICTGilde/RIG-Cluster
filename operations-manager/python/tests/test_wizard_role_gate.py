"""Regression tests for the wizard-edit privilege-escalation fix.

The full-page wizard edit flow used to only check
``is_user_authorized_for_project`` (true for any role, including a plain
member) and then mass-merged the form output over the stored project with
``existing_data.update(data)``. A member could therefore enter the wizard
and rewrite the ``users`` list to make themselves owner.

These tests pin:

- a plain ``member`` is rejected from the wizard edit GET entry and from
  the mutating save path (role gate);
- an ``owner`` (and global ``admin``) passes the gate and can update the
  legitimately-editable fields including ``users`` and ``config``;
- a payload that tries to set the immutable fields ``name`` / ``clusters``
  is rejected with 400 -- no form should expose those, so seeing them in
  submitted data is a structural-integrity violation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from opi.services.project_service import ProjectService, ProjectUser
from opi.services.user_service import get_user_service
from opi.web.project_edit_security import (
    IMMUTABLE_PROJECT_FIELDS,
    apply_form_data_to_project,
    require_project_edit_access,
)
from opi.web.router_wizard import _save_existing_project

PROJECT_NAME = "takeover-target"

OWNER_EMAIL = "owner@example.com"
MEMBER_EMAIL = "member@example.com"
ADMIN_EMAIL = "global-admin@example.com"

# The wizard save path now schema-checks the finished file before handing it to the
# store, so the secrets in this fixture have to look like what the schema demands:
# AGE-encrypted, not plaintext placeholders.
AGE_BLOB = "-----BEGIN AGE ENCRYPTED FILE-----\nfake\n-----END AGE ENCRYPTED FILE-----"

STORED_DATA: dict[str, Any] = {
    "name": PROJECT_NAME,
    "display-name": "Takeover Target",
    "clusters": ["odcn-production"],
    "users": [
        {"email": OWNER_EMAIL, "role": "owner"},
        {"email": MEMBER_EMAIL, "role": "member"},
    ],
    "config": {
        "api-key": AGE_BLOB,
        "age-public-key": "age1publicpublicpublic",
        "age-private-key": AGE_BLOB,
    },
    "components": [{"name": "frontend", "image": "nginx:latest"}],
}


def _patch_project_manager(captured: dict[str, Any]):
    """Patch ProjectManager so _save_existing_project reads STORED_DATA fresh via
    the single-load-path (get_contents) and captures what the single-save-path
    (save_and_commit_project) persists, without touching real git/kubectl connectors.
    """
    import copy

    class _FakeProjectManager:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def get_contents(self) -> dict[str, Any]:
            return copy.deepcopy(STORED_DATA)

        async def save_and_commit_project(
            self, project_data: dict[str, Any], commit_message: str, **_kwargs: Any
        ) -> None:
            captured["data"] = project_data

        async def close(self) -> None:
            pass

    return patch("opi.manager.project_manager.ProjectManager", _FakeProjectManager)


@pytest.fixture
def project_service() -> ProjectService:
    """Singleton ProjectService seeded with one project, cleaned up after."""
    service = ProjectService()
    saved_projects = dict(service._projects)
    saved_admins = set(get_user_service()._platform_admin_emails)
    service._projects.clear()
    get_user_service()._platform_admin_emails.clear()
    get_user_service()._platform_admin_emails.add(ADMIN_EMAIL)
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
    get_user_service()._platform_admin_emails.clear()
    get_user_service()._platform_admin_emails.update(saved_admins)


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
async def test_owner_save_rejects_immutable_field_in_payload(project_service: ProjectService) -> None:
    """A payload that tries to set an immutable field (name/clusters) must
    be rejected with 400. No form exposes these fields, so any submission
    containing them is a form bug or a tampered request.
    """
    payload = {
        "display-name": "Renamed By Owner",
        "name": "evil-rename",
    }
    captured: dict[str, Any] = {}
    with (
        patch("opi.web.project_edit_security.get_current_user", return_value={"email": OWNER_EMAIL}),
        _patch_project_manager(captured),
        pytest.raises(HTTPException) as exc,
    ):
        await _save_existing_project(_request_for(OWNER_EMAIL), PROJECT_NAME, payload)
    assert exc.value.status_code == 400
    assert "name" in exc.value.detail
    # The forbidden payload must be rejected before anything is persisted.
    assert "data" not in captured


@pytest.mark.asyncio
async def test_owner_save_rejects_clusters_in_payload(project_service: ProjectService) -> None:
    """Same as above for ``clusters``; cluster editing post-creation is not
    yet a supported feature.
    """
    payload = {
        "display-name": "Same Name",
        "clusters": ["attacker-cluster"],
    }
    captured: dict[str, Any] = {}
    with (
        patch("opi.web.project_edit_security.get_current_user", return_value={"email": OWNER_EMAIL}),
        _patch_project_manager(captured),
        pytest.raises(HTTPException) as exc,
    ):
        await _save_existing_project(_request_for(OWNER_EMAIL), PROJECT_NAME, payload)
    assert exc.value.status_code == 400
    assert "clusters" in exc.value.detail
    # The forbidden payload must be rejected before anything is persisted.
    assert "data" not in captured


@pytest.mark.asyncio
async def test_owner_save_can_update_users_and_config(project_service: ProjectService) -> None:
    """Admin/owner CAN edit users and config via the legitimate flows.

    Role-based access is enforced by ``require_project_edit_access``;
    once you are admin/owner there is no separate field-level lock on
    these. Field-level RBAC for finer-grained restrictions is tracked
    in ``features/futures/form-field-rbac.md``.
    """
    captured: dict[str, Any] = {}

    payload = {
        "display-name": "Same Name",
        "users": [
            {"email": OWNER_EMAIL, "role": "owner"},
            {"email": "new-mate@example.com", "role": "member"},
        ],
        "config": {"api-key": AGE_BLOB, "age-public-key": "age1publicpublicpublic"},
    }
    with (
        patch("opi.web.project_edit_security.get_current_user", return_value={"email": OWNER_EMAIL}),
        _patch_project_manager(captured),
        patch("opi.web.router_wizard.clear_wizard_state"),
    ):
        response = await _save_existing_project(_request_for(OWNER_EMAIL), PROJECT_NAME, payload)

    assert response.status_code == 200
    saved = captured["data"]
    assert saved["users"] == payload["users"]
    assert saved["config"] == payload["config"]
    # Immutable fields stay untouched (re-derived from existing project).
    assert saved["name"] == PROJECT_NAME
    assert saved["clusters"] == STORED_DATA["clusters"]


# ---------------------------------------------------------------------------
# Route-level gate: the GET handler wires through require_project_edit_access
#
# The helper-level tests above prove the gate function itself rejects a
# member. This test pins the route -> gate wiring so a future refactor
# cannot silently drop the gate call from `wizard_edit_page` without a test
# failure. The route does light setup (get_flow / get_current_user /
# de templateomgeving) before calling the gate, all mocked here.
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
        patch("opi.web.router_wizard.templates_lotc", MagicMock()),
        pytest.raises(HTTPException) as exc,
    ):
        await wizard_edit_page(  # @requires_sso just delegates; calling the wrapper is fine
            request=_request_for(MEMBER_EMAIL),
            flow_id="edit-project",
            project_name=PROJECT_NAME,
        )

    assert exc.value.status_code == 403
    gate.assert_called_once()


# ---------------------------------------------------------------------------
# Central helper: apply_form_data_to_project
# ---------------------------------------------------------------------------


def test_apply_form_data_raises_on_immutable_field() -> None:
    """Submitted data containing an immutable field is rejected loudly."""
    existing = {"name": "real-name", "clusters": ["odcn"], "display-name": "Real"}
    for field in IMMUTABLE_PROJECT_FIELDS:
        with pytest.raises(HTTPException) as exc:
            apply_form_data_to_project(existing, {field: "anything", "display-name": "X"})
        assert exc.value.status_code == 400
        assert field in exc.value.detail


def test_apply_form_data_passes_non_immutable_fields_through() -> None:
    """Users/config/components/display-name go through; immutable fields stay."""
    existing = {
        "name": "real-name",
        "clusters": ["odcn"],
        "users": [{"email": "a@x", "role": "owner"}],
        "config": {"api-key": "real"},
        "display-name": "Old",
    }
    submitted = {
        "users": [{"email": "a@x", "role": "owner"}, {"email": "b@x", "role": "member"}],
        "config": {"api-key": "rotated"},
        "display-name": "New",
        "components": [{"name": "frontend"}],
    }
    merged = apply_form_data_to_project(existing, submitted)

    assert merged["users"] == submitted["users"]
    assert merged["config"] == submitted["config"]
    assert merged["display-name"] == "New"
    assert merged["components"] == submitted["components"]
    # Immutable fields are re-derived from the stored project.
    assert merged["name"] == "real-name"
    assert merged["clusters"] == ["odcn"]


def test_apply_form_data_returns_new_dict() -> None:
    """The helper must not mutate either input dict."""
    existing = {"users": [{"email": "owner@x"}]}
    submitted = {"users": [{"email": "attacker@x"}], "display-name": "X"}
    merged = apply_form_data_to_project(existing, submitted)

    assert merged is not existing
    assert merged is not submitted
    assert existing == {"users": [{"email": "owner@x"}]}
    assert submitted == {"users": [{"email": "attacker@x"}], "display-name": "X"}


def test_immutable_fields_constant_covers_the_documented_set() -> None:
    """Lock the immutable field set so a silent change here surfaces in review."""
    assert set(IMMUTABLE_PROJECT_FIELDS) == {"name", "clusters"}


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
