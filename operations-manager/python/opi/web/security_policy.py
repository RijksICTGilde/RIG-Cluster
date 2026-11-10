"""``SECURITY.md`` from the root of the repo as HTML, for the ``/security`` page.

One source: the file GitHub shows is the file the page shows. It is converted once, on the
first request, and served from memory after that.
"""

import re
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from markdown_it import MarkdownIt

if TYPE_CHECKING:
    from markdown_it.token import Token

#: Where the relative links in ``SECURITY.md`` point: paths in the repo, which would be a 404
#: in the app.
REPO_BLOB_URL = "https://github.com/RijksICTGilde/RIG-Cluster/blob/main/"

_OPI_DIR = Path(__file__).resolve().parent.parent

#: In the image the file sits next to ``opi/`` (the Dockerfile copies it into ``/app``); in a
#: checkout it sits two directories up, in the root of the repo.
_CANDIDATES = (
    _OPI_DIR.parent / "SECURITY.md",
    _OPI_DIR.parent.parent.parent / "SECURITY.md",
)

_ABSOLUTE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|#|/)", re.IGNORECASE)


def _slug(text: str) -> str:
    """The anchor GitHub makes for a heading, so a link to one works here as well."""
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
    """Convert the markdown to HTML, letting no raw HTML from the source through."""
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    tokens = md.parse(markdown)
    _rewrite(tokens)
    return md.renderer.render(tokens, md.options, {})


@cache
def security_policy_html() -> str:
    """``SECURITY.md`` as HTML.

    Raises:
        FileNotFoundError: when the file sits in neither place.
    """
    for path in _CANDIDATES:
        if path.is_file():
            return render_security_policy(path.read_text(encoding="utf-8"))
    raise FileNotFoundError("SECURITY.md staat niet naast opi/ en niet in de root van de repo")
