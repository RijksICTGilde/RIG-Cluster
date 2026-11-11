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

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.authz.actions import Action
from opi.forms.visualizers.flows import get_flow
from opi.http.auth.authz import authorize
from opi.services.project_service import ProjectService, ProjectUser
from opi.services.project_store import get_project_store
from opi.services.user_service import get_user_service
from opi.web.project_edit_security import IMMUTABLE_PROJECT_FIELDS, refuse_immutable_changes
from opi.web.router_wizard import _save_existing_project

PROJECT_NAME = "takeover-target"

OWNER_EMAIL = "owner@example.com"
MEMBER_EMAIL = "member@example.com"
ADMIN_EMAIL = "global-admin@example.com"
# Alleen voor de operationele-rolgrens (RC-323): niet in STORED_DATA, dat bepaalt de
# inhoud van het projectbestand voor de save-tests en blijft ongewijzigd.
DEVELOPER_EMAIL = "developer@example.com"

# The wizard save path now schema-checks the finished file before handing it to the
# store, so the secrets in this fixture have to look like what the schema demands:
# AGE-encrypted, not plaintext placeholders.
AGE_BLOB = "-----BEGIN AGE ENCRYPTED FILE-----\nfake\n-----END AGE ENCRYPTED FILE-----"

STORED_DATA: dict[str, Any] = {
    "name": PROJECT_NAME,
    "display-name": "Takeover Target",
    "clusters": ["odcn-production"],
    "users": [
        {"email": OWNER_EMAIL, "roles": ["admin", "ops"]},
        {"email": MEMBER_EMAIL, "roles": ["viewer"]},
    ],
    "config": {
        "age-public-key": "age1publicpublicpublic",
        "age-private-key": AGE_BLOB,
    },
    "components": [{"name": "frontend", "image": "nginx:latest"}],
}

#: The sections of the flow these saves run: their editables name the writable paths.
EDIT_SECTIONS = list(get_flow("edit-project").sections)


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
        api_key=None,
        filename=f"{PROJECT_NAME}.yaml",
        users=[
            ProjectUser(email=OWNER_EMAIL, role="owner"),
            ProjectUser(email=MEMBER_EMAIL, role="member"),
            ProjectUser(email=DEVELOPER_EMAIL, role="developer"),
        ],
        data=dict(STORED_DATA),
    )
    yield service
    service._projects.clear()
    service._projects.update(saved_projects)
    get_user_service()._platform_admin_emails.clear()
    get_user_service()._platform_admin_emails.update(saved_admins)


def _request_for(email: str, *, verse_step_up: bool = False) -> Any:
    """Minimal fake request whose state.user yields the given email.

    ``verse_step_up`` zet de stempel in de sessie: het wijzigen van de leden vraagt er
    sinds RC-392 een, ook via de wizard.
    """
    sessie: dict[str, Any] = {}
    if verse_step_up:
        sessie["step_up_at"] = datetime.now(UTC).isoformat()
    return SimpleNamespace(state=SimpleNamespace(user={"email": email}), session=sessie, app=_task_queue())


def _task_queue() -> Any:
    """A queue that accepts the rollout task a wizard save now starts (RC-483 P1).

    One instance for every fake request, so two requests built the same way still compare
    equal.
    """
    return _TASK_QUEUE


def _build_task_queue() -> Any:
    service = MagicMock()
    service.create_task = AsyncMock(return_value={"task_id": "rollout-1"})
    return SimpleNamespace(state=SimpleNamespace(task_service=service))


_TASK_QUEUE = _build_task_queue()


# ---------------------------------------------------------------------------
# Role gate (mirrors detail-edit, reused by wizard_edit_page)
# ---------------------------------------------------------------------------


def test_member_rejected_from_wizard_edit_gate(project_service: ProjectService) -> None:
    """A plain member must be denied entry to the wizard edit flow."""
    with (
        patch("opi.web.router_detail_edit.get_current_user", return_value={"email": MEMBER_EMAIL}),
        pytest.raises(HTTPException) as exc,
    ):
        authorize(_request_for(MEMBER_EMAIL), action=Action.PROJECT_EDIT, project_name=PROJECT_NAME)
    assert exc.value.status_code == 403


