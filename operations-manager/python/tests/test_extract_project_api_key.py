"""Tests for scripts/extract_project_api_key.py.

Only ``_key_content_from_secret``'s handling of a missing kubectl binary: the rest of the
script's two-step unwrap chain is the same code ``unwrap_project_key_sync`` already covers
in tests/test_project_key_wrapping.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import extract_project_api_key as tool  # noqa: E402


def test_a_missing_kubectl_binary_is_the_same_answer_as_a_missing_secret() -> None:
    """No kubectl on the PATH must read as "no such secret", not crash the caller.

    ``subprocess.run`` raises its own ``FileNotFoundError`` when the executable itself is
    not found, which is a different failure than a found-but-empty/non-zero kubectl call.
    Before the fix only the latter was handled (via the return code), so a fully offline
    run (master key and KEK both given as files, but resolving the OTHER one still calling
    this function) crashed instead of falling back.
    """
    with patch.object(tool.subprocess, "run", side_effect=FileNotFoundError("kubectl")):
        assert tool._key_content_from_secret("project-kek") is None


def test_get_project_kek_private_key_falls_back_without_kubectl() -> None:
    """``get_project_kek_private_key`` without --kek-file must not crash when kubectl is absent.

    A cluster that has not been converted to the KEK yet has no such secret either, so
    "no kubectl" and "no secret" are meant to lead to the same None.
    """
    args = argparse.Namespace(kek_file=None)

    with patch.object(tool.subprocess, "run", side_effect=FileNotFoundError("kubectl")):
        assert tool.get_project_kek_private_key(args) is None
