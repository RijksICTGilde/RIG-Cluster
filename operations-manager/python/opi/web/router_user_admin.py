"""Web routes for platform user administration (CRUD)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from opi.core.auth_decorators import require_platform_admin, requires_sso
from opi.forms import FormRenderer, get_default_nl_translator
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.user_editables import USER_SECTION
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.services.user_admin_service import UserAdminService
from opi.services.user_service import get_user_service
from opi.utils.csrf import ensure_csrf_token
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

user_admin_router = APIRouter(prefix="/admin/users", tags=["user-admin"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_service() -> UserAdminService:
    return UserAdminService()


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
    """Render the user form fields HTML from editables.

    De adapter rendert meteen af, ook de stapel eromheen (``render_flow``), dus die string
    mag NIET nog een keer door een sjabloonrender: hij draagt wat iemand in het formulier
    heeft getypt, en dat hoort geen Jinja te worden.
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


def _user_form_response(
    request: Request,
    user: dict,
    page_heading: str,
    form_action: str,
    data: dict,
    errors: dict | None = None,
    edit_mode: bool = False,
) -> Response:
    """Het gebruikersformulier, in de weergave die dit verzoek krijgt.

    Alle vijf de plekken die dit formulier tonen (aanmaken, bewerken, en de drie keer dat
    het met fouten terugkomt) lopen hierlangs, zodat de keuze tussen de twee weergaven op
    een plek staat en niet vijf keer meegeschreven hoeft te worden.
    """
    from opi.web.lotc_switch import build_lotc_admin, render

    return render(
        request,
        template="bg/admin-user-form.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "page_heading": page_heading,
            "form_action": form_action,
            "form_html": _render_form_html(data=data, errors=errors, edit_mode=edit_mode),
            "csrf_token": ensure_csrf_token(request),
            **build_lotc_admin(user=user, current_path="/admin/users"),
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@user_admin_router.get("", response_class=HTMLResponse)
@requires_sso
async def list_users(request: Request) -> Response:
    """List all platform users."""
    user = require_platform_admin(request)
    service = _get_service()
    users = await service.list_users()

    csrf_token = ensure_csrf_token(request)

    success_message = request.query_params.get("success")

    # Dezelfde gegevens, twee weergaven; zie opi/web/lotc_switch.py.
    from opi.web.lotc_switch import build_lotc_admin, render

    return render(
        request,
        template="bg/admin-users.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "users": users,
            "csrf_token": csrf_token,
            "success_message": success_message,
            **build_lotc_admin(user=user, current_path="/admin/users"),
        },
    )


@user_admin_router.get("/create", response_class=HTMLResponse)
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
@requires_sso
async def create_user_submit(request: Request) -> Response:
    """Process the create user form."""
    user = require_platform_admin(request)
    form_data = await request.form()
    submitted = dict(form_data)

    # Validate via processor
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

    # Sync: add new email to the in-memory allowlist
    get_user_service().add_allowed_email(new_email)

    return RedirectResponse(
        url="/admin/users?success=Gebruiker+aangemaakt",
        status_code=303,
    )


@user_admin_router.get("/{user_id}/edit", response_class=HTMLResponse)
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
@requires_sso
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

    # Sync: if email changed, update the allowlist
    user_service = get_user_service()
    if old_email.lower() != new_email.lower():
        user_service.remove_allowed_email(old_email)
    user_service.add_allowed_email(new_email)

    return RedirectResponse(
        url="/admin/users?success=Gebruiker+bijgewerkt",
        status_code=303,
    )


@user_admin_router.post("/{user_id}/delete", response_model=None)
@requires_sso
async def delete_user(request: Request, user_id: str) -> Response:
    """Delete a user."""
    require_platform_admin(request)
    service = _get_service()

    # Get email before deleting so we can remove from allowlist
    existing = await service.get_user(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    deleted = await service.delete_user(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    # Sync: remove email from the allowlist
    get_user_service().remove_allowed_email(existing["email"])

    return RedirectResponse(
        url="/admin/users?success=Gebruiker+verwijderd",
        status_code=303,
    )