def test_owner_passes_wizard_edit_gate(project_service: ProjectService) -> None:
    """An owner is allowed into the wizard edit flow."""
    with patch("opi.web.router_detail_edit.get_current_user", return_value={"email": OWNER_EMAIL}):
        project, user_email = authorize(
            _request_for(OWNER_EMAIL), action=Action.PROJECT_EDIT, project_name=PROJECT_NAME
        )
    assert project.name == PROJECT_NAME
    assert user_email == OWNER_EMAIL


def test_global_admin_passes_wizard_edit_gate(project_service: ProjectService) -> None:
    """A global admin is allowed into the wizard edit flow."""
    with patch("opi.web.router_detail_edit.get_current_user", return_value={"email": ADMIN_EMAIL}):
        project, _ = authorize(_request_for(ADMIN_EMAIL), action=Action.PROJECT_EDIT, project_name=PROJECT_NAME)
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
        "users": [{"email": MEMBER_EMAIL, "roles": ["admin", "ops"]}],
    }
    with (
        patch("opi.web.router_detail_edit.get_current_user", return_value={"email": MEMBER_EMAIL}),
        pytest.raises(HTTPException) as exc,
    ):
        await _save_existing_project(
            _request_for(MEMBER_EMAIL), PROJECT_NAME, malicious_payload, active_sections=EDIT_SECTIONS
        )
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
        patch("opi.http.auth.authz.get_current_user", return_value={"email": OWNER_EMAIL}),
        _patch_project_manager(captured),
        pytest.raises(HTTPException) as exc,
    ):
        await _save_existing_project(_request_for(OWNER_EMAIL), PROJECT_NAME, payload, active_sections=EDIT_SECTIONS)
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
        patch("opi.http.auth.authz.get_current_user", return_value={"email": OWNER_EMAIL}),
        _patch_project_manager(captured),
        pytest.raises(HTTPException) as exc,
    ):
        await _save_existing_project(_request_for(OWNER_EMAIL), PROJECT_NAME, payload, active_sections=EDIT_SECTIONS)
    assert exc.value.status_code == 400
    assert "clusters" in exc.value.detail
    # The forbidden payload must be rejected before anything is persisted.
    assert "data" not in captured


@pytest.mark.asyncio
async def test_developer_kan_zichzelf_geen_admin_maken_via_de_wizard(project_service: ProjectService) -> None:
    """Plan paragraaf 7, bewijs 1: een developer die de POST stuurt krijgt 403 en het
    bestand verandert niet. Ook via de algemene wizard-opslagweg, die ``users`` net zo
    goed kan bevatten: die vraagt ``member.manage`` zodra de ledenlijst wijzigt.
    """
    captured: dict[str, Any] = {}
    payload = {
        "display-name": "Takeover Target",
        "users": [
            {"email": OWNER_EMAIL, "roles": ["admin", "ops"]},
            {"email": DEVELOPER_EMAIL, "roles": ["admin", "ops"]},
        ],
    }
    with (
        patch("opi.http.auth.authz.get_current_user", return_value={"email": DEVELOPER_EMAIL}),
        _patch_project_manager(captured),
        patch("opi.web.router_wizard.clear_wizard_state"),
        pytest.raises(HTTPException) as exc,
    ):
        await _save_existing_project(
            _request_for(DEVELOPER_EMAIL, verse_step_up=True), PROJECT_NAME, payload, active_sections=EDIT_SECTIONS
        )

    assert exc.value.status_code == 403
    assert "data" not in captured, "het bestand mag niet veranderen"


@pytest.mark.asyncio
async def test_developer_save_zonder_ledenwijziging_vraagt_geen_member_manage(
    project_service: ProjectService,
) -> None:
    """De rolpoort (RC-392) vraagt ``member.manage`` alleen zodra de ledenlijst wijzigt.

    Een developer die een veld opslaat dat ``users`` niet raakt, mag dat op een project
    zonder productie zonder step-up: ``project.edit`` heeft hij al. Zonder deze test zou
    een regressie die de poort onvoorwaardelijk maakt (altijd ``member.manage`` eisen)
    pas aan het licht komen als elke developer-opslag 403 geeft.
    """
    captured: dict[str, Any] = {}
    payload = {"display-name": "Nieuwe titel"}

    with (
        patch("opi.http.auth.authz.get_current_user", return_value={"email": DEVELOPER_EMAIL}),
        _patch_project_manager(captured),
        patch("opi.web.router_wizard.clear_wizard_state"),
    ):
        response = await _save_existing_project(
            _request_for(DEVELOPER_EMAIL), PROJECT_NAME, payload, active_sections=EDIT_SECTIONS
        )

    assert response.status_code == 200
    assert captured["data"]["users"] == STORED_DATA["users"]
    assert captured["data"]["display-name"] == "Nieuwe titel"


