"""The composite value an invitation stores as its destination, and how to split it.

    component:deployment           -- the component's only public address
    component:deployment:/path     -- one of several paths that component publishes

``:`` is safe as the separator. Deployment and component names are DNS-1123 labels and so
cannot contain a colon themselves, and the path sits LAST: split from the left with a
maximum of two and a path that does contain a colon survives intact. Splitting on ``/``
would not survive, because a path is made of slashes.

Nothing else lives here: no project data, no derivation, no imports. Both the config model
(which rejects a value that names no component and no deployment) and the resolver (which
turns it into an address) need this, and neither should have to import the other to get it.
"""

from __future__ import annotations

import re

SEPARATOR = ":"

#: The shape a component and a deployment name have in the project schema
#: (``project_v2.json``: ``^[a-z]([-a-z0-9]*[a-z0-9])?`` with a 63-character ceiling).
#:
#: It is repeated here as a GUARD, not as a second definition of the rule. Without it,
#: ``https://ergens.example.nl/`` splits into ``("https", "//ergens.example.nl/")`` -- a
#: URL contains colons too, so the split alone happily reports a valid-looking choice for a
#: value that names no component at all. Since the safety of ``:`` as the separator rests on
#: exactly this shape, checking the shape is what makes that argument hold.
_NAME = re.compile(r"[a-z]([-a-z0-9]*[a-z0-9])?")
_NAME_MAX = 63


def _is_name(value: str) -> bool:
    return len(value) <= _NAME_MAX and _NAME.fullmatch(value) is not None


def split_target(value: str | None) -> tuple[str, str, str | None]:
    """``component:deployment[:path]`` -> ``(component, deployment, path or None)``.

    Anything that does not split into two names comes back as empty strings, which is what
    every caller treats as "this names no destination". Raising would push the same
    judgement onto four call sites, and one of them renders a public page.
    """
    if not value or not isinstance(value, str):
        return "", "", None
    parts = value.split(SEPARATOR, 2)
    if len(parts) < 2:
        return "", "", None
    component, deployment = parts[0].strip(), parts[1].strip()
    if not _is_name(component) or not _is_name(deployment):
        return "", "", None
    path = parts[2].strip() if len(parts) == 3 else None
    return component, deployment, path or None


def join_target(component: str, deployment: str, path: str | None = None) -> str:
    """``(component, deployment, path)`` -> the stored value.

    The path is left out where there is none, so a component with a single address stores
    ``component:deployment`` and not a trailing separator that means nothing.
    """
    basis = f"{component}{SEPARATOR}{deployment}"
    return f"{basis}{SEPARATOR}{path}" if path else basis
