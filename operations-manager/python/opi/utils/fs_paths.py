"""Padcomponenten van buiten: één insluitingstoets voor het bestandssysteem.

Een chartpad uit een projectbestand en een `file://`-dependency uit de `Chart.yaml` van
een geclonede repo worden allebei aan een werkmap geplakt. `os.path.join` negeert zijn
eerste argument zodra het tweede absoluut is, en een symlink in de clone kan buiten die
werkmap wijzen, dus elke samenstelling loopt via `safe_join` en elke kopie langs
`reject_escaping_symlinks`.
"""

from __future__ import annotations

import os
from pathlib import Path


def safe_join(root: str, *parts: str) -> str:
    """Plak `parts` achter `root` en geef het genormaliseerde pad terug.

    Raises:
        ValueError: Als het pad na normalisatie en symlink-resolutie buiten `root` ligt.
    """
    resolved_root = Path(root).resolve()
    candidate = Path(root).joinpath(*parts).resolve()

    if not candidate.is_relative_to(resolved_root):
        raise ValueError(f"Pad '{os.path.join(*parts)}' valt buiten '{root}'")

    return str(candidate)


def single_segment(value: object, field: str) -> str:
    """Toets dat `value` één padsegment is: geen scheidingsteken, geen '.' of '..'.

    Raises:
        ValueError: Als de waarde geen string is, leeg is, een scheidingsteken bevat,
            of met een punt begint.
    """
    if not isinstance(value, str):
        raise ValueError(f"Veld '{field}' moet een string zijn, niet {type(value).__name__}")  # noqa: TRY004
    if not value or "/" in value or "\\" in value or value.startswith("."):
        raise ValueError(f"Ongeldige waarde '{value}' voor '{field}': verwacht één padsegment")
    return value


def reject_escaping_symlinks(root: str) -> None:
    """Weiger een boom met een symlink die buiten `root` wijst.

    `.git` blijft buiten de toets: de kopielus slaat die map ook over.

    Raises:
        ValueError: Bij de eerste symlink onder `root` waarvan het doel erbuiten ligt.
    """
    resolved_root = Path(root).resolve()

    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in dirnames + filenames:
            entry = Path(dirpath, name)
            if entry.is_symlink() and not entry.resolve().is_relative_to(resolved_root):
                raise ValueError(f"Symlink '{entry}' wijst buiten '{root}'")
