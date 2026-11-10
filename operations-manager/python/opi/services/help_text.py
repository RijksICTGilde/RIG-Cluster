"""A service's long-form explanation, written once and read by both a human and a client.

Every service carries a prose explanation of what it is and when you would use it, as
markdown, and that is the single source. The portal renders it (this module turns it into
the components the popup is built from) and ``GET /api/v2/services/{name}`` returns it
as-is. Two renderings, one file.

**The supported markdown is deliberately small**, exactly the shapes the explanations use: a
title, sections, paragraphs, bullets and bold. Anything richer would need a component that
does not exist in the design system, and a syntax that renders as literal text in one of the
two readers is worse than a syntax that does not exist.

    # Title             -> <c-heading type="h2"> with the service's own icon
    ## Section          -> <c-heading type="h3">
    A paragraph.        -> <c-paragraph>
    - a bullet          -> <c-rich-text><ul><li>
    **bold**            -> <c-b>
    [label](/pad)       -> <c-link>

The icon is not in the markdown. It is on the service definition, where it already is for
the card and the picker, so the popup and the card cannot show different icons.
"""

from __future__ import annotations

import pathlib
import re
from typing import TYPE_CHECKING

import opi
from opi.http.render.navigation_lotc import to_nldd_icon
from opi.services.services import ServiceAdapter

if TYPE_CHECKING:
    from opi.services.services_enums import ServiceType

#: Where a ``help_template`` may resolve: a service package or the shared templates tree.
#: The same three roots the Jinja loader searches.
_ROOTS = (
    pathlib.Path(opi.__file__).parent / "services" / "catalog",
    pathlib.Path(opi.__file__).parent / "templates_lotc",
    pathlib.Path(opi.__file__).parent / "templates_lotc" / "help",
)

_BOLD = re.compile(r"\*\*(.+?)\*\*")
#: An inline link. ONLY an internal path or an https address: the same markdown goes to API
#: clients unchanged, and a ``javascript:`` or ``data:`` href in a service explanation has no
#: honest use. What does not match stays plain text.
_LINK = re.compile(r"\[([^\]\n]+)\]\((/[^)\s]*|https://[^)\s]+)\)")
#: A backslash escape, so a literal ``*`` can sit next to the emphasis markers.
_ESCAPE = re.compile(r"\\(.)")


def is_markdown_help(help_template: str) -> bool:
    """Whether this help reference names a markdown file rather than a Jinja template."""
    return help_template.endswith(".md")


def help_file(help_template: str) -> pathlib.Path | None:
    """The file a ``help_template`` resolves to, or None when it resolves to nothing."""
    for root in _ROOTS:
        candidate = root / help_template
        if candidate.is_file():
            return candidate
    return None


def read_help_markdown(help_template: str) -> str:
    """The markdown behind a ``help_template``.

    Raises ``FileNotFoundError`` rather than returning an empty string: a service whose
    explanation has gone missing must not silently describe itself as nothing.
    """
    path = help_file(help_template)
    if path is None:
        msg = f"Help template '{help_template}' does not exist"
        raise FileNotFoundError(msg)
    return path.read_text(encoding="utf-8")


def service_help_markdown(service_type: ServiceType) -> str:
    """The explanation of one service, as markdown."""
    definition = ServiceAdapter.get_service_definition(service_type)
    if not definition.help_template:
        return ""
    return read_help_markdown(definition.help_template)


def service_guide_markdown(service_type: ServiceType) -> str:
    """The application-oriented guide of one service, as markdown; empty when it has none.

    Same contract as :func:`service_help_markdown`, for the ``guide_template`` a service
    may declare next to its ``help_template``: one file, read by the API (the ``guide``
    field of ``GET /api/v2/services/{name}``) and rendered by the portal through the same
    help route.
    """
    definition = ServiceAdapter.get_service_definition(service_type)
    if not definition.guide_template:
        return ""
    return read_help_markdown(definition.guide_template)


def _text(raw: str) -> str:
    """Prose as it may be handed to the template engine.

    Every ``{`` becomes an entity. The markup this module produces is compiled by the
    Jinja environment (that is how the ``c-`` components are expanded), so a ``{{`` or a
    ``{%`` in the prose would be evaluated as a template expression. Escaping the brace
    itself rather than the two-character openers leaves nothing to nest: there is no
    sequence of removed pairs that reconstructs one, because no ``{`` survives at all.
    Prose that means to show ``{project}_{deployment}`` still renders as written.
    """
    return raw.replace("{", "&#123;")


def _inline(raw: str) -> str:
    """One line of prose: bold becomes ``c-b``, the rest is text.

    A backslash escapes the character after it, as it does in markdown. It earns its
    place on the one line that ends a bold run with a literal asterisk
    (``**DATABASE_\\***``): without it the emphasis markers and the wildcard are the same
    character and the wildcard silently disappears.
    """
    protected: dict[str, str] = {}
    links: dict[str, str] = {}

    def hide(match: re.Match[str]) -> str:
        token = f"\x00{len(protected)}\x00"
        protected[token] = match.group(1)
        return token

    def hide_link(match: re.Match[str]) -> str:
        # The label as PLAIN text: it goes into an attribute, and markup in an attribute
        # value does not survive the component layer.
        token = f"\x01{len(links)}\x01"
        links[token] = f'<c-link href="{_text(match.group(2))}" label="{_text(match.group(1))}" />'
        return token

    rendered = _BOLD.sub(
        lambda match: f"<c-b>{_text(match.group(1))}</c-b>",
        _LINK.sub(hide_link, _text(_ESCAPE.sub(hide, raw))),
    )
    for token, markup in links.items():
        rendered = rendered.replace(token, markup)
    for token, character in protected.items():
        rendered = rendered.replace(token, _text(character))
    return rendered


