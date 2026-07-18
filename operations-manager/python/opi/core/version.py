"""Application version info for ZAD/OPI.

Resolves the running build's version metadata. Priority (highest first):

1. A generated ``opi/version.json`` - written from git by ``task version:generate``
   and hot-synced into the pod during skaffold dev, so ``/version`` tracks the
   running code (commit, branch, dirty flag).
2. Environment variables baked at image build time (``ZAD_VERSION`` /
   ``ZAD_GIT_COMMIT`` / ``ZAD_GIT_BRANCH`` / ``ZAD_BUILD_DATE``) - used for CI/prod
   immutable images.
3. Static defaults.

Intentionally NOT cached, so a live-synced ``version.json`` is reflected without a
process restart.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

APP_NAME = "ZAD"

# opi/version.json lives next to the opi package (this module is opi/core/version.py).
_VERSION_FILE = Path(__file__).resolve().parent.parent / "version.json"

_FILE_FIELDS = ("version", "commit", "branch", "build_date", "dirty")


def get_version_info() -> dict[str, object]:
    """Return version metadata: name, version, commit, branch, build_date, dirty."""
    info: dict[str, object] = {
        "name": APP_NAME,
        "version": os.environ.get("ZAD_VERSION", "0.1.0"),
        "commit": os.environ.get("ZAD_GIT_COMMIT", ""),
        "branch": os.environ.get("ZAD_GIT_BRANCH", ""),
        "build_date": os.environ.get("ZAD_BUILD_DATE", ""),
        "dirty": False,
    }
    try:
        if _VERSION_FILE.is_file():
            data = json.loads(_VERSION_FILE.read_text(encoding="utf-8"))
            for key in _FILE_FIELDS:
                value = data.get(key)
                if value not in (None, ""):
                    info[key] = value
    except (OSError, ValueError) as exc:
        logger.debug("Could not read version file %s: %s", _VERSION_FILE, exc)
    return info
