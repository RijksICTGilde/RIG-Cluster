"""Tests for scripts/lint_baseline.py: the ruff ratchet over operations-manager/python.

The baseline exists so a rule from docs/development/coding-rules.md can be switched on while the
code that predates it stays green. That only works in one direction: an entry disappears when the
file stops breaking the rule, and nothing is ever added by tooling.

The measurement below is the half that makes a cleaned-up file leave the list, so a baseline
entry cannot outlive the violation it was written for.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lint_baseline import (  # noqa: E402
    BASELINE_FILE,
    load_baseline,
    main,
    pruned,
    render_baseline,
    ruff_violations,
    stale_entries,
)

ruff_missing = pytest.mark.skipif(
    importlib.util.find_spec("ruff") is None, reason="ruff is not installed in this environment"
)


@ruff_missing
def test_no_baseline_entry_outlives_its_violation() -> None:
    stale = stale_entries(load_baseline(BASELINE_FILE), ruff_violations())
    report = "\n".join(f"  {path}: {', '.join(sorted(codes))}" for path, codes in sorted(stale.items()))
    assert not stale, f"stale entries in {BASELINE_FILE.name}, run `task lint-baseline`:\n{report}"


@ruff_missing
def test_the_repository_is_green_under_the_baseline() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "."],
        cwd=BASELINE_FILE.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout


class TestYamlBannedApiGate:
    """TID251 in pyproject.toml: ``yaml`` and every import path to ``ruamel.yaml.YAML``
    are banned outside ``opi/utils/yaml_util.py``, which owns every YAML parser and emitter
    in opi (docs/development/coding-rules.md, chapter 5)."""

    @ruff_missing
    def test_yaml_and_ruamel_yaml_are_banned_outside_yaml_util(self, tmp_path: Path) -> None:
        """No --select: this is what `ruff check .` flags by default, the command the
        gate actually runs as. A gate that only fires under an explicit --select is off."""
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import yaml\nfrom ruamel.yaml import YAML\nfrom ruamel.yaml.main import YAML as Y\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--no-cache", str(probe)],
            cwd=BASELINE_FILE.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode != 0, "the banned-api gate no longer flags yaml or ruamel.yaml.YAML"
        assert "`yaml` is banned" in completed.stdout
        assert "`ruamel.yaml.YAML` is banned" in completed.stdout
        assert "`ruamel.yaml.main.YAML` is banned" in completed.stdout

    @ruff_missing
    def test_the_ruamel_types_stay_available(self, tmp_path: Path) -> None:
        """Only the parser and emitter are banned. CommentedMap and the scalar-string types
        are what the profiles hand back, so every caller needs them."""
        probe = tmp_path / "types.py"
        probe.write_text(
            "from ruamel.yaml.comments import CommentedMap\n"
            "from ruamel.yaml.error import YAMLError\n"
            "from ruamel.yaml.scalarstring import LiteralScalarString\n"
            "__all__ = ['CommentedMap', 'YAMLError', 'LiteralScalarString']\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--no-cache", str(probe)],
            cwd=BASELINE_FILE.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        assert "TID251" not in completed.stdout, completed.stdout

    @ruff_missing
    def test_yaml_util_itself_is_exempt(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--no-cache", "opi/utils/yaml_util.py"],
            cwd=BASELINE_FILE.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        assert "TID251" not in completed.stdout, completed.stdout


def test_stale_entries_names_the_rule_a_file_no_longer_breaks() -> None:
    baseline = {"opi/a.py": {"C901", "BLE001"}, "opi/b.py": {"ANN401"}}
    current = {"opi/a.py": {"C901"}}
    assert stale_entries(baseline, current) == {"opi/a.py": {"BLE001"}, "opi/b.py": {"ANN401"}}


def test_pruning_drops_what_is_clean_and_adds_nothing() -> None:
    baseline = {"opi/a.py": {"C901", "BLE001"}}
    current = {"opi/a.py": {"C901"}, "opi/new.py": {"ANN401"}}
    assert pruned(baseline, current) == {"opi/a.py": {"C901"}}


def test_a_file_without_violations_leaves_the_baseline() -> None:
    assert pruned({"opi/a.py": {"C901"}}, {}) == {}


def test_render_and_load_are_each_other_reverse(tmp_path: Path) -> None:
    entries = {"opi/b.py": {"ANN401"}, "opi/a.py": {"C901", "BLE001"}}
    path = tmp_path / "ruff-baseline.toml"
    path.write_text(render_baseline(entries), encoding="utf-8")
    assert load_baseline(path) == entries


def test_initial_is_refused_once_the_baseline_has_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--initial writes a baseline, it never replaces one: that would be the way to grow it."""
    import lint_baseline

    path = tmp_path / "ruff-baseline.toml"
    path.write_text(render_baseline({"opi/a.py": {"C901"}}), encoding="utf-8")
    monkeypatch.setattr(lint_baseline, "BASELINE_FILE", path)
    monkeypatch.setattr(lint_baseline, "ruff_violations", lambda: {"opi/a.py": {"C901", "BLE001"}})

    assert main(["--initial"]) == 1
    assert load_baseline(path) == {"opi/a.py": {"C901"}}


def test_check_fails_on_a_stale_entry_and_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import lint_baseline

    path = tmp_path / "ruff-baseline.toml"
    original = render_baseline({"opi/a.py": {"C901", "BLE001"}})
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(lint_baseline, "BASELINE_FILE", path)
    monkeypatch.setattr(lint_baseline, "ruff_violations", lambda: {"opi/a.py": {"C901"}})

    assert main(["--check"]) == 1
    assert path.read_text(encoding="utf-8") == original
    assert main([]) == 0
    assert load_baseline(path) == {"opi/a.py": {"C901"}}
