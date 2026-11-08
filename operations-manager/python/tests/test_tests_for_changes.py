"""Tests for the changed-files-to-test-files mapping used by `task test-changed`.

The mapping is by name, not by import graph, so what must stay correct is: a changed
``opi/x/y.py`` yields the dotted module ``opi.x.y`` to search for, a changed test file
picks itself without needing to be found in another file's source, and a file outside
``opi/`` or ``manifests/`` contributes no needle at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import scripts.tests_for_changes as tfc
from scripts.tests_for_changes import changed_files, needles

if TYPE_CHECKING:
    import pytest

#: Imported under an alias: its name starts with ``test_``, so pytest would otherwise
#: try to collect the function itself as a test.
select_test_files = tfc.test_files_for


def test_needles_for_an_opi_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tfc, "PYTHON_ROOT", Path("/repo/python"))
    path = Path("/repo/python/opi/manager/project_manager.py")

    assert needles(path) == ["opi.manager.project_manager"]


def test_needles_for_an_opi_package_init(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tfc, "PYTHON_ROOT", Path("/repo/python"))
    path = Path("/repo/python/opi/manager/__init__.py")

    assert needles(path) == ["opi.manager"]


def test_needles_for_a_manifest_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tfc, "PYTHON_ROOT", Path("/repo/python"))
    path = Path("/repo/python/manifests/deployment.yaml.jinja")

    assert needles(path) == ["deployment.yaml.jinja"]


def test_needles_is_empty_outside_opi_and_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tfc, "PYTHON_ROOT", Path("/repo/python"))

    assert needles(Path("/repo/python/scripts/some_tool.py")) == []


def test_needles_is_empty_for_a_path_outside_python_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tfc, "PYTHON_ROOT", Path("/repo/python"))

    assert needles(Path("/repo/workflow/build.md")) == []


def _make_tree(tmp_path: Path) -> Path:
    python_root = tmp_path / "python"
    (python_root / "opi" / "manager").mkdir(parents=True)
    (python_root / "opi" / "manager" / "project_manager.py").write_text("")
    tests_root = python_root / "tests"
    tests_root.mkdir()
    (tests_root / "test_project_manager.py").write_text("from opi.manager.project_manager import ProjectManager\n")
    (tests_root / "test_unrelated.py").write_text("import os\n")
    (tests_root / "test_changed_itself.py").write_text("import os\n")
    return python_root


def test_test_files_for_a_changed_module_finds_its_test(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    python_root = _make_tree(tmp_path)
    monkeypatch.setattr(tfc, "PYTHON_ROOT", python_root)

    changed = [python_root / "opi" / "manager" / "project_manager.py"]
    result = select_test_files(changed)

    assert result == [Path("tests/test_project_manager.py")]


def test_test_files_for_a_changed_test_file_selects_itself(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    python_root = _make_tree(tmp_path)
    monkeypatch.setattr(tfc, "PYTHON_ROOT", python_root)

    changed = [python_root / "tests" / "test_changed_itself.py"]
    result = select_test_files(changed)

    assert result == [Path("tests/test_changed_itself.py")]


def test_test_files_for_no_matching_change_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    python_root = _make_tree(tmp_path)
    monkeypatch.setattr(tfc, "PYTHON_ROOT", python_root)

    changed = [python_root / "README.md"]

    assert select_test_files(changed) == []


def test_changed_files_combines_diff_and_status_without_duplicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tfc, "REPO_ROOT", tmp_path)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if argv[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(argv, 0, stdout="abc123\n", stderr="")
        if argv[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(argv, 0, stdout="opi/a.py\nopi/b.py\n", stderr="")
        if argv[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(argv, 0, stdout=" M opi/a.py\n?? opi/c.py\n", stderr="")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(tfc.subprocess, "run", fake_run)

    result = changed_files("origin/main")

    assert sorted(p.name for p in result) == ["a.py", "b.py", "c.py"]
