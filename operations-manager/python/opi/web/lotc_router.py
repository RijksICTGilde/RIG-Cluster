"""Routes of the LOTC build line, next to the existing pages.

A converted page is reachable at ``/lotc/<path>`` while the original stays at ``<path>``, so
the two can be laid side by side. The screenshot tests run on exactly that pair.

This router is PUBLIC: no route here carries ``requires_sso``, so everything ``page_data()``
supplies is out in the open. Visibly made-up values only, the admin pages included: a threshold
or the list of which platform services go unmeasured belongs behind
``require_platform_admin`` and not here.

``opi/server.py`` therefore registers it only when ENVIRONMENT is ``local`` or
``sandboxed-local``; in production these paths do not exist.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.errors import log_render_failure
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.http.render.navigation_lotc import get_navigation
from opi.http.render.templates_lotc import TEMPLATES_LOTC_DIR, templates_lotc
from opi.web.lotc_fixtures import page_data
from opi.web.lotc_form_preview import EXAMPLE_FIELDS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lotc", tags=["lotc"])

#: The user the preview shows when nobody is signed in.
PREVIEW_USER = {"email": "beheerder@voorbeeld.nl", "name": "Voorbeeldbeheerder"}


def _previewable_pages() -> dict[str, str]:
    """The converted pages that render with only the navigation, so without page data.

    The result is an allowlist, built at startup. Without it the page name from the URL would
    become a template path directly, and a ``../`` would render any template on the search path.
    """
    pages: dict[str, str] = {}
    for path in sorted(TEMPLATES_LOTC_DIR.rglob("*.j2")):
        name = str(path.relative_to(TEMPLATES_LOTC_DIR))
        if 'extends "base_lotc.html.j2"' not in path.read_text():
            continue
        slug = str(Path(name).with_suffix("")).removesuffix(".html")
        pages[slug] = name
    return pages


PREVIEWABLE_PAGES = _previewable_pages()


def _context(request: Request, **extra: object) -> dict[str, object]:
    """The base context for a LOTC page.

    The menu items come from ``opi/http/render/menu.py``, which stays the one source of which
    links exist and who may see them; ``navigation_lotc`` only rearranges them.
    """
    user = request.session.get("user") if hasattr(request, "session") else None
    # Without a user the menu holds only "Inloggen", which is the least informative state for
    # a preview meant to judge the DESIGN, so it falls back on a visibly made-up user.
    user = user or PREVIEW_USER
    return {
        "request": request,
        "navigation": get_navigation(user, current_path=request.url.path.removeprefix("/lotc")),
        **extra,
    }


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def lotc_index(request: Request) -> HTMLResponse:
    """The shell itself, with no page content: the smallest check on the whole tool chain."""
    return templates_lotc.TemplateResponse(request, "base_lotc.html.j2", _context(request))


@router.get("/pagina/{slug:path}", response_class=HTMLResponse, include_in_schema=False)
async def lotc_page(request: Request, slug: str) -> HTMLResponse:
    """Show a converted page, so it can be laid next to the original.

    Pages from the allowlist only: an unknown name is a 404 and not an attempt to load. A page
    that does exist but needs page data answers 422 with the reason.
    """
    template_name = PREVIEWABLE_PAGES.get(slug)
    if template_name is None:
        raise HTTPException(status_code=404, detail=f"onbekende pagina: {slug}")
    try:
        return templates_lotc.TemplateResponse(request, template_name, _context(request))
    except Exception as error:
        log_render_failure(logger, f"voorbeeldpagina {template_name}", error)
        raise HTTPException(
            status_code=422,
            detail=f"{template_name} rendert nog niet zonder paginadata. De reden staat in de log.",
        ) from error


@router.get("/formulier", response_class=HTMLResponse, include_in_schema=False)
async def lotc_form_preview(request: Request) -> HTMLResponse:
    """The converted form layer, rendered from the example fields.

    The wizard needs a real project, so this route is the only way to look at the form layer.
    """
    adapter = LOTCWidgetAdapter()
    rendered_fields = [adapter.render_field(field) for field in EXAMPLE_FIELDS]
    return templates_lotc.TemplateResponse(
        request,
        "form-preview.html.j2",
        _context(request, rendered_fields=rendered_fields),
    )


# Example data for the redesigned dashboard, drawn from the example projects so there is no
# second made-up reality next to them.
_ACTIVITY = [
    {
        "icon": "plus",
        "actor": "Voorbeeldbeheerder",
        "action": "project aangemaakt",
        "resource": "voorbeeld-klein",
        "at": "vandaag 09:12",
    },
    {
        "icon": "arrow-up-arrow-down",
        "actor": "Voorbeeldontwikkelaar",
        "action": "deployment uitgerold",
        "resource": "voorbeeld-volledig / productie",
        "at": "gisteren 16:40",
    },
    {
        "icon": "lock-closed",
        "actor": "Voorbeeldbeheerder",
        "action": "sleutel vernieuwd",
        "resource": "voorbeeld-volledig / api-key",
        "at": "gisteren 11:05",
    },
]


def _redesigned_pages() -> dict[str, str]:
    """The redesigned pages in bg form, as an allowlist built at startup.

    Same reason as for the converted pages: without it the name from the URL would become a
    template path directly.
    """
    directory = TEMPLATES_LOTC_DIR / "bg"
    return {
        path.name.removesuffix(".html.j2"): f"bg/{path.name}"
        for path in sorted(directory.glob("*.j2"))
        # A leading underscore marks a shared partial, which has no content of its own.
        if not path.name.startswith("_")
    }


REDESIGNED_PAGES = _redesigned_pages()


@router.get("/bg/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def lotc_redesigned_page(request: Request, slug: str) -> HTMLResponse:
    """A page as bg.rijks.app would build it, next to the converted version."""
    template_name = REDESIGNED_PAGES.get(slug)
    if template_name is None:
        raise HTTPException(status_code=404, detail=f"onbekende pagina: {slug}")

    data = page_data(slug)

    # The filter on the services page sits in the URL, so it is shareable and works without
    # JavaScript.
    if "service_filters" in data:
        chosen = request.query_params.get("kind", "")
        data["service_filter"] = chosen
        if chosen == "system":
            data["services"] = [service for service in data["services"] if service["kind_label"]]
        elif chosen == "user":
            data["services"] = [service for service in data["services"] if not service["kind_label"]]

    tabs = data.get("tabs")
    if tabs:
        # The tabs are real links, and an unknown name falls back on the first one rather than
        # failing: a wrongly shared link should show the page.
        requested = request.query_params.get("tab", "")
        data["active_tab"] = requested if requested in tabs else next(iter(tabs))
        # The REAL page gives every tab a path of its own; this preview has no project, so it
        # keeps the ``?tab=`` on this one demo URL. The template reads ``tab.url`` either way.
        data["tabs"] = {key: {**tab, "url": f"/lotc/bg/{slug}?tab={key}"} for key, tab in tabs.items()}

    return templates_lotc.TemplateResponse(request, template_name, _context(request, **data))