def markdown_to_components(source: str, *, icon: str | None = None, color: str | None = None) -> str:
    """Turn a help markdown document into the component markup the portal renders.

    ``icon`` and ``color`` decorate the title, and come from the service definition; a
    document rendered without them simply gets a title with no icon.
    """
    out: list[str] = []
    bullets: list[str] = []
    paragraph: list[str] = []
    title_done = False

    def flush() -> None:
        nonlocal bullets, paragraph
        if bullets:
            # A <ul> inside a <c-rich-text> and NOT <c-list>: that last one is NLDD's
            # interactive row list, which draws separators and no bullets. And rich-text and
            # not <c-paragraph>: that wraps its contents in a <p>, and a list inside a
            # paragraph is not valid HTML.
            items = "".join(f"<li>{item}</li>" for item in bullets)
            out.append(f"<c-rich-text><ul>{items}</ul></c-rich-text>")
            bullets = []
        if paragraph:
            out.append(f"<c-paragraph>{' '.join(paragraph)}</c-paragraph>")
            paragraph = []

    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith("## "):
            flush()
            out.append(f'<c-heading type="h3">{_inline(stripped[3:].strip())}</c-heading>')
        elif stripped.startswith("# "):
            flush()
            title = _inline(stripped[2:].strip())
            # Only the first title carries the icon: a second one would be a second
            # service block on one page.
            if icon and not title_done:
                # Through the translation: the service definitions carry our own icon names
                # and the design system has its own vocabulary. A template has the nldd_icon
                # filter for this; here the markup is built directly, and without this line
                # every explanation gets an EMPTY icon above its title, quietly, because an
                # unknown name raises nothing.
                #
                # The icon sits NEXT TO the heading and not inside it: an xl icon inside a
                # <c-heading> falls over the text, because a heading does not lay its
                # children out side by side. A cluster does.
                out.append(
                    f'<c-cluster gap="sm" align="center">'
                    f'<c-icon icon="{to_nldd_icon(icon)}" size="xl" color="{color or ""}"/>'
                    f'<c-heading type="h2">{title}</c-heading>'
                    f"</c-cluster>"
                )
                title_done = True
                continue
            title_done = True
            out.append(f'<c-heading type="h2">{title}</c-heading>')
        elif stripped.startswith("- "):
            if paragraph:
                flush()
            bullets.append(_inline(stripped[2:].strip()))
        else:
            if bullets and line[:1] in (" ", "\t"):
                # A wrapped bullet item: the indented continuation line belongs to the
                # bullet above, it does not open a paragraph of its own. Without this the
                # list flushed mid-sentence and the rest of the line stood as its own
                # paragraph under the list.
                bullets[-1] += " " + _inline(stripped)
                continue
            if bullets:
                flush()
            paragraph.append(_inline(stripped))
    flush()
    # IN A STACK, because a gap exists only where a stack is the PARENT (see the head of
    # bg/_patterns.html.j2). The popup puts this markup in a bare <div>, so without the stack
    # the headings sit tight against the paragraphs above and below them.
    content = "\n".join(out)
    return f'<c-stack gap="md">\n{content}\n</c-stack>'


def render_service_help(help_template: str) -> str:
    """The rendered HTML of one help document, with the icon of the service that owns it.

    The owner is looked up by ``help_template`` rather than passed in: the caller is a
    route that knows only which help was asked for, and a help document that belongs to no
    service (the container-image note) simply renders without an icon.
    """
    owner = next(
        (
            (service_type, d)
            for service_type, d in ServiceAdapter.SERVICE_DEFINITIONS.items()
            if help_template in (d.help_template, d.guide_template)
        ),
        None,
    )
    definition = owner[1] if owner else None
    # The imports sit inside to avoid a cycle: the template environment leans on the service
    # registry, which brings in the service packages that read this module.
    from opi.http.render.templates_lotc import templates_lotc
    from opi.services.registry import get_service

    markup = markdown_to_components(
        read_help_markdown(help_template),
        icon=definition.icon if definition else None,
        color=definition.color if definition else None,
    )
    # The approval warning comes from the service's DECLARATION and not from its explanation,
    # so it stands on every service that requires approval and the text cannot claim what the
    # service does not deliver. It is about ENABLING, so it asks exactly that: a decision over
    # a config value (redis without a key prefix) only applies at that value. Only on the
    # POPUP explanation: a guide describes per scenario what is a request and what is not, and
    # the generic banner would claim more broadly there than is true.
    if owner and owner[1].help_template == help_template and get_service(owner[0]).enabling_needs_approval():
        warning = (
            '<c-alert type="warning" heading="Vereist goedkeuring">'
            "Het aanzetten van deze dienst is een aanvraag: een beheerder moet hem "
            "goedkeuren voordat er iets gebeurt.</c-alert>"
        )
        markup = markup.replace('<c-stack gap="md">\n', f'<c-stack gap="md">\n{warning}\n', 1)
    return templates_lotc.env.from_string(markup).render()
