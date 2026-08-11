"""One explanation per service, read by the portal and by the API (RC-59).

The prose was written for a popup, in de componentmarkup van het portaal. An agent asking the API what
a service does would have got `utrecht-*` classes and sentences pointing at buttons -- so
the prose is markdown now, and the portal renders it into the same components it always
showed.

Two things have to stay true or the move was not worth making:

* the popup still shows what it showed -- same components, same text, same icon;
* there is exactly ONE file per service. A ``help.md`` next to a ``help.html.j2`` would
  be two sources, and the one nobody renders is the one that goes stale.

The conversion itself is small on purpose (title, sections, paragraphs, bullets, bold);
what is tested is the mapping, the escaping, and that no service is left behind.
"""

from __future__ import annotations

import pathlib
import re

import opi
import pytest
from opi.core.templates_lotc import templates_lotc
from opi.services.help_text import (
    help_file,
    is_markdown_help,
    markdown_to_components,
    render_service_help,
    service_help_markdown,
)
from opi.services.services import ServiceAdapter
from opi.services.services_enums import ServiceType

_CATALOG = pathlib.Path(opi.__file__).parent / "services" / "catalog"
_SERVICES = sorted(ServiceType, key=lambda s: s.value)


def _render(markdown: str, **kwargs) -> str:
    return templates_lotc.env.from_string(markdown_to_components(markdown, **kwargs)).render()


def _icoonnamen(html: str) -> list[str]:
    """De iconnamen in gerenderde HTML.

    Op het GERENDERDE element en niet op de naam die wij meegeven: het componenten-
    systeem lost zijn eigen aliassen op (``database`` wordt ``cylinder-split``), dus een
    vergelijking met onze naam zou daar vals alarm geven.
    """
    return re.findall(r'<nldd-icon[^>]*\bname="([^"]+)"', html)


def _visible(html: str) -> str:
    """The rendered HTML as a browser shows it: the brace entity is a brace on screen."""
    return html.replace("&#123;", "{")


# ---------------------------------------------------------------------------
# One source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", _SERVICES, ids=lambda s: s.value)
def test_every_service_explains_itself_in_markdown(service: ServiceType) -> None:
    definition = ServiceAdapter.get_service_definition(service)
    assert definition.help_template is not None
    assert is_markdown_help(definition.help_template), (
        f"{service.value} still points at {definition.help_template}; the explanation is markdown now"
    )
    assert help_file(definition.help_template) is not None


@pytest.mark.parametrize("service", _SERVICES, ids=lambda s: s.value)
def test_the_explanation_starts_with_a_title_and_says_something(service: ServiceType) -> None:
    markdown = service_help_markdown(service)
    assert markdown.startswith("# "), f"{service.value} has no title"
    assert len(markdown.split()) > 20, f"{service.value} explains itself in a handful of words"


def test_no_service_keeps_a_second_copy_of_its_explanation() -> None:
    """The whole reason markdown became the source instead of a sibling file."""
    leftovers = sorted(path.parent.name for path in _CATALOG.glob("*/help.html.j2"))
    assert leftovers == [], f"these packages have two explanations: {leftovers}"


@pytest.mark.parametrize("service", _SERVICES, ids=lambda s: s.value)
def test_the_api_and_the_portal_read_the_same_file(service: ServiceType) -> None:
    definition = ServiceAdapter.get_service_definition(service)
    assert definition.help_template is not None
    assert service_help_markdown(service) == help_file(definition.help_template).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The conversion
# ---------------------------------------------------------------------------


def test_a_title_becomes_a_heading_with_the_service_icon() -> None:
    rendered = _render("# Redis Cache", icon="zandloper", color="rood")

    assert 'data-lotc-component="heading" size="2"' in rendered
    assert _icoonnamen(rendered) == ["timer"], "zandloper wordt in de NLDD-woordenschat timer"
    assert "Redis Cache" in rendered


def test_a_section_becomes_a_subheading_without_an_icon() -> None:
    rendered = _render("# Titel\n\n## Wanneer gebruik je dit?", icon="klok", color="rood")

    assert 'data-lotc-component="heading" size="3"' in rendered
    assert len(_icoonnamen(rendered)) == 1, "only the title carries the icon"


def test_paragraphs_and_bullets_become_their_components() -> None:
    rendered = _render("Eerste regel\nvan een alinea.\n\n- een\n- twee\n\nEen tweede alinea.")

    assert rendered.count('data-lotc-component="paragraph"') == 2
    assert 'data-lotc-component="list"' in rendered
    assert rendered.count('data-lotc-component="list-item"') == 2
    assert "Eerste regel van een alinea." in rendered, "wrapped lines are one paragraph"


def test_bold_becomes_strong() -> None:
    rendered = _render("Zet **DATABASE_DB** in je omgeving.")

    assert 'data-lotc-component="b"' in rendered
    assert "DATABASE_DB" in rendered


def test_a_backslash_keeps_a_literal_asterisk_next_to_the_bold_markers() -> None:
    """`**DATABASE_\\***` is the one line in the catalog where the two collide."""
    rendered = _render(r"De **DATABASE_\***-variabelen.")

    assert "DATABASE_*" in rendered
    assert 'data-lotc-component="b"' in rendered


def test_prose_can_never_become_a_template_expression() -> None:
    """The markup is compiled by Jinja, so a brace in the prose has to be inert.

    Doubled up as well: defusing the two-character opener with a replace can be nested
    around, and this is the check that the brace itself is what is escaped.
    """
    rendered = _render("Naam: {{ 7 * 6 }} en {% raw %} en {{{{ 7 * 6 }}}}")

    assert "42" not in rendered
    assert "{" not in rendered, "no brace reaches the template engine"
    assert "{{ 7 * 6 }}" in _visible(rendered), "and the reader still sees what was written"
    assert "{% raw %}" in _visible(rendered)


def test_a_placeholder_in_the_prose_survives_as_written() -> None:
    """Escaping braces must not spoil the naming patterns the prose explains."""
    rendered = _render("De volledige naam wordt {project}_{deployment}_{postfix}.")

    assert "{project}_{deployment}_{postfix}" in _visible(rendered)


# ---------------------------------------------------------------------------
# What the portal gets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", _SERVICES, ids=lambda s: s.value)
def test_the_popup_renders_without_leaving_a_component_behind(service: ServiceType) -> None:
    definition = ServiceAdapter.get_service_definition(service)
    assert definition.help_template is not None
    rendered = render_service_help(definition.help_template)

    assert rendered.strip()
    assert "<c-" not in rendered, "an unexpanded component means the markup is wrong"
    assert _icoonnamen(rendered), f"{definition.help_template} shows no icon at all"


@pytest.mark.parametrize(
    "service",
    [ServiceType.ALIASES, ServiceType.USER_ENV_VARS],
    ids=lambda s: s.value,
)
def test_the_two_value_services_say_that_they_carry_the_other_half(service: ServiceType) -> None:
    """A client reading `variables` of the other services sees only what the platform sets.

    What a project sets itself travels through these two, and an agent that does not know
    that will conclude a container holds half of what it holds. So these two explanations
    say it, rather than the API growing a note of its own about them.
    """
    markdown = service_help_markdown(service)

    assert "variables" in markdown
    assert "/values/component/" in markdown


def test_a_help_reference_that_resolves_to_nothing_raises() -> None:
    with pytest.raises(FileNotFoundError):
        render_service_help("does_not_exist/help.md")
