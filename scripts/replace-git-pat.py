"""Replace the GitHub PAT in every project file -- the same loop, with a different entry.

    uv run --project operations-manager/python python scripts/replace-git-pat.py --projects <clone>/projects --dry-run
    uv run --project operations-manager/python python scripts/replace-git-pat.py --projects <clone>/projects

It takes TWO tokens, from two files in ``security/`` answered the way the key files are:
``--pat-current-file`` (default ``security/pat_current.txt``) is the token being replaced and
``--pat-new-file`` (default ``security/pat_new.txt``) the one that takes its place. Only a
field whose plaintext IS the current token is replaced; anything else keeps its value, is
re-encrypted where it still sits on the old key, and is named in the report with its path.

Hard precondition: the new PAT must already be valid on GitHub before the first file is written,
with the old one still valid too. Otherwise a project loses its repository access the moment its
file is converted while the rest is not.

The logic lives in ``project_rotation.py`` next to this file; see ``scripts/README.md``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_rotation import main_replace_pat  # type: ignore[reportMissingImports]

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_replace_pat()))
