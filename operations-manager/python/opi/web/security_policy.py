"""``SECURITY.md`` uit de root van de repo als HTML, voor de pagina ``/security``.

Eén bron: het bestand dat GitHub toont is het bestand dat de pagina toont. Het wordt bij
de eerste aanvraag een keer omgezet en daarna uit het geheugen geserveerd.
"""

import re
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from markdown_it import MarkdownIt

if TYPE_CHECKING:
    from markdown_it.token import Token

#: Waar de relatieve links in ``SECURITY.md`` naar wijzen: paden in de repo, die in de
#: app een 404 zouden geven.
REPO_BLOB_URL = "https://github.com/RijksICTGilde/RIG-Cluster/blob/main/"

_OPI_DIR = Path(__file__).resolve().parent.parent

#: In de image staat het bestand naast ``opi/`` (de Dockerfile kopieert het naar
#: ``/app``); in een checkout staat het twee mappen hoger, in de root van de repo.
_CANDIDATES = (
    _OPI_DIR.parent / "SECURITY.md",
    _OPI_DIR.parent.parent.parent / "SECURITY.md",
)

_ABSOLUTE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|#|/)", re.IGNORECASE)


def _slug(text: str) -> str:
    """Het anker dat GitHub voor een kop maakt, zodat ``#de-toeleverketen-...`` hier ook werkt."""
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


def _rewrite(tokens: list[Token]) -> None:
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            token.attrSet("id", _slug(tokens[index + 1].content))
        elif token.type == "inline" and token.children:
            for child in token.children:
                href = child.attrGet("href") if child.type == "link_open" else None
                if isinstance(href, str) and not _ABSOLUTE.match(href):
                    child.attrSet("href", REPO_BLOB_URL + href)


def render_security_policy(markdown: str) -> str:
    """Zet de markdown om naar HTML, zonder ruwe HTML uit de bron door te laten."""
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    tokens = md.parse(markdown)
    _rewrite(tokens)
    return md.renderer.render(tokens, md.options, {})


@cache
def security_policy_html() -> str:
    """``SECURITY.md`` als HTML.

    Raises:
        FileNotFoundError: Als het bestand op geen van beide plekken staat.
    """
    for path in _CANDIDATES:
        if path.is_file():
            return render_security_policy(path.read_text(encoding="utf-8"))
    raise FileNotFoundError("SECURITY.md staat niet naast opi/ en niet in de root van de repo")
