"""Web routes for platform user administration (CRUD)."""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from starlette.responses import Response

from opi.core.auth_decorators import require_platform_admin, requires_sso, requires_step_up
from opi.core.authz.actions import Action, action
from opi.core.step_up import StepUpAction
from opi.forms import FormRenderer, get_default_nl_translator
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.user_editables import USER_SECTION
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.services.session_revocation_service import revoke_sessions
from opi.services.step_up_conditions import vraagt_altijd_step_up
from opi.services.user_admin_service import UserAdminService
from opi.services.user_otp_service import UserOtpService
from opi.services.user_service import get_user_service
from opi.utils.csrf import ensure_csrf_token
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

user_admin_router = APIRouter(prefix="/admin/users", tags=["user-admin"])

#: De velden die de step-up-dialoog voor dit formulier moet herhalen: de dialoog neemt de
#: plaats van het formulier in. Uit de formulierdefinitie, zodat een veld erbij meekomt.
_FORMULIERVELDEN = tuple(visualizer.editable.yaml_path for visualizer in USER_SECTION.editables)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _terug(request: Request, melding: str) -> Response:
    """Terug naar de lijst, en onder htmx als volledige navigatie.

    De formulieren posten met htmx omdat de step-up een dialoog in de plaats van het
    formulier zet. Een 303 zou htmx de hele pagina in die dialoog laten hangen, dus gaat
    de verversing als HX-Redirect; dezelfde weg als ``router_tokens._terug``.
    """
    url = f"/admin/users?success={quote_plus(melding)}"
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
    """Het gebruikersformulier, in de weergave die dit verzoek krijgt.

    Alle vijf de plekken die dit formulier tonen (aanmaken, bewerken, en de drie keer dat
    het met fouten terugkomt) lopen hierlangs, zodat de keuze tussen de twee weergaven op
    een plek staat en niet vijf keer meegeschreven hoeft te worden.

    Een htmx-verzoek krijgt alleen het formulier terug, en dat vervangt zichzelf waar het
    staat: op de pagina, of in de dialoog als het daar na de step-up terechtkwam. Een hele
    pagina past in die dialoog niet.
    """
    from opi.web.lotc_switch import build_lotc_admin, render, render_fragment

    velden = {
        "request": request,
        "form_action": form_action,
        "form_html": _render_form_html(data=data, errors=errors, edit_mode=edit_mode),
        "csrf_token": ensure_csrf_token(request),
    }
    if request.headers.get("HX-Request"):
        return HTMLResponse(content=render_fragment(request, template="bg/_admin-user-form.html.j2", context=velden))

    return render(
        request,
        template="bg/admin-user-form.html.j2",
        context={
            **velden,
            "menu_items": get_menu_items(user),
            "page_heading": page_heading,
            **build_lotc_admin(user=user, current_path="/admin/users"),
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


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
@requires_step_up(vraagt_altijd_step_up, actie=StepUpAction.USER_CREATE, velden=_FORMULIERVELDEN)
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

    return _terug(request, "Gebruiker aangemaakt")


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
@requires_step_up(vraagt_altijd_step_up, actie=StepUpAction.USER_EDIT, velden=_FORMULIERVELDEN)
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

    return _terug(request, "Gebruiker bijgewerkt")


@user_admin_router.post("/{user_id}/revoke-sessions", response_model=None)
@action(Action.PLATFORM_USERS)
@requires_sso
@requires_step_up(vraagt_altijd_step_up, actie=StepUpAction.USER_SESSIONS_REVOKE)
async def revoke_user_sessions(request: Request, user_id: str) -> Response:
    """Beëindig elke lopende portaalsessie van deze gebruiker."""
    admin = require_platform_admin(request)
    service = _get_service()
    existing = await service.get_user(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    revoked_after = await revoke_sessions(existing["email"])
    # Deze handeling hoort in de gebeurtenissentabel zodra die er is; tot die tijd staat
    # hij hier, met wie, voor wie en vanaf wanneer.
    logger.info(
        "Platformbeheerder %s beëindigde de sessies van %s vanaf %s",
        admin.get("email", "onbekend"),
        existing["email"],
        revoked_after.isoformat(),
    )

    return _terug(request, "Sessies beëindigd")


@user_admin_router.post("/{user_id}/delete", response_model=None)
@action(Action.PLATFORM_USERS)
@requires_sso
@requires_step_up(vraagt_altijd_step_up, actie=StepUpAction.USER_DELETE)
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

    return _terug(request, "Gebruiker verwijderd")


@user_admin_router.post("/{user_id}/otp-reset", response_model=None)
@action(Action.PLATFORM_USERS)
@requires_sso
@requires_step_up(vraagt_altijd_step_up, actie=StepUpAction.USER_OTP_RESET)
async def reset_user_otp(request: Request, user_id: str) -> Response:
    """Maak de tweede factor van een ANDERE gebruiker leeg.

    Voor wie geen code meer uit zijn app kan halen. Welke kolommen dit leegmaakt
    staat in ``features/otp-op-de-zad-gebruiker.md``, zodat een handmatige ingreep
    hetzelfde doet.

    Niet voor jezelf: je eigen factor leegmaken zonder tweede factor zou van elke
    overgenomen beheerderssessie een weg om die factor heen maken. Vervangen kan wel, op
    /account/otp, en dat vraagt een code van de bestaande factor.
    """
    beheerder = require_platform_admin(request)
    service = _get_service()
    existing = await service.get_user(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Gebruiker niet gevonden")

    door = str(beheerder.get("email", "")).lower()
    if existing["email"].lower() == door:
        raise HTTPException(status_code=403, detail="Je eigen 2-factor reset je op /account/otp")

    await _get_otp_service().reset(existing["email"], by=door)
    return _terug(request, "Tweede factor gereset")
