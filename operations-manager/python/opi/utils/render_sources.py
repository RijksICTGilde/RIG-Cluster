"""Welke git-bronnen de renderketen mag klonen.

Een helmfile- of chartbron die OPI kloont, komt integraal in de deploymentmap terecht en
wordt daar door de CMP gerenderd. Het is dus code die op het platform draait, en
``RENDER_SOURCE_ALLOWLIST`` zegt van welke bronnen dat mag. Leeg betekent: geen enkele.
"""

import os
import posixpath
import re
from urllib.parse import urlparse

from opi.core.config import settings

#: ``git@host:pad`` heeft geen schema, maar is een ssh-URL.
_SSH_SHORTHAND = re.compile(r"^[^/@:]+@(?P<host>[^/:]+):(?P<path>.+)$")


def normalize_git_url(url: str) -> str:
    """Schema, host en pad, zonder userinfo en zonder ``.git`` of afsluitende slash.

    Userinfo eruit omdat een allowlist over de bron gaat en niet over wie hem ophaalt:
    ``https://aanvaller@host/pad`` is dezelfde bron als ``https://host/pad``.

    Het pad wordt opgelost, want git vraagt ``/toegestaan/../../elders`` op als
    ``/elders``: zonder dat glipt elke bron langs de allowlist.
    """
    url = url.strip()
    shorthand = _SSH_SHORTHAND.match(url)
    if shorthand:
        url = f"ssh://{shorthand['host']}/{shorthand['path'].lstrip('/')}"

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = posixpath.normpath(parsed.path) if parsed.path else ""
    if path == ".":
        path = ""
    path = path.rstrip("/").removesuffix(".git").rstrip("/")
    return f"{parsed.scheme}://{host}{path}"


def allowed_render_sources() -> list[str]:
    """De allowlist uit de configuratie, als lijst."""
    raw = settings.RENDER_SOURCE_ALLOWLIST or ""
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def is_allowed_render_source(url: str) -> bool:
    target = normalize_git_url(url)
    for entry in allowed_render_sources():
        allowed = normalize_git_url(entry)
        # Gelijk, of eronder: een prefix zonder padgrens zou
        # ``.../mijn-bureau-infra-van-een-ander`` ook toelaten.
        if target == allowed or target.startswith(f"{allowed}/"):
            return True
    return False


def require_allowed_render_source(url: str, what: str) -> None:
    """Weiger een bron die niet op de allowlist staat.

    De melding noemt de genormaliseerde URL: ``helmfile[].url`` kent geen aparte
    credentials, dus een private bron heeft een token in de userinfo, en de melding
    bereikt de gebruiker en de logs.
    """
    if is_allowed_render_source(url):
        return
    raise ValueError(
        f"De bron '{normalize_git_url(url)}' van {what} staat niet op RENDER_SOURCE_ALLOWLIST en wordt "
        f"niet gekloond. Toegestaan: {allowed_render_sources() or 'niets'}."
    )


def path_inside_clone(root: str, relative: str, what: str) -> str:
    """Los een pad uit het projectbestand op binnen een gekloonde bron, en weiger eruit.

    ``chart-path``, ``helmfile[].path`` en ``helmfile[].entry`` komen uit het
    projectbestand en worden aan de clone geplakt. Zonder deze controle kopieert een
    absoluut pad, een ``..`` of een symlink in de bron inhoud van de container naar de
    deploymentmap, waar de CMP hem rendert.
    """
    base = os.path.realpath(root)
    resolved = os.path.realpath(os.path.join(base, relative)) if relative else base
    if resolved != base and not resolved.startswith(f"{base}{os.sep}"):
        raise ValueError(f"Het pad '{relative}' van {what} wijst buiten de gekloonde bron en wordt niet gebruikt.")
    return resolved
