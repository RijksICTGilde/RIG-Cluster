"""Web routes for platform user administration (CRUD)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from asyncpg import UniqueViolationError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

if TYPE_CHECKING:
    from starlette.responses import Response

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.database_pools import get_database_pool
from opi.core.templates import get_templates
from opi.forms import FormRenderer, ROOSWidgetAdapter, get_default_nl_translator
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.user_editables import USER_SECTION
from opi.services.project_service import get_project_service
from opi.services.user_admin_service import UserAdminService
from opi.utils.csrf import ensure_csrf_token
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

user_admin_router = APIRouter(prefix="/admin/users", tags=["user-admin"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_service() -> UserAdminService:
    pool = get_database_pool("main")
    return UserAdminService(pool)


def _require_admin(request: Request) -> dict:
    """Return the current user dict or raise 403 if not an admin."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Niet ingelogd")
    email = user.get("email", "").lower()
    project_service = get_project_service()
    if not project_service.is_admin(email):
        raise HTTPException(status_code=403, detail="Alleen beheerders hebben toegang")
    return user


def _create_renderer() -> FormRenderer:
    return FormRenderer(
        widget_adapter=ROOSWidgetAdapter(),
        translator=get_default_nl_translator(),
    )


def _render_form_html(
    data: dict,
    errors: dict | None = None,
    edit_mode: bool = False,
) -> str:
    """Render the user form fields HTML from editables."""
    renderer = _create_renderer()
    html = renderer.render_fields_from_editables(
        editables=USER_SECTION.editables,
        yaml_data=data,
        layout=USER_SECTION.layout,
        errors=errors,
        edit_mode=edit_mode,
    )
    templates = get_templates()
    process_components = templates.env.filters.get("process_components")
    if process_components is not None:
        html = str(process_components(html))
    return html


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@user_admin_router.get("", response_class=HTMLResponse)
@requires_sso
async def list_users(request: Request) -> HTMLResponse:
    """List all platform users."""
    user = _require_admin(request)
    service = _get_service()
    users = await service.list_users()

    templates = get_templates()
    csrf_token = ensure_csrf_token(request)

    success_message = request.query_params.get("success")

    return templates.TemplateResponse(
        "admin/users.html.j2",
        {
            "request": request,
            "menu_items": get_menu_items(user, is_admin=True),
            "users": users,
            "csrf_token": csrf_token,
            "success_message": success_message,
        },
    )


@user_admin_router.get("/create", response_class=HTMLResponse)
@requires_sso
async def create_user_form(request: Request) -> HTMLResponse:
    """Show the create user form."""
    user = _require_admin(request)
    form_html = _render_form_html(data={})

    templates = get_templates()
    csrf_token = ensure_csrf_token(request)

    return templates.TemplateResponse(
        "admin/user-form.html.j2",
        {
            "request": request,
            "menu_items": get_menu_items(user, is_admin=True),
            "page_heading": "Gebruiker toevoegen",
            "form_action": "/admin/users/create",
            "form_html": form_html,
            "csrf_token": csrf_token,
        },
    )


@user_admin_router.post("/create", response_model=None)
@requires_sso
async def create_user_submit(request: Request) -> Response:
    """Process the create user form."""
    user = _require_admin(request)
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
        form_html = _render_form_html(data=submitted, errors=errors)
        templates = get_templates()
        csrf_token = ensure_csrf_token(request)
        return templates.TemplateResponse(
            "admin/user-form.html.j2",
            {
                "request": request,
                "menu_items": get_menu_items(user, is_admin=True),
                "page_heading": "Gebruiker toevoegen",
                "form_action": "/admin/users/create",
                "form_html": form_html,
                "csrf_token": csrf_token,
            },
        )

    service = _get_service()
    try:
        await service.create_user(
            email=result.get("email", "").strip(),
            full_name=result.get("full_name", "").strip(),
        )
    except UniqueViolationError:
        errors = {"email": ["Er bestaat al een gebruiker met dit e-mailadres"]}
        form_html = _render_form_html(data=submitted, errors=errors)
        templates = get_templates()
        csrf_token = ensure_csrf_token(request)
        return templates.TemplateResponse(
            "admin/user-form.html.j2",
            {
                "request": request,
                "menu_items": get_menu_items(user, is_admin=True),
                "page_heading": "Gebruiker toevoegen",
                "form_action": "/admin/users/create",
                "form_html": form_html,
                "csrf_token": csrf_token,
            },
        )

    return RedirectResponse(
        url="/admin/users?success=Gebruiker+aangemaakt",
        status_code=303,
    )


@user_admin_router.get("/{user_id}/edit", response_class=HTMLResponse)
@requires_sso
async def edit_user_form(request: Request, user_id: str) -> HTMLResponse:
    """Show the edit user form, pre-filled."""
    user = _require_admin(request)
    service = _get_service()
    existing = await service.get_user(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    form_html = _render_form_html(data=existing, edit_mode=True)

    templates = get_templates()
    csrf_token = ensure_csrf_token(request)

    return templates.TemplateResponse(
        "admin/user-form.html.j2",
        {
            "request": request,
            "menu_items": get_menu_items(user, is_admin=True),
            "page_heading": "Gebruiker bewerken",
            "form_action": f"/admin/users/{user_id}/edit",
            "form_html": form_html,
            "csrf_token": csrf_token,
        },
    )


@user_admin_router.post("/{user_id}/edit", response_model=None)
@requires_sso
async def edit_user_submit(request: Request, user_id: str) -> Response:
    """Process the edit user form."""
    user = _require_admin(request)
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
        form_html = _render_form_html(data=submitted, errors=errors, edit_mode=True)
        templates = get_templates()
        csrf_token = ensure_csrf_token(request)
        return templates.TemplateResponse(
            "admin/user-form.html.j2",
            {
                "request": request,
                "menu_items": get_menu_items(user, is_admin=True),
                "page_heading": "Gebruiker bewerken",
                "form_action": f"/admin/users/{user_id}/edit",
                "form_html": form_html,
                "csrf_token": csrf_token,
            },
        )

    try:
        updated = await service.update_user(
            user_id=user_id,
            email=result.get("email", "").strip(),
            full_name=result.get("full_name", "").strip(),
        )
    except UniqueViolationError:
        errors = {"email": ["Er bestaat al een gebruiker met dit e-mailadres"]}
        form_html = _render_form_html(data=submitted, errors=errors, edit_mode=True)
        templates = get_templates()
        csrf_token = ensure_csrf_token(request)
        return templates.TemplateResponse(
            "admin/user-form.html.j2",
            {
                "request": request,
                "menu_items": get_menu_items(user, is_admin=True),
                "page_heading": "Gebruiker bewerken",
                "form_action": f"/admin/users/{user_id}/edit",
                "form_html": form_html,
                "csrf_token": csrf_token,
            },
        )

    if not updated:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    return RedirectResponse(
        url="/admin/users?success=Gebruiker+bijgewerkt",
        status_code=303,
    )


@user_admin_router.post("/{user_id}/delete", response_model=None)
@requires_sso
async def delete_user(request: Request, user_id: str) -> Response:
    """Delete a user."""
    _require_admin(request)
    service = _get_service()
    deleted = await service.delete_user(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    return RedirectResponse(
        url="/admin/users?success=Gebruiker+verwijderd",
        status_code=303,
    )
