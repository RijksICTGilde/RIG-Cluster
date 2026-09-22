#!/usr/bin/env python3
"""Move the project files to the new platform key: two fields per file, one commit per project.

    scripts/rotate-project-keys.py --projects <clone>/projects --dry-run
    scripts/rotate-project-keys.py --projects <clone>/projects

Point it at a LOCAL CLONE of the projects repo. It writes and commits there and pushes nothing:
the cutover verifies while nothing has been pushed yet, and then the operator pushes the clone.

The logic lives in ``project_rotation.py`` next to this file; see ``scripts/README.md``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_rotation import main_rotate_keys  # type: ignore[reportMissingImports]

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_rotate_keys()))
