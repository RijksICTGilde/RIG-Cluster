"""Web routes for platform user administration (CRUD)."""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from opi.core.authz.actions import Action, action
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.user_editables import USER_SECTION
from opi.forms.i18n import get_default_nl_translator
from opi.forms.renderer import FormRenderer
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.http.auth.auth_decorators import require_platform_admin, requires_sso, requires_step_up
from opi.http.auth.step_up_conditions import always_requires_step_up
from opi.http.render.menu import get_menu_items
from opi.services.session_revocation_service import revoke_sessions
from opi.services.step_up import StepUpAction
from opi.services.user_admin_service import UserAdminService
from opi.services.user_otp_service import UserOtpService
from opi.services.user_service import get_user_service
from opi.utils.csrf import ensure_csrf_token

logger = logging.getLogger(__name__)

user_admin_router = APIRouter(prefix="/admin/users", tags=["user-admin"])

#: The fields the step-up dialog has to repeat for this form, because the dialog takes the
#: place of the form. Read from the form definition, so an added field comes along.
_FORM_FIELDS = tuple(visualizer.editable.yaml_path for visualizer in USER_SECTION.editables)


def _get_service() -> UserAdminService:
    return UserAdminService()


def _get_otp_service() -> UserOtpService:
    return UserOtpService()