@pytest.mark.asyncio
async def test_owner_save_can_update_users_and_config(project_service: ProjectService) -> None:
    """Admin/owner CAN edit users and config via the legitimate flows.

    Role-based access is enforced by ``authorize`` (opi/http/auth/authz.py);
    once you are admin/owner there is no separate field-level lock on
    these. Field-level RBAC for finer-grained restrictions is tracked
    in ``features/futures/form-field-rbac.md``.
    """
    captured: dict[str, Any] = {}

    payload = {
        "display-name": "Same Name",
        "users": [
            {"email": OWNER_EMAIL, "roles": ["admin", "ops"]},
            {"email": "new-mate@example.com", "roles": ["viewer"]},
        ],
        "config": {"age-public-key": "age1publicpublicpublic"},
    }
    with (
        patch("opi.http.auth.authz.get_current_user", return_value={"email": OWNER_EMAIL}),
        _patch_project_manager(captured),
        patch("opi.web.router_wizard.clear_wizard_state"),
    ):
        response = await _save_existing_project(
            _request_for(OWNER_EMAIL, verse_step_up=True), PROJECT_NAME, payload, active_sections=EDIT_SECTIONS
        )

    assert response.status_code == 200
    saved = captured["data"]
    assert saved["users"] == payload["users"]
    assert saved["config"] == payload["config"]
    # Immutable fields stay untouched (re-derived from existing project).
    assert saved["name"] == PROJECT_NAME
    assert saved["clusters"] == STORED_DATA["clusters"]


# ---------------------------------------------------------------------------
# Route-level gate: the GET handler carries @action(project.edit)
#
# The helper-level tests above prove the gate function itself rejects a
# member. Sinds RC-398 weegt ``enforce_route_action`` die action als
# router-dependency, en leest de handler alleen nog wat de poort toeliet. Wat hier
# gepind blijft is dus de MARKERING: zonder die action gaat de route langs de poort
# heen, en dan is het lek van deze testbundel terug.
# ---------------------------------------------------------------------------


def test_wizard_edit_page_draagt_de_bewerkaction() -> None:
    from opi.core.authz.actions import action_of
    from opi.web.router_wizard import wizard_edit_page

    assert action_of(wizard_edit_page) is Action.PROJECT_EDIT


def _route_request(email: str, endpoint: Any) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": email}),
        session={},
        headers={},
        cookies={},
        path_params={"project_name": PROJECT_NAME},
        query_params={},
        scope={"route": SimpleNamespace(endpoint=endpoint)},
    )


@pytest.mark.asyncio
async def test_wizard_edit_page_leest_het_project_dat_de_poort_al_toeliet(
    project_service: ProjectService,
) -> None:
    """De GET gaat door ``enforce_route_action``; ``wizard_edit_page`` vraagt daarna
    niets meer aan ``authorize``, maar leest het project dat de poort al toeliet.

    Draait de echte handler, niet alleen de poort: een vergeten of verkeerd bedrade
    ``authorized_project`` in de body zou hier een 500 of een verkeerd project geven.
    """
    from opi.http.auth.authz import enforce_route_action
    from opi.web.router_wizard import wizard_edit_page

    request = _route_request(OWNER_EMAIL, wizard_edit_page)
    await enforce_route_action(request)

    with patch("opi.web.router_wizard.render", lambda _r, template, context: context):
        pagina = await wizard_edit_page(request, "edit-project", PROJECT_NAME)

    assert pagina["project_name"] == PROJECT_NAME


