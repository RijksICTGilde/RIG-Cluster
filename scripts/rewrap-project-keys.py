"""Move config.age-private-key from the platform key to the project-KEK, in both clones.

    uv run --project operations-manager/python python scripts/rewrap-project-keys.py --cluster <cluster> \
        --projects <projects-clone>/projects --deployments <deployments-clone> --dry-run
    uv run --project operations-manager/python python scripts/rewrap-project-keys.py --cluster <cluster> \
        --projects <projects-clone>/projects --deployments <deployments-clone>

Place the KEK in the cluster first, with ``set-sops-key-secret.py --target project-kek``. This
writes and commits in the clones and pushes nothing.

The logic lives in ``project_rotation.py`` next to this file; see ``scripts/README.md``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_rotation import main_rewrap_project_keys  # type: ignore[reportMissingImports]

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_rewrap_project_keys()))