def _create_renderer() -> FormRenderer:
    return FormRenderer(
        widget_adapter=LOTCWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _render_form_html(
    data: dict,
    errors: dict | None = None,
    edit_mode: bool = False,
) -> str:
    """Render the user form fields HTML from the editables.

    The adapter renders the whole stack at once, so the result must NOT go through a template
    render again: it carries what someone typed into the form, and that is not to become Jinja.
    """
    renderer = _create_renderer()

    html = renderer.render_fields_from_editables(
        editables=USER_SECTION.editables,
        yaml_data=data,
        layout=USER_SECTION.layout,
        errors=errors,
        edit_mode=edit_mode,
    )
    return html


def _back_to_list(request: Request, message: str) -> Response:
    """Back to the list, as a full navigation under htmx.

    The forms post with htmx because the step-up puts a dialog in the place of the form. A 303
    would leave htmx hanging the whole page in that dialog, so the refresh goes as HX-Redirect,
    the same road as ``router_tokens._back_to_team``.
    """
    url = f"/admin/users?success={quote_plus(message)}"
    if request.headers.get("HX-Request"):
        return HTMLResponse(content="", headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


def _user_form_response(
    request: Request,
    user: dict,
    page_heading: str,
    form_action: str,
    data: dict,
    errors: dict | None = None,
    edit_mode: bool = False,
) -> Response:
    """The user form, in the shape this request gets.

    An htmx request gets the form only, and it replaces itself where it stands: on the page, or
    in the dialog when it ended up there after the step-up. A whole page does not fit in that
    dialog.
    """
    from opi.http.render.lotc_switch import build_lotc_admin, render, render_fragment

    fields = {
        "request": request,
        "form_action": form_action,
        "form_html": _render_form_html(data=data, errors=errors, edit_mode=edit_mode),
        "csrf_token": ensure_csrf_token(request),
    }
    if request.headers.get("HX-Request"):
        return HTMLResponse(content=render_fragment(request, template="bg/_admin-user-form.html.j2", context=fields))

    return render(
        request,
        template="bg/admin-user-form.html.j2",
        context={
            **fields,
            "menu_items": get_menu_items(user),
            "page_heading": page_heading,
            **build_lotc_admin(user=user, current_path="/admin/users"),
        },
    )


@user_admin_router.get("", response_class=HTMLResponse)
@action(Action.PLATFORM_USERS)
@requires_sso
async def list_users(request: Request) -> Response:
    """List all platform users."""
    user = require_platform_admin(request)
    service = _get_service()
    users = await service.list_users()

    csrf_token = ensure_csrf_token(request)

    success_message = request.query_params.get("success")

    from opi.http.render.lotc_switch import build_lotc_admin, render

    return render(
        request,
        template="bg/admin-users.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "users": users,
            "csrf_token": csrf_token,
            "success_message": success_message,
            "eigen_email": str(user.get("email", "")).lower(),
            **build_lotc_admin(user=user, current_path="/admin/users"),
        },
    )


@user_admin_router.get("/create", response_class=HTMLResponse)
@action(Action.PLATFORM_USERS)
@requires_sso
async def create_user_form(request: Request) -> Response:
    """Show the create user form."""
    user = require_platform_admin(request)
    return _user_form_response(
        request,
        user,
        page_heading="Gebruiker toevoegen",
        form_action="/admin/users/create",
        data={},
    )


@user_admin_router.post("/create", response_model=None)
@action(Action.PLATFORM_USERS)
@requires_sso
@requires_step_up(always_requires_step_up, action=StepUpAction.USER_CREATE, fields=_FORM_FIELDS)
async def create_user_submit(request: Request) -> Response:
    """Process the create user form."""
    user = require_platform_admin(request)
    form_data = await request.form()
    submitted = dict(form_data)

    processor = EditableFormProcessor()
    result, errors = await processor.process_json_submission(
        submitted=submitted,
        editables=USER_SECTION.editables,
        yaml_data={},
        edit_mode=False,
    )

    if errors:
        return _user_form_response(
            request,
            user,
            page_heading="Gebruiker toevoegen",
            form_action="/admin/users/create",
            data=submitted,
            errors=errors,
        )

    service = _get_service()
    new_email = result.get("email", "").strip()
    try:
        await service.create_user(
            email=new_email,
            full_name=result.get("full_name", "").strip(),
        )
    except IntegrityError:
        errors = {"email": ["Er bestaat al een gebruiker met dit e-mailadres"]}
        return _user_form_response(
            request,
            user,
            page_heading="Gebruiker toevoegen",
            form_action="/admin/users/create",
            data=submitted,
            errors=errors,
        )

    get_user_service().add_allowed_email(new_email)

    return _back_to_list(request, "Gebruiker aangemaakt")


@user_admin_router.get("/{user_id}/edit", response_class=HTMLResponse)
@action(Action.PLATFORM_USERS)
@requires_sso
async def edit_user_form(request: Request, user_id: str) -> Response:
    """Show the edit user form, pre-filled."""
    user = require_platform_admin(request)
    service = _get_service()
    existing = await service.get_user(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    return _user_form_response(
        request,
        user,
        page_heading="Gebruiker bewerken",
        form_action=f"/admin/users/{user_id}/edit",
        data=existing,
        edit_mode=True,
    )


@user_admin_router.post("/{user_id}/edit", response_model=None)
@action(Action.PLATFORM_USERS)
@requires_sso
@requires_step_up(always_requires_step_up, action=StepUpAction.USER_EDIT, fields=_FORM_FIELDS)
async def edit_user_submit(request: Request, user_id: str) -> Response:
    """Process the edit user form."""
    user = require_platform_admin(request)
    service = _get_service()
    existing = await service.get_user(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    form_data = await request.form()
    submitted = dict(form_data)

    processor = EditableFormProcessor()
    result, errors = await processor.process_json_submission(
        submitted=submitted,
        editables=USER_SECTION.editables,
        yaml_data=existing,
        edit_mode=True,
    )

    if errors:
        return _user_form_response(
            request,
            user,
            page_heading="Gebruiker bewerken",
            form_action=f"/admin/users/{user_id}/edit",
            data=submitted,
            errors=errors,
            edit_mode=True,
        )

    old_email = existing.get("email", "")
    new_email = result.get("email", "").strip()
    try:
        updated = await service.update_user(
            user_id=user_id,
            email=new_email,
            full_name=result.get("full_name", "").strip(),
        )
    except IntegrityError:
        errors = {"email": ["Er bestaat al een gebruiker met dit e-mailadres"]}
        return _user_form_response(
            request,
            user,
            page_heading="Gebruiker bewerken",
            form_action=f"/admin/users/{user_id}/edit",
            data=submitted,
            errors=errors,
            edit_mode=True,
        )

    if not updated:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    user_service = get_user_service()
    if old_email.lower() != new_email.lower():
        user_service.remove_allowed_email(old_email)
    user_service.add_allowed_email(new_email)

    return _back_to_list(request, "Gebruiker bijgewerkt")


@user_admin_router.post("/{user_id}/revoke-sessions", response_model=None)
@action(Action.PLATFORM_USERS)
@requires_sso
@requires_step_up(always_requires_step_up, action=StepUpAction.USER_SESSIONS_REVOKE)
async def revoke_user_sessions(request: Request, user_id: str) -> Response:
    """End every running portal session of this user."""
    admin = require_platform_admin(request)
    service = _get_service()
    existing = await service.get_user(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    revoked_after = await revoke_sessions(existing["email"])
    # Logged here with who, for whom and from when, until there is an events table.
    logger.info(
        "Platformbeheerder %s beëindigde de sessies van %s vanaf %s",
        admin.get("email", "onbekend"),
        existing["email"],
        revoked_after.isoformat(),
    )

    return _back_to_list(request, "Sessies beëindigd")


@user_admin_router.post("/{user_id}/delete", response_model=None)
@action(Action.PLATFORM_USERS)
@requires_sso
@requires_step_up(always_requires_step_up, action=StepUpAction.USER_DELETE)
async def delete_user(request: Request, user_id: str) -> Response:
    """Delete a user."""
    require_platform_admin(request)
    service = _get_service()

    existing = await service.get_user(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    deleted = await service.delete_user(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    get_user_service().remove_allowed_email(existing["email"])

    return _back_to_list(request, "Gebruiker verwijderd")


@user_admin_router.post("/{user_id}/otp-reset", response_model=None)
@action(Action.PLATFORM_USERS)
@requires_sso
@requires_step_up(always_requires_step_up, action=StepUpAction.USER_OTP_RESET)
async def reset_user_otp(request: Request, user_id: str) -> Response:
    """Clear the second factor of ANOTHER user, for someone who can no longer get a code.

    Which columns this clears is written down in "features/otp-op-de-zad-gebruiker.md", so a
    manual intervention does the same.

    Never for yourself: clearing your own factor without a second factor would turn any
    hijacked admin session into a way around that factor. Replacing it is possible on
    /account/otp, and that asks for a code from the existing factor.
    """
    admin = require_platform_admin(request)
    service = _get_service()
    existing = await service.get_user(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    by_email = str(admin.get("email", "")).lower()
    if existing["email"].lower() == by_email:
        raise HTTPException(status_code=403, detail="Je eigen 2-factor reset je op /account/otp")

    await _get_otp_service().reset(existing["email"], by=by_email)
    return _back_to_list(request, "Tweede factor gereset")