@pytest.mark.asyncio
async def test_wizard_edit_page_poort_weigert_een_lid(project_service: ProjectService) -> None:
    """Dezelfde weigering als ``test_member_rejected_from_wizard_edit_gate``, nu op de
    echte router-dependency: een lid komt de route niet meer in, ook al roept
    ``wizard_edit_page`` zelf geen ``authorize`` meer aan.
    """
    from opi.http.auth.authz import enforce_route_action
    from opi.web.router_wizard import wizard_edit_page

    with pytest.raises(HTTPException) as exc:
        await enforce_route_action(_route_request(MEMBER_EMAIL, wizard_edit_page))
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Central helper: refuse_immutable_changes
# ---------------------------------------------------------------------------


def test_refuse_immutable_changes_raises_on_a_changed_field() -> None:
    """Submitted data containing a CHANGED immutable field is rejected loudly."""
    existing = {"name": "real-name", "clusters": ["odcn"], "display-name": "Real"}
    for field in IMMUTABLE_PROJECT_FIELDS:
        with pytest.raises(HTTPException) as exc:
            refuse_immutable_changes(existing, {field: "anything", "display-name": "X"})
        assert exc.value.status_code == 400
        assert field in exc.value.detail


def test_refuse_immutable_changes_allows_an_unchanged_field() -> None:
    """Merged wizard data always carries the stored immutable fields; that is not a write.

    De bewerkwizard zaaide zijn sessie uit het projectbestand, dus ``clusters`` zit er
    altijd exact zo in als opgeslagen. De poort weigert een WIJZIGING, niet een herhaling
    van de stand - anders kan de wizard helemaal niet meer opslaan (gemeten op de
    sandbox: elke no-op save eindigde in een 400, RC-388).
    """
    existing = {"name": "real-name", "clusters": ["odcn"], "display-name": "Real"}
    refuse_immutable_changes(existing, {"name": "real-name", "clusters": ["odcn"], "display-name": "Nieuw"})


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
            "opi.web.router_detail_edit.authorize",
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
    """Een back-upstroom vraagt backup.create, niet het bredere project.edit.

    Since RC-475 it weighs that on a deployment, so the gate of the flow is
    ``require_on_any_deployment`` and not the project-wide ``authorize``.
    """
    from opi.web.router_detail_edit import _modal_do_submit

    with (
        patch("opi.web.router_detail_edit.require_on_any_deployment") as gate,
        patch(
            "opi.web.router_detail_edit.authorize",
            side_effect=AssertionError("a backup flow must not ask a project-wide gate"),
        ),
        # State lookup returns None -> raises 400 before any data mutation.
        patch("opi.web.router_detail_edit.load_modal_state_for", return_value=None),
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
    gate.assert_called_once_with(_request_for(MEMBER_EMAIL), Action.BACKUP_CREATE, PROJECT_NAME)


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
            "opi.web.router_wizard.authorize",
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
            "opi.web.router_wizard.authorize",
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


# ---------------------------------------------------------------------------
# Backup/restore flow-role split (RC-323): the flow gate
#
# modal-backup stays membership-gated for every role. modal-restore moved to the
# operational-role gate (developer/owner/admin, not member) because a restore
# overwrites a deployment's data or writes a new one into the project file. Every
# other modal-wizard flow keeps the admin/owner edit gate.
# ---------------------------------------------------------------------------


def test_authorize_flow_backup_allows_developer(project_service: ProjectService) -> None:
    """Een back-up maken is operationeel: een developer mag het, een viewer niet.

    Het portaal liet hier elk projectlid door; sinds RC-392 schrijft een viewer niets
    (ontwerp paragraaf 4), dus dit versmalt naar developer.
    """
    from opi.web.router_detail_edit import _authorize_flow

    project, user_email = _authorize_flow(_request_for(DEVELOPER_EMAIL), PROJECT_NAME, "modal-backup")
    assert project.name == PROJECT_NAME
    assert user_email == DEVELOPER_EMAIL


def test_authorize_flow_backup_rejects_a_viewer(project_service: ProjectService) -> None:
    from opi.web.router_detail_edit import _authorize_flow

    with pytest.raises(HTTPException) as exc:
        _authorize_flow(_request_for(MEMBER_EMAIL), PROJECT_NAME, "modal-backup")
    assert exc.value.status_code == 403


def test_authorize_flow_restore_rejects_member(project_service: ProjectService) -> None:
    from opi.web.router_detail_edit import _authorize_flow

    with pytest.raises(HTTPException) as exc:
        _authorize_flow(_request_for(MEMBER_EMAIL), PROJECT_NAME, "modal-restore")
    assert exc.value.status_code == 403


def test_authorize_flow_restore_allows_developer(project_service: ProjectService) -> None:
    """developer is not admin/owner, so this proves the operate-gate runs here,
    not the stricter edit-gate (which would also happen to accept owner/admin).
    """
    from opi.web.router_detail_edit import _authorize_flow

    project, user_email = _authorize_flow(_request_for(DEVELOPER_EMAIL), PROJECT_NAME, "modal-restore")
    assert project.name == PROJECT_NAME
    assert user_email == DEVELOPER_EMAIL


def test_authorize_flow_other_flow_weegt_of_het_project_productie_heeft(
    project_service: ProjectService,
) -> None:
    """Elke stroom buiten back-up en terugzetten bewerkt het projectbestand.

    Sinds RC-392 is dat ``project.edit``, en dat weegt of het PROJECT productie heeft
    (ontwerp paragraaf 4, rij 10 en 11): zonder productie mag een developer het, met
    productie vraagt het ops. Het fixtureproject heeft geen deployment op productie.
    """
    from opi.web.router_detail_edit import _authorize_flow

    project, _email = _authorize_flow(_request_for(DEVELOPER_EMAIL), PROJECT_NAME, "modal-edit-identity")
    assert project.name == PROJECT_NAME

    project.data = {"deployments": [{"name": "live", "environment": "productie"}]}
    with pytest.raises(HTTPException) as exc:
        _authorize_flow(_request_for(DEVELOPER_EMAIL), PROJECT_NAME, "modal-edit-identity")
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Route -> gate wiring for the routes touched by RC-323
#
# `modal_wizard_load_step` (back-navigation) used to always require admin/owner,
# so a member doing backup/restore step back got 403 even though init/submit
# allowed them. These pin that it now goes through the shared navigation gate,
# like the other five modal-wizard routes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modal_wizard_load_step_invokes_flow_access_gate() -> None:
    from opi.web.router_detail_edit import modal_wizard_load_step

    sentinel = HTTPException(status_code=403, detail="gate-fired-from-test")
    request = SimpleNamespace(state=SimpleNamespace(user={"email": MEMBER_EMAIL}), query_params={})

    with (
        patch("opi.web.router_detail_edit._check_flow_access", side_effect=sentinel) as gate,
        pytest.raises(HTTPException) as exc,
    ):
        await modal_wizard_load_step(
            request=request,
            project_name=PROJECT_NAME,
            flow_id="modal-backup",
            section_id="select-deployment",
        )

    assert exc.value.status_code == 403
    gate.assert_called_once_with(request, PROJECT_NAME, "modal-backup")


#: Wat een te smalle rol oplevert, zoals ``require_action`` hem weigert.
_TE_SMALLE_ROL = HTTPException(status_code=403, detail="gate-fired-from-test")


@pytest.mark.asyncio
async def test_backup_select_deployment_invokes_operate_access_gate() -> None:
    """`backup_select_deployment` re-renders a backup step; it must use the
    operational gate, not plain membership (RC-323).
    """
    from opi.web.router_detail_edit import backup_select_deployment

    request = SimpleNamespace(state=SimpleNamespace(user={"email": MEMBER_EMAIL}))

    # ``require_on_any_deployment`` and not ``authorize``: re-rendering the form is not a
    # backup, so the audit gets no row about it (RC-398). On any deployment, because this
    # step is where the user picks one (RC-475).
    with (
        patch("opi.web.router_detail_edit.require_on_any_deployment", side_effect=_TE_SMALLE_ROL) as gate,
        patch("opi.web.router_detail_edit.authorize", side_effect=AssertionError("geen rij hierover")),
        pytest.raises(HTTPException) as exc,
    ):
        await backup_select_deployment(request=request, project_name=PROJECT_NAME)

    assert exc.value.status_code == 403
    gate.assert_called_once_with(request, Action.BACKUP_CREATE, PROJECT_NAME)


@pytest.mark.asyncio
async def test_backup_select_deployment_weighs_a_single_deployment(
    project_service: ProjectService,
) -> None:
    """A developer who may open the wizard may also switch the deployment.

    This step weighs on any deployment, not on the project's highest marking: otherwise
    the wizard opens and this step answers 403 (RC-475).
    """
    from opi.web.router_detail_edit import backup_select_deployment

    request = SimpleNamespace(
        state=SimpleNamespace(user={"email": DEVELOPER_EMAIL}),
        session={},
        query_params={"deployment_name": "test"},
    )
    project = get_project_store().get(PROJECT_NAME)
    assert project is not None
    project.data = {
        "deployments": [
            {"name": "live", "environment": "productie"},
            {"name": "test", "environment": "test"},
        ]
    }

    # No session state, so the step falls on 400 once it is past the gate.
    with (
        patch("opi.web.router_detail_edit.load_modal_state_for", return_value=None),
        pytest.raises(HTTPException) as exc,
    ):
        await backup_select_deployment(request=request, project_name=PROJECT_NAME)
    assert exc.value.status_code == 400

    project.data = {"deployments": [{"name": "live", "environment": "productie"}]}
    with pytest.raises(HTTPException) as exc:
        await backup_select_deployment(request=request, project_name=PROJECT_NAME)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_restore_select_mode_invokes_operate_access_gate() -> None:
    """`restore_select_mode` re-renders a restore step; it must use the
    operational gate, not plain membership (RC-323).
    """
    from opi.web.router_detail_edit import restore_select_mode

    request = SimpleNamespace(state=SimpleNamespace(user={"email": MEMBER_EMAIL}))

    # ``require_on_any_deployment`` en niet ``authorize``: het formulier herrenderen is geen
    # herstel, en de audit hoort er geen rij over te krijgen (RC-398). Op minstens een
    # deployment, want het doel kiest de gebruiker pas in de wizard (RC-417).
    with (
        patch("opi.web.router_detail_edit.require_on_any_deployment", side_effect=_TE_SMALLE_ROL) as gate,
        patch("opi.web.router_detail_edit.authorize", side_effect=AssertionError("geen rij hierover")),
        pytest.raises(HTTPException) as exc,
    ):
        await restore_select_mode(request=request, project_name=PROJECT_NAME)

    assert exc.value.status_code == 403
    gate.assert_called_once_with(request, Action.RESTORE_RUN, PROJECT_NAME)


# ---------------------------------------------------------------------------
# _handle_backup_restore_submit: the gate on trigger_restore
#
# The route-level gates above run at init/step time, and without a deployment. A restore
# is only actually executed here, so this is where ``restore.run`` is weighed on the
# source and on the target it really points at (RC-417); a backup needs no such guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_backup_restore_submit_gates_restore() -> None:
    from unittest.mock import MagicMock

    from opi.web.router_detail_edit import _handle_backup_restore_submit

    state = MagicMock()
    state.get_merged_data.return_value = {}

    sentinel = HTTPException(status_code=403, detail="gate-fired-from-test")
    with (
        patch("opi.http.task_helpers.get_task_service", return_value=MagicMock()),
        patch("opi.http.auth.restore_guard.require_action", side_effect=sentinel) as gate,
        pytest.raises(HTTPException) as exc,
    ):
        await _handle_backup_restore_submit(
            request=_request_for(MEMBER_EMAIL),
            wizard_token="tok",
            project_name=PROJECT_NAME,
            flow_id="modal-restore",
            action="trigger_restore",
            state=state,
        )

    assert exc.value.status_code == 403
    gate.assert_called_once_with(_request_for(MEMBER_EMAIL), Action.RESTORE_RUN, PROJECT_NAME, deployment="")


def _backup_submit_state(resource_types: list[str] | None = None):
    from unittest.mock import MagicMock

    state = MagicMock()
    state.get_merged_data.return_value = {
        "deployment_name": "dep1",
        "resource_types": resource_types if resource_types is not None else ["database"],
    }
    return state


@pytest.mark.asyncio
async def test_handle_backup_restore_submit_weighs_the_chosen_deployment() -> None:
    """The backup weighs on the deployment it names (RC-475, decision B7).

    It used to weigh against the project's highest marking, and not at all on the submit:
    a developer who could not open the wizard in a project with production could back up a
    non-production deployment through the API all the same.
    """
    from unittest.mock import AsyncMock, MagicMock

    from opi.web.router_detail_edit import _handle_backup_restore_submit

    task_service = MagicMock()
    task_service.create_task = AsyncMock(return_value={"task_id": "t1"})

    with (
        patch("opi.http.task_helpers.get_task_service", return_value=task_service),
        patch("opi.web.router_detail_edit.authorize") as gate,
        patch("opi.web.router_detail_edit.render_fragment", return_value="<div>progress</div>"),
        patch("opi.web.router_detail_edit.clear_modal_state_by_token"),
    ):
        response = await _handle_backup_restore_submit(
            request=_request_for(DEVELOPER_EMAIL),
            wizard_token="tok",
            project_name=PROJECT_NAME,
            flow_id="modal-backup",
            action="trigger_backup",
            state=_backup_submit_state(),
        )

    assert response.status_code == 200
    task_service.create_task.assert_called_once()
    assert gate.call_args.kwargs["action"] is Action.BACKUP_CREATE
    assert gate.call_args.kwargs["deployment"] == "dep1"


@pytest.mark.asyncio
async def test_handle_backup_restore_submit_refuses_an_unknown_resource_type() -> None:
    """An unknown resource_type out of the session answers 422 (RC-475, decision B6).

    It used to be passed through raw and ignored by the run, so the backup that was asked
    for did not happen and nobody got to see that.
    """
    from unittest.mock import AsyncMock, MagicMock

    from opi.web.router_detail_edit import _handle_backup_restore_submit

    task_service = MagicMock()
    task_service.create_task = AsyncMock(return_value={"task_id": "t1"})

    with (
        patch("opi.http.task_helpers.get_task_service", return_value=task_service),
        patch("opi.web.router_detail_edit.authorize"),
        patch("opi.web.router_detail_edit.render_fragment", return_value="<div>progress</div>"),
        patch("opi.web.router_detail_edit.clear_modal_state_by_token"),
        pytest.raises(HTTPException) as exc,
    ):
        await _handle_backup_restore_submit(
            request=_request_for(DEVELOPER_EMAIL),
            wizard_token="tok",
            project_name=PROJECT_NAME,
            flow_id="modal-backup",
            action="trigger_backup",
            state=_backup_submit_state(["geen-type"]),
        )

    assert exc.value.status_code == 422
    task_service.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_handle_backup_restore_submit_reads_the_source_from_the_run_and_omits_its_items() -> None:
    """RC-482, decision R11: the submit still derives the SOURCE deployment from the chosen
    run (needed for the authorization weighing above), but no longer carries the run's
    items along -- the task reads them fresh from the backup store when it starts."""
    from unittest.mock import AsyncMock, MagicMock

    from opi.web.router_detail_edit import _handle_backup_restore_submit

    task_service = MagicMock()
    task_service.create_task = AsyncMock(return_value={"task_id": "t1"})

    state = MagicMock()
    state.get_merged_data.return_value = {
        "backup_run_id": "20261103020000",
        "restore_mode": "existing",
        "target_deployment": "staging",
    }
    state.base_data = {
        "_backup_runs": [
            {
                "backup_run_id": "20261103020000",
                "deployment_name": "productie",
                "items": [{"resource_type": "pvc", "snapshot_id": "snap-1"}],
            }
        ]
    }

    with (
        patch("opi.http.task_helpers.get_task_service", return_value=task_service),
        patch("opi.web.router_detail_edit._authorize_restore", AsyncMock(return_value=None)),
        patch("opi.web.router_detail_edit.render_fragment", return_value="<div>progress</div>"),
        patch("opi.web.router_detail_edit.clear_modal_state_by_token"),
    ):
        response = await _handle_backup_restore_submit(
            request=_request_for(DEVELOPER_EMAIL),
            wizard_token="tok",
            project_name=PROJECT_NAME,
            flow_id="modal-restore",
            action="trigger_restore",
            state=state,
        )

    assert response.status_code == 200
    payload = task_service.create_task.call_args.kwargs["payload"]
    assert payload["source_deployment"] == "productie"
    assert payload["target_deployment"] == "staging"
    assert payload["backup_run_id"] == "20261103020000"
    assert "backup_items" not in payload
