"""Tests for scripts/code_conventions.py and its gate: the ratchet on language and comment density.

The tests come in pairs: a Dutch identifier, a Dutch comment and a Dutch docstring have to
count, and English, an allowlisted domain term and a Dutch string the user SEES must not. Dutch
UI text is allowed by chapter 2 of docs/development/coding-rules.md.

The ratchet itself is measured the same way: equal is fine, one more fails, a file that is not in
the baseline has to be at zero, and updating the baseline only ever lowers a count.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from code_conventions import (  # noqa: E402
    BASELINE_FILE,
    COMMENT_HEAVY,
    DUTCH_COMMENTS,
    DUTCH_IDENTIFIERS,
    LINES_SHOWN,
    Finding,
    counts_per_file,
    dutch_in_identifier,
    identifier_parts,
    load_baseline,
    lowered,
    over_baseline,
    report,
    scan_source,
    scan_tree,
    write_baseline,
)


def _check_code_conventions_module():
    """The hyphenated CLI, loaded by path so its own ``main`` can be exercised directly."""
    existing = sys.modules.get("check_code_conventions")
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location("check_code_conventions", _SCRIPTS_DIR / "check-code-conventions.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_code_conventions"] = module
    spec.loader.exec_module(module)
    return module


def measures(source: str) -> dict[str, int]:
    return counts_per_file(scan_source(source, "opi/sample.py")).get("opi/sample.py", {})


def test_identifier_parts_splits_on_underscore_and_camel_case() -> None:
    assert identifier_parts("renderPaginaNaam") == ["render", "pagina", "naam"]
    assert identifier_parts("render_pagina_naam") == ["render", "pagina", "naam"]
    assert identifier_parts("HTTPFoutMelding") == ["http", "fout", "melding"]


def test_a_dutch_function_name_counts() -> None:
    assert measures("def maak_sleutel() -> None:\n    pass\n") == {DUTCH_IDENTIFIERS: 1}


def test_a_dutch_parameter_and_variable_count() -> None:
    assert measures("def f(waarde: int) -> None:\n    uitkomst = waarde\n") == {DUTCH_IDENTIFIERS: 2}


def test_an_english_name_does_not_count() -> None:
    assert measures("def create_key(value: int) -> None:\n    result = value\n") == {}


def test_a_domain_term_on_the_allowlist_does_not_count() -> None:
    assert dutch_in_identifier("zad_project") is None
    assert dutch_in_identifier("rijksapps_host") is None


def test_a_word_that_reads_as_english_too_is_left_out() -> None:
    """A missing word loses signal; a wrong one produces an alarm nobody can act on."""
    for name in ("condition_met", "is_a_no_op", "pad_sparse_row", "value_not_available_na"):
        assert dutch_in_identifier(name) is None, name


def test_a_quoted_fragment_in_english_prose_does_not_count() -> None:
    """Both false positives the precision sample turned up were of this shape."""
    source = (
        '"""A tab that shows "geen backups" for a project that cannot back anything up."""\n'
        "\n"
        'value = 1  # example name: "regel_k4c_pr104"\n'
    )
    assert measures(source) == {}


def test_dutch_prose_around_a_quoted_fragment_still_counts() -> None:
    assert measures("value = 1  # De waarde van `x` wordt niet gelezen.\n") == {DUTCH_COMMENTS: 1}


def test_a_dutch_comment_counts() -> None:
    assert measures("value = 1  # dit is de waarde die eruit komt\n") == {DUTCH_COMMENTS: 1}


def test_a_dutch_docstring_counts_once_however_long_it_is() -> None:
    source = '"""De eerste regel.\n\nEn nog een alinea die ook Nederlands is.\n"""\n'
    assert measures(source) == {DUTCH_COMMENTS: 1}


def test_an_english_comment_and_docstring_do_not_count() -> None:
    assert measures('"""Return the key."""\n\nvalue = 1  # the key as it was stored\n') == {}


def test_dutch_text_the_user_sees_does_not_count() -> None:
    """Chapter 2 allows Dutch in a label, a help text and an HTTPException detail."""
    source = (
        'LABEL = "Naam van het project"\n'
        'HELP = "Vul hier een geldige naam in, zonder hoofdletters."\n'
        'raise HTTPException(status_code=400, detail="Dit project bestaat niet")\n'
    )
    assert measures(source) == {}


def test_a_function_with_more_comment_than_code_counts() -> None:
    source = "def f() -> int:\n    # eerst dit\n    # dan dat\n    # en nog iets\n    return 1\n"
    assert measures(source)[COMMENT_HEAVY] == 1


def test_an_equal_split_is_not_comment_heavy() -> None:
    source = "def f() -> int:\n    # waarom dit zo is\n    return 1\n"
    assert COMMENT_HEAVY not in measures(source)


def test_the_docstring_is_not_counted_as_comment_or_as_code() -> None:
    source = 'def f() -> int:\n    """Return one."""\n    # why\n    return 1\n'
    assert COMMENT_HEAVY not in measures(source)


def test_a_trailing_comment_is_not_a_comment_line_for_the_density() -> None:
    source = "def f() -> int:\n    value = 1  # the one\n    return value  # back\n"
    assert COMMENT_HEAVY not in measures(source)


def test_a_nested_function_is_measured_on_its_own_and_not_twice() -> None:
    source = (
        "def outer() -> int:\n"
        "    def inner() -> int:\n"
        "        # eerst dit\n"
        "        # dan dat\n"
        "        return 1\n"
        "\n"
        "    return inner()\n"
    )
    assert measures(source)[COMMENT_HEAVY] == 1


def test_a_file_that_does_not_parse_is_passed_over() -> None:
    assert measures("def (:\n") == {}


def test_a_count_equal_to_the_baseline_passes() -> None:
    baseline = {"opi/a.py": {DUTCH_COMMENTS: 3}}
    assert over_baseline(baseline, {"opi/a.py": {DUTCH_COMMENTS: 3}}) == []


def test_one_more_than_the_baseline_fails() -> None:
    baseline = {"opi/a.py": {DUTCH_COMMENTS: 3}}
    assert over_baseline(baseline, {"opi/a.py": {DUTCH_COMMENTS: 4}}) == [("opi/a.py", DUTCH_COMMENTS, 4, 3)]


def test_a_file_outside_the_baseline_has_to_be_at_zero() -> None:
    assert over_baseline({}, {"opi/new.py": {DUTCH_IDENTIFIERS: 1}}) == [("opi/new.py", DUTCH_IDENTIFIERS, 1, 0)]


def test_a_measure_outside_the_baseline_entry_has_to_be_at_zero() -> None:
    baseline = {"opi/a.py": {DUTCH_COMMENTS: 3}}
    current = {"opi/a.py": {DUTCH_COMMENTS: 3, COMMENT_HEAVY: 1}}
    assert over_baseline(baseline, current) == [("opi/a.py", COMMENT_HEAVY, 1, 3 - 3)]


def test_updating_the_baseline_only_lowers_a_count() -> None:
    baseline = {"opi/a.py": {DUTCH_COMMENTS: 5}, "opi/b.py": {DUTCH_COMMENTS: 2}}
    current = {"opi/a.py": {DUTCH_COMMENTS: 1}, "opi/b.py": {DUTCH_COMMENTS: 9}}
    assert lowered(baseline, current) == {"opi/a.py": {DUTCH_COMMENTS: 1}, "opi/b.py": {DUTCH_COMMENTS: 2}}


def test_updating_the_baseline_drops_a_file_that_came_clean() -> None:
    assert lowered({"opi/a.py": {DUTCH_COMMENTS: 5}}, {"opi/a.py": {}}) == {}


def test_updating_the_baseline_never_adds_a_file() -> None:
    assert lowered({}, {"opi/new.py": {DUTCH_COMMENTS: 4}}) == {}


def test_updating_the_baseline_drops_a_file_that_is_gone() -> None:
    assert lowered({"opi/weg.py": {DUTCH_COMMENTS: 5}}, {}) == {}


def test_the_baseline_is_json_that_round_trips(tmp_path: Path) -> None:
    counts = {"opi/a.py": {DUTCH_COMMENTS: 2, COMMENT_HEAVY: 1}}
    path = tmp_path / "code-conventions-baseline.json"
    write_baseline(path, counts)
    assert load_baseline(path) == counts
    assert "GENERATED" in json.loads(path.read_text(encoding="utf-8"))["_"]


@pytest.mark.slow
def test_this_repository_is_within_its_baseline_right_now() -> None:
    """The gate itself, so a build outside pre-commit has it too."""
    exceeded = over_baseline(load_baseline(BASELINE_FILE), counts_per_file(scan_tree()))
    text = "\n".join(f"  {name}: {measure} is {now}, baseline {allowed}" for name, measure, now, allowed in exceeded)
    assert not exceeded, f"counts above the baseline, run `task code-conventions` for the lines:\n{text}"


def test_report_passes_and_prints_clean_when_nothing_exceeds(capsys: pytest.CaptureFixture) -> None:
    code = report([], {"opi/a.py": {DUTCH_COMMENTS: 1}}, scanned=3)
    assert code == 0
    assert "CLEAN 3 files" in capsys.readouterr().out


def test_report_fails_and_names_the_file_measure_and_line(capsys: pytest.CaptureFixture) -> None:
    findings = [Finding("opi/new.py", DUTCH_IDENTIFIERS, 7, "maak_iets: maak")]
    code = report(findings, {}, scanned=1)

    out = capsys.readouterr().out
    assert code == 1
    assert "opi/new.py: dutch-identifiers is 1, the baseline allows 0" in out
    assert "line 7: maak_iets: maak" in out


def test_report_cuts_a_long_list_and_says_how_many_are_left(capsys: pytest.CaptureFixture) -> None:
    findings = [Finding("opi/big.py", DUTCH_COMMENTS, n, "comment: het") for n in range(1, 41)]
    assert report(findings, {"opi/big.py": {DUTCH_COMMENTS: 39}}, scanned=1) == 1

    out = capsys.readouterr().out
    assert out.count("comment: het") == LINES_SHOWN
    assert f"... and {40 - LINES_SHOWN} more" in out


def test_report_flag_prints_every_finding_and_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    module = _check_code_conventions_module()
    finding = Finding("opi/a.py", DUTCH_COMMENTS, 2, "comment: dit")
    monkeypatch.setattr(module, "scan_tree", lambda: [finding])
    monkeypatch.setattr(module, "python_files", lambda: [Path("opi/a.py")])

    assert module.main(["--report"]) == 0
    out = capsys.readouterr().out
    assert str(finding) in out
    assert "1 counting lines in 1 files" in out


def test_default_gate_passes_when_within_baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _check_code_conventions_module()
    baseline_path = tmp_path / "code-conventions-baseline.json"
    monkeypatch.setattr(module, "BASELINE_FILE", baseline_path)
    monkeypatch.setattr(module, "scan_tree", list)
    monkeypatch.setattr(module, "python_files", lambda: [Path("opi/a.py")])

    assert module.main([]) == 0


def test_default_gate_fails_on_a_new_file_outside_the_baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _check_code_conventions_module()
    baseline_path = tmp_path / "code-conventions-baseline.json"
    monkeypatch.setattr(module, "BASELINE_FILE", baseline_path)
    monkeypatch.setattr(module, "scan_tree", lambda: [Finding("opi/new.py", DUTCH_IDENTIFIERS, 7, "maak: maak")])
    monkeypatch.setattr(module, "python_files", lambda: [Path("opi/new.py")])

    assert module.main([]) == 1


def test_initial_is_refused_once_the_baseline_has_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--initial writes a baseline, it never replaces one: that would be the way to grow it."""
    module = _check_code_conventions_module()
    baseline_path = tmp_path / "code-conventions-baseline.json"
    write_baseline(baseline_path, {"opi/a.py": {DUTCH_COMMENTS: 1}})
    monkeypatch.setattr(module, "BASELINE_FILE", baseline_path)
    monkeypatch.setattr(module, "scan_tree", lambda: [Finding("opi/a.py", DUTCH_COMMENTS, 1, "comment: dit")])
    monkeypatch.setattr(module, "python_files", lambda: [Path("opi/a.py")])

    assert module.main(["--initial"]) == 1
    assert load_baseline(baseline_path) == {"opi/a.py": {DUTCH_COMMENTS: 1}}


