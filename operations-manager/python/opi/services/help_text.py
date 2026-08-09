"""A service's long-form explanation, written once and read by both a human and a client.

Every service carries a prose explanation of what it is and when you would use it. It was
written for one reader: a popup in the portal, in ROOS component markup
(``<c-heading>``, ``<c-p>``, ``<c-ul>``). That made it unusable for the other reader RC-59
is about -- a client or an agent asking the API what a service does gets markup full of
``utrecht-*`` classes and sentences that point at buttons.

The prose is now markdown, and that is the single source. The portal renders it (this
module turns it into the same ROOS components it always used, so the popup looks the same)
and ``GET /api/v2/services/{name}`` returns it as-is. Two renderings, one file: the
alternative -- a ``help.md`` next to a ``help.html.j2`` -- is drift with extra steps.

**The supported markdown is deliberately small**, exactly the shapes the 21 explanations
use: a title, sections, paragraphs, bullets and bold. Anything richer would need a
component that does not exist in the design system, and a syntax that renders as literal
text in one of the two readers is worse than a syntax that does not exist.

    # Title             -> <c-heading type="h2"> with the service's own icon
    ## Section          -> <c-heading type="h3">
    A paragraph.        -> <c-p>
    - a bullet          -> <c-ul><c-li>
    **bold**            -> <c-strong>

The icon is not in the markdown. It is on the service definition, where it already is for
the card and the picker, so the popup and the card cannot show different icons.
"""

from __future__ import annotations

import pathlib
import re
from typing import TYPE_CHECKING

import opi
from opi.services.services import ServiceAdapter

if TYPE_CHECKING:
    from opi.services.services_enums import ServiceType

#: Where a ``help_template`` may resolve: a service package (RC-36) or the shared
#: templates tree. The same three roots the Jinja loader searches.
_ROOTS = (
    pathlib.Path(opi.__file__).parent / "services" / "catalog",
    pathlib.Path(opi.__file__).parent / "templates",
    pathlib.Path(opi.__file__).parent / "templates" / "help",
)

_BOLD = re.compile(r"\*\*(.+?)\*\*")
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
    """One line of prose: bold becomes ``c-strong``, the rest is text.

    A backslash escapes the character after it, as it does in markdown. It earns its
    place on the one line that ends a bold run with a literal asterisk
    (``**DATABASE_\\***``): without it the emphasis markers and the wildcard are the same
    character and the wildcard silently disappears.
    """
    protected: dict[str, str] = {}

    def hide(match: re.Match[str]) -> str:
        token = f"\x00{len(protected)}\x00"
        protected[token] = match.group(1)
        return token

    rendered = _BOLD.sub(
        lambda match: f"<c-strong>{_text(match.group(1))}</c-strong>",
        _text(_ESCAPE.sub(hide, raw)),
    )
    for token, character in protected.items():
        rendered = rendered.replace(token, _text(character))
    return rendered


def markdown_to_roos(source: str, *, icon: str | None = None, color: str | None = None) -> str:
    """Turn a help markdown document into the ROOS component markup the portal renders.

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
            items = "".join(f"<c-li>{item}</c-li>" for item in bullets)
            out.append(f"<c-ul>{items}</c-ul>")
            bullets = []
        if paragraph:
            out.append(f"<c-p>{' '.join(paragraph)}</c-p>")
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
                title = f'<c-icon icon="{icon}" size="xl" color="{color or ""}"/>{title}'
            title_done = True
            out.append(f'<c-heading type="h2">{title}</c-heading>')
        elif stripped.startswith("- "):
            if paragraph:
                flush()
            bullets.append(_inline(stripped[2:].strip()))
        else:
            if bullets:
                flush()
            paragraph.append(_inline(stripped))
    flush()
    return "\n".join(out)


def render_service_help(help_template: str) -> str:
    """The rendered HTML of one help document, with the icon of the service that owns it.

    The owner is looked up by ``help_template`` rather than passed in: the caller is a
    route that knows only which help was asked for, and a help document that belongs to no
    service (the container-image note) simply renders without an icon.
    """
    from opi.core.templates import get_templates

    definition = next(
        (d for d in ServiceAdapter.SERVICE_DEFINITIONS.values() if d.help_template == help_template),
        None,
    )
    markup = markdown_to_roos(
        read_help_markdown(help_template),
        icon=definition.icon if definition else None,
        color=definition.color if definition else None,
    )
    return get_templates().env.from_string(markup).render()
