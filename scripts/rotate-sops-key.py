#!/usr/bin/env python3
"""Replace the platform AGE key in this repo: the SOPS files and the loose values.

The logic lives in ``sops_rotation.py`` next to this file; see ``scripts/README.md``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sops_rotation import main  # type: ignore[reportMissingImports]

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
