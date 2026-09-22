#!/usr/bin/env python3
"""Put the new key in the cluster's sops-age-key secrets and restart what has to reread it.

The logic lives in ``sops_key_secret.py`` next to this file. This entry point only exists so the tool
can be called by the name the plan uses; a hyphen in a filename is not importable, and the
tests need to reach the logic directly.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sops_key_secret import main  # type: ignore[reportMissingImports]

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
