#!/usr/bin/env python3
"""Replace the GitHub PAT in every project file -- the same loop, with a different entry.

    scripts/replace-git-pat.py --projects <clone>/projects --dry-run
    scripts/replace-git-pat.py --projects <clone>/projects

Hard precondition: the new PAT must already be valid on GitHub before the first file is written,
with the old one still valid too. Otherwise a project loses its repository access the moment its
file is converted while the rest is not.

The logic lives in ``project_rotation.py`` next to this file, which holds the round both entry
points share. This file only exists so the tool can be called by the name the plan uses; a hyphen
in a filename is not importable, and the tests need to reach the logic directly.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_rotation import main_replace_pat  # type: ignore[reportMissingImports]

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_replace_pat()))
