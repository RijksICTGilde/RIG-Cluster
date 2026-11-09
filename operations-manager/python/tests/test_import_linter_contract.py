"""Proves the import-linter layers gate (docs/development/coding-rules.md chapter 5 and 11)
actually turns red. The gate is new config in pyproject.toml, not application behavior, so
these are the regression tests for the gate itself: a layer violation, a needed exception
removed, a superfluous exception added, and the TYPE_CHECKING exclusion removed must each
break the contract.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

_PYTHON_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _PYTHON_ROOT / "pyproject.toml"
_PROBE_FILE = _PYTHON_ROOT / "opi" / "core" / "_test_probe_layer_violation.py"
_IGNORE_IMPORT_LINE_RE = re.compile(r'^\s*"([\w.]+) -> ([\w.]+)",\s*$', re.MULTILINE)

importlinter_missing = pytest.mark.skipif(
    importlib.util.find_spec("importlinter") is None,
    reason="import-linter is not installed in this environment",
)


def _importlinter_config_text() -> str:
    """The [tool.importlinter] table plus its [[contracts]] subtable, as a standalone
    document import-linter can load via --config, without touching the real pyproject.toml."""
    text = _PYPROJECT.read_text(encoding="utf-8")
    start = text.index("[tool.importlinter]")
    rest = text[start:]
    next_top_level_section = re.search(r"\n\[(?!\[?tool\.importlinter)", rest)
    return rest if next_top_level_section is None else rest[: next_top_level_section.start()]


def _lint(config_text: str, tmp_path: Path, cache_dir: Path) -> int:
    """Runs import-linter in a subprocess; in-process would disable loggers and break caplog tests."""
    config_file = tmp_path / "import_linter.toml"
    config_file.write_text(config_text, encoding="utf-8")
    script = (
        "import sys\n"
        "from importlinter.cli import lint_imports\n"
        f"sys.exit(lint_imports(config_filename={str(config_file)!r}, "
        f"cache_dir={str(cache_dir)!r}, no_logo=True))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_PYTHON_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("import_linter_cache")


@importlinter_missing
def test_the_repository_is_green_under_the_layers_contract(tmp_path: Path, cache_dir: Path) -> None:
    assert _lint(_importlinter_config_text(), tmp_path, cache_dir) == 0


@importlinter_missing
def test_a_layer_violation_turns_the_gate_red(tmp_path: Path, cache_dir: Path) -> None:
    """opi.core (layer 9) importing from opi.manager (layer 5) is a pijl omhoog: the
    layers contract must reject it, without needing an ignore_imports entry for it."""
    _PROBE_FILE.write_text(
        "from opi.manager.project_manager import ProjectManager  # noqa: F401\n",
        encoding="utf-8",
    )
    try:
        exit_code = _lint(_importlinter_config_text(), tmp_path, cache_dir)
    finally:
        _PROBE_FILE.unlink()
    assert exit_code != 0


@importlinter_missing
def test_removing_a_needed_ignore_import_turns_the_gate_red(tmp_path: Path, cache_dir: Path) -> None:
    """Every ignore_imports entry exists because the import it names is real today. Taking
    the first one out must make the gate catch that real, unexempted layer violation again."""
    config_text = _importlinter_config_text()
    match = _IGNORE_IMPORT_LINE_RE.search(config_text)
    assert match, "no ignore_imports entry found in the layers contract"
    config_without_exception = config_text.replace(match.group(0), "", 1)
    exit_code = _lint(config_without_exception, tmp_path, cache_dir)
    assert exit_code != 0, f"removing {match.group(1)} -> {match.group(2)} did not break the gate"


@importlinter_missing
def test_adding_a_superfluous_ignore_import_turns_the_gate_red(tmp_path: Path, cache_dir: Path) -> None:
    """unmatched_ignore_imports_alerting = "error" is the ratchet: an ignore_imports entry
    for an import that is not actually there must break the gate, so the list can only
    shrink and never silently grow with exceptions nobody needs anymore."""
    config_text = _importlinter_config_text()
    superfluous_entry = '    "opi.connectors.skopeo -> opi.services.project_store",\n'
    trimmed = config_text.rstrip("\n")
    assert trimmed.endswith("]"), "expected the config to end with the ignore_imports closing bracket"
    config_with_extra_exception = trimmed[:-1] + superfluous_entry + "]\n"
    exit_code = _lint(config_with_extra_exception, tmp_path, cache_dir)
    assert exit_code != 0


@importlinter_missing
def test_removing_the_type_checking_exclusion_turns_the_gate_red(tmp_path: Path, cache_dir: Path) -> None:
    """exclude_type_checking_imports = true hides real layer violations that exist today
    only under TYPE_CHECKING (e.g. opi.manager importing opi.runtime for a type hint).
    Without it those would need their own ignore_imports entries, so removing the setting
    must break the gate just like removing one of those entries would."""
    config_text = _importlinter_config_text()
    assert "exclude_type_checking_imports = true" in config_text
    config_without_exclusion = config_text.replace("exclude_type_checking_imports = true\n", "", 1)
    exit_code = _lint(config_without_exclusion, tmp_path, cache_dir)
    assert exit_code != 0, "removing exclude_type_checking_imports did not break the gate"
