"""Put the new key in the cluster's sops-age-key secrets and restart what has to reread it.

The logic lives in ``sops_key_secret.py`` next to this file; see ``scripts/README.md``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sops_key_secret import main  # type: ignore[reportMissingImports]

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