def test_initial_writes_a_fresh_baseline_when_there_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _check_code_conventions_module()
    baseline_path = tmp_path / "code-conventions-baseline.json"
    monkeypatch.setattr(module, "BASELINE_FILE", baseline_path)
    monkeypatch.setattr(module, "scan_tree", lambda: [Finding("opi/a.py", DUTCH_COMMENTS, 1, "comment: dit")])
    monkeypatch.setattr(module, "python_files", lambda: [Path("opi/a.py")])

    assert module.main(["--initial"]) == 0
    assert load_baseline(baseline_path) == {"opi/a.py": {DUTCH_COMMENTS: 1}}


def test_update_baseline_only_lowers_what_is_already_there(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _check_code_conventions_module()
    baseline_path = tmp_path / "code-conventions-baseline.json"
    write_baseline(baseline_path, {"opi/a.py": {DUTCH_COMMENTS: 5}, "opi/weg.py": {DUTCH_COMMENTS: 2}})
    monkeypatch.setattr(module, "BASELINE_FILE", baseline_path)
    monkeypatch.setattr(module, "scan_tree", lambda: [Finding("opi/a.py", DUTCH_COMMENTS, 1, "comment: dit")])
    monkeypatch.setattr(module, "python_files", lambda: [Path("opi/a.py")])

    assert module.main(["--update-baseline"]) == 0
    assert load_baseline(baseline_path) == {"opi/a.py": {DUTCH_COMMENTS: 1}}
