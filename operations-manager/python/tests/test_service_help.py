"""Every service explains itself, and it does so from one place.

Two failures this guards, both silent:

* A service without ``help_template`` simply renders no question mark. Nothing
  raises, nothing logs -- the same failure mode as a non-existent icon (see
  ``tests/test_menu_icons_exist.py``). Same for a ``help_template`` pointing at a
  file that does not exist: the button appears and the modal shows "kon niet
  geladen worden".
* The service picker and the service overview each used to build their own
  service block, and drifted: the overview lost the help button entirely. They
  now share ``service_block`` from ``widgets/_macros.html.j2``, and this checks
  they keep sharing it. The components section of the project page is the third
  user: it showed the very same services as a line of raw names, without icon,
  description or the question mark.
"""

from __future__ import annotations

import pathlib

import opi
import pytest
from opi.core.templates import get_templates
from opi.services import ServiceType
from opi.services.help_text import render_service_help
from opi.services.services import ServiceAdapter

_TEMPLATE_ROOT = pathlib.Path(opi.__file__).parent / "templates"
# A service's explanation lives in that service's own package (RC-36); the loader has
# the catalog directory on its search path, so a help_template resolves against either.
_CATALOG_ROOT = pathlib.Path(opi.__file__).parent / "services" / "catalog"
_HELP_ROOT = _TEMPLATE_ROOT / "help"


def _help_file(help_template: str) -> pathlib.Path | None:
    """The file a help_template resolves to, or None when it resolves to nothing."""
    for root in (_TEMPLATE_ROOT, _CATALOG_ROOT, _HELP_ROOT):
        candidate = root / help_template
        if candidate.is_file():
            return candidate
    return None


# Every template that renders a service block. All of them go through the macro.
# Het gaat om het bestand waar de markup staat: toen services-overview.html.j2 een
# samenstelling werd, verhuisde het serviceblok mee naar het deeltemplate.
_SERVICE_BLOCK_USERS = (
    "widgets/service_cards.html.j2",
    "services-overview/_diensten.html.j2",
    "project-details/section-components.html.j2",
)


def _source(name: str) -> str:
    return (_TEMPLATE_ROOT / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# One macro
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", _SERVICE_BLOCK_USERS)
def test_service_block_is_rendered_through_the_macro(template_name: str) -> None:
    source = _source(template_name)
    assert "service_block" in source, (
        f"{template_name} must import and call the service_block macro from widgets/_macros.html.j2"
    )
    assert "service_block(" in source, f"{template_name} imports service_block but never calls it"


@pytest.mark.parametrize("template_name", _SERVICE_BLOCK_USERS)
def test_service_block_users_do_not_render_their_own_service_icon(template_name: str) -> None:
    """The icon, name, description and help button live in the macro, nowhere else.

    Without this the two templates drift apart again, which is exactly what
    happened before: only one of them grew the help button.
    """
    source = _source(template_name)
    for forbidden in (
        "openServiceHelp(",
        "service-card__title",
        "service-card__help-btn",
        "service-card__description",
    ):
        assert forbidden not in source, (
            f"{template_name} renders its own service block ({forbidden!r}); use the service_block macro instead"
        )


def test_the_macro_only_shows_the_help_button_when_there_is_help() -> None:
    macro = get_templates().get_template("widgets/_macros.html.j2").module.service_block  # type: ignore[attr-defined]

    with_help = str(macro("Naam", "Omschrijving", "sleutel", "groen", "keycloak/help.md"))
    without_help = str(macro("Naam", "Omschrijving", "sleutel", "groen", None))

    assert "openServiceHelp('keycloak/help.md')" in with_help
    assert "service-card__help-btn" in with_help
    assert "service-card__help-btn" not in without_help
    for rendered in (with_help, without_help):
        assert "Naam" in rendered
        assert "Omschrijving" in rendered
        assert "rvo-icon-sleutel" in rendered


# ---------------------------------------------------------------------------
# Every service has an explanation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", sorted(ServiceType, key=lambda s: s.value), ids=lambda s: s.value)
def test_every_service_has_a_help_template(service: ServiceType) -> None:
    definition = ServiceAdapter.get_service_definition(service)
    assert definition.help_template, (
        f"service {service.value!r} has no help_template, so it shows no explanation and no "
        f"question mark -- add a help.md to its package and point at it"
    )


@pytest.mark.parametrize("service", sorted(ServiceType, key=lambda s: s.value), ids=lambda s: s.value)
def test_the_help_template_of_every_service_exists(service: ServiceType) -> None:
    """A missing file fails silently: the button renders, the modal shows an error."""
    help_template = ServiceAdapter.get_service_definition(service).help_template
    assert help_template is not None
    assert _help_file(help_template) is not None, (
        f"service {service.value!r} points at {help_template}, which does not exist"
    )


@pytest.mark.parametrize("service", sorted(ServiceType, key=lambda s: s.value), ids=lambda s: s.value)
def test_the_help_template_of_every_service_renders(service: ServiceType) -> None:
    """The modal fetches the help at click time, so a broken one is only seen there."""
    help_template = ServiceAdapter.get_service_definition(service).help_template
    assert help_template is not None
    rendered = render_service_help(help_template)
    assert rendered.strip(), f"{help_template} renders empty"
    assert "<c-" not in rendered, (
        f"{help_template} still contains an unprocessed ROOS component; check the component name"
    )


@pytest.mark.parametrize("service", sorted(ServiceType, key=lambda s: s.value), ids=lambda s: s.value)
def test_the_help_template_uses_the_icon_of_its_service(service: ServiceType) -> None:
    """The card and the modal should look like they belong together.

    Not a matter of the prose remembering to say so any more: the icon is taken from the
    definition when the markdown is rendered, so the two cannot come apart.
    """
    definition = ServiceAdapter.get_service_definition(service)
    assert definition.help_template is not None
    rendered = render_service_help(definition.help_template)
    assert f"rvo-icon-{definition.icon}" in rendered, (
        f"{definition.help_template} does not show the service icon {definition.icon!r}"
    )
