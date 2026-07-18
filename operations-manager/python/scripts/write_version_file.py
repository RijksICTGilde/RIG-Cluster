#!/usr/bin/env python3
"""Write opi/version.json from git for the /version endpoint and the footer.

Run via ``task version:generate`` (once) or ``task version:watch`` (continuously).
During skaffold dev the file is hot-synced into the pod, so /version reflects the
running code: commit, branch and whether the working tree is dirty.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def _git(*args: str) -> str:
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, check=True)  # noqa: S603, S607
        return result.stdout.strip()
    except subprocess.CalledProcessError, FileNotFoundError:
        return ""


def main() -> None:
    commit = _git("rev-parse", "HEAD")
    short = _git("rev-parse", "--short", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(_git("status", "--porcelain"))

    info = {
        "version": short or "0.1.0",
        "commit": commit,
        "branch": branch,
        "build_date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dirty": dirty,
    }

    out = Path(__file__).resolve().parent.parent / "opi" / "version.json"
    out.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out} -> {info['version']}{'*' if dirty else ''} ({branch or 'no-branch'})")


if __name__ == "__main__":
    main()
