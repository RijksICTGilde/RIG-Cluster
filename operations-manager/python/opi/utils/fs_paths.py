"""Path components from outside: one containment check for the file system.

A chart path from a project file and a `file://` dependency from the `Chart.yaml` of a
cloned repo are both glued onto a working directory. `os.path.join` ignores its first
argument the moment the second is absolute, and a symlink in the clone can point outside
that working directory, so every join goes through `safe_join` and every copy past
`reject_escaping_symlinks`.
"""

from __future__ import annotations

import os
from pathlib import Path


def safe_join(root: str, *parts: str) -> str:
    """Join `parts` onto `root` and return the normalised path.

    Raises:
        ValueError: when the path lies outside `root` after normalisation and symlink
            resolution.
    """
    resolved_root = Path(root).resolve()
    candidate = Path(root).joinpath(*parts).resolve()

    if not candidate.is_relative_to(resolved_root):
        raise ValueError(f"Pad '{os.path.join(*parts)}' valt buiten '{root}'")

    return str(candidate)


def single_segment(value: object, field: str) -> str:
    """Check that `value` is a single path segment: no separator, no '.' or '..'.

    Raises:
        ValueError: when the value is not a string, is empty, holds a separator, or
            starts with a dot.
    """
    if not isinstance(value, str):
        raise ValueError(f"Veld '{field}' moet een string zijn, niet {type(value).__name__}")  # noqa: TRY004  # the caller handles one error type, so a type mismatch raises ValueError too
    if not value or "/" in value or "\\" in value or value.startswith("."):
        raise ValueError(f"Ongeldige waarde '{value}' voor '{field}': verwacht één padsegment")
    return value


def reject_escaping_symlinks(root: str) -> None:
    """Refuse a tree holding a symlink that points outside `root`.

    `.git` stays out of the check: the copy loop skips that directory as well.

    Raises:
        ValueError: on the first symlink under `root` whose target lies outside it.
    """
    resolved_root = Path(root).resolve()

    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in dirnames + filenames:
            entry = Path(dirpath, name)
            if entry.is_symlink() and not entry.resolve().is_relative_to(resolved_root):
                raise ValueError(f"Symlink '{entry}' wijst buiten '{root}'")
