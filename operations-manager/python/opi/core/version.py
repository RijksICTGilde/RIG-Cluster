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

Next to what was built, the answer says *who is answering*: ``pod`` and ``image``.
During a rolling update two pods serve the same Service, so two consecutive calls
can legitimately report two different commits. With only a commit in the answer
that reads as drift; with the pod name in it, it reads as "a rollout is running,
wait". The image reference is the one thing that is true by construction - it is
what the kubelet started - while the commit is derived from the build and can be
wrong if the build went wrong.
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

# The image this pod runs, resolved once from the cluster (see set_running_image).
# Empty outside Kubernetes, and empty when the lookup was not allowed or not possible;
# an empty field is honest, a guessed one is not.
_running_image: str = ""


def set_running_image(image: str) -> None:
    """Record the container image this pod runs, as the cluster reports it."""
    global _running_image
    _running_image = image or ""
    logger.info("Running image resolved as: %s", _running_image or "<unknown>")


def get_running_image() -> str:
    """The container image this pod runs, or the build-time fallback, or empty."""
    return _running_image or os.environ.get("ZAD_IMAGE", "")


def get_version_info() -> dict[str, object]:
    """Return version metadata: name, version, commit, branch, build_date, dirty, pod, image."""
    info: dict[str, object] = {
        "name": APP_NAME,
        "version": os.environ.get("ZAD_VERSION", "0.1.0"),
        "commit": os.environ.get("ZAD_GIT_COMMIT", ""),
        "branch": os.environ.get("ZAD_GIT_BRANCH", ""),
        "build_date": os.environ.get("ZAD_BUILD_DATE", ""),
        "dirty": False,
        # Who is answering. POD_NAME comes from the downward API; outside Kubernetes
        # there is no pod and the field stays empty.
        "pod": os.environ.get("POD_NAME", ""),
        "image": get_running_image(),
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
