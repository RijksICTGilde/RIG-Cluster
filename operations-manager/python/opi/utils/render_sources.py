"""Which git sources the render chain may clone.

A helmfile or chart source OPI clones lands in the deployment directory in full and is
rendered there by the CMP. It is therefore code running on the platform, and
``RENDER_SOURCE_ALLOWLIST`` says which sources may do that. Empty means none at all.
"""

import os
import posixpath
import re
from urllib.parse import urlparse

from opi.core.config import settings

#: ``git@host:path`` carries no scheme, but is an ssh URL.
_SSH_SHORTHAND = re.compile(r"^[^/@:]+@(?P<host>[^/:]+):(?P<path>.+)$")


def normalize_git_url(url: str) -> str:
    """Scheme, host and path, without userinfo and without ``.git`` or a trailing slash.

    Userinfo is dropped because an allowlist is about the source and not about who fetches
    it: ``https://attacker@host/path`` is the same source as ``https://host/path``.

    The path is resolved, because git asks for ``/allowed/../../elsewhere`` as
    ``/elsewhere``: without that, any source slips past the allowlist.
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
    """The allowlist from the configuration, as a list."""
    raw = settings.RENDER_SOURCE_ALLOWLIST or ""
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def is_allowed_render_source(url: str) -> bool:
    target = normalize_git_url(url)
    for entry in allowed_render_sources():
        allowed = normalize_git_url(entry)
        # Equal, or below it: a prefix without a path boundary would also admit
        # ``.../my-team-infra-of-somebody-else``.
        if target == allowed or target.startswith(f"{allowed}/"):
            return True
    return False


def require_allowed_render_source(url: str, what: str) -> None:
    """Refuse a source that is not on the allowlist.

    The message names the normalised URL: ``helmfile[].url`` has no separate credentials, so
    a private source carries a token in its userinfo, and the message reaches the user and
    the logs.
    """
    if is_allowed_render_source(url):
        return
    raise ValueError(
        f"De bron '{normalize_git_url(url)}' van {what} staat niet op RENDER_SOURCE_ALLOWLIST en wordt "
        f"niet gekloond. Toegestaan: {allowed_render_sources() or 'niets'}."
    )


def path_inside_clone(root: str, relative: str, what: str) -> str:
    """Resolve a path from the project file inside a cloned source, and refuse one outside it.

    ``chart-path``, ``helmfile[].path`` and ``helmfile[].entry`` come from the project file
    and are glued onto the clone. Without this check an absolute path, a ``..`` or a symlink
    in the source copies container content into the deployment directory, where the CMP
    renders it.
    """
    base = os.path.realpath(root)
    resolved = os.path.realpath(os.path.join(base, relative)) if relative else base
    if resolved != base and not resolved.startswith(f"{base}{os.sep}"):
        raise ValueError(f"Het pad '{relative}' van {what} wijst buiten de gekloonde bron en wordt niet gebruikt.")
    return resolved
