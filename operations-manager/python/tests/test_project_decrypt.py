"""Tests for ``scripts/project_decrypt.py``'s ``read_key``.

This reader has its own code path (it also accepts the key itself as an argument, not
only a path), so it sat outside the shared ``require_private_file`` guard until RC-311
wired it in. The promise under test is that path, not the YAML decryption the rest of the
script does: a key file that others can read must stop this reader too, with the same
``chmod 600`` advice the other three readers give, and the refusal has to surface as
``SystemExit`` the way this script's other refusals already do.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from project_decrypt import read_key  # type: ignore[reportMissingImports]  # noqa: E402


def _write_private(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o600)
    return path


def test_a_key_passed_as_the_argument_itself_is_used_directly(tmp_path: Path) -> None:
    key = "AGE-SECRET-KEY-" + "A" * 58
    assert read_key(key) == key


def test_a_private_key_file_is_read(tmp_path: Path) -> None:
    key = "AGE-SECRET-KEY-" + "B" * 58
    path = _write_private(tmp_path / "key.txt", f"# created: today\n{key}\n")
    assert read_key(str(path)) == key


def test_a_missing_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=str(tmp_path / "nope.txt")):
        read_key(str(tmp_path / "nope.txt"))


def test_a_file_without_the_marker_line_is_refused(tmp_path: Path) -> None:
    path = _write_private(tmp_path / "key.txt", "no key in here\n")
    with pytest.raises(SystemExit, match="AGE-SECRET-KEY-"):
        read_key(str(path))


def test_a_key_file_others_can_read_is_refused_with_the_path_and_chmod_advice(tmp_path: Path) -> None:
    key = "AGE-SECRET-KEY-" + "C" * 58
    path = tmp_path / "key.txt"
    path.write_text(f"{key}\n")
    path.chmod(0o644)

    with pytest.raises(SystemExit) as caught:
        read_key(str(path))

    assert str(path) in str(caught.value)
    assert f"chmod 600 {path}" in str(caught.value)
