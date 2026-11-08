"""Tests for scripts/ast_equivalence.py: the proof that a cleanup round only changed text.

The script is only worth leaning on if it says no, so the tests come in pairs: rewriting a
comment or a docstring has to come out equal, and a changed string literal, an extra statement
or an unlisted rename has to come out different.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from ast_equivalence import difference, parse_renames  # noqa: E402

BEFORE = '''
"""Een moduledocstring in het Nederlands."""


def bereken(waarde: int) -> int:
    """Geeft de waarde plus een."""
    # tel er een bij op
    return waarde + 1
'''

AFTER_TEXT_ONLY = """
def bereken(waarde: int) -> int:
    return waarde + 1
"""


def test_comments_and_docstrings_removed_is_equal() -> None:
    assert difference(BEFORE, AFTER_TEXT_ONLY) is None


def test_added_comment_is_equal() -> None:
    after = "def f() -> int:\n    # why the offset is one\n    return 1\n"
    assert difference("def f() -> int:\n    return 1\n", after) is None


def test_changed_string_literal_differs() -> None:
    detail = difference('def f() -> str:\n    return "oud"\n', 'def f() -> str:\n    return "new"\n')
    assert detail is not None
    assert "f." in detail


def test_added_statement_differs() -> None:
    detail = difference("def f() -> int:\n    return 1\n", "def f() -> int:\n    x = 2\n    return x\n")
    assert detail is not None


def test_docstring_only_body_equals_pass() -> None:
    assert difference('def f() -> None:\n    """Doet niets."""\n', "def f() -> None:\n    pass\n") is None


def test_rename_differs_without_the_table() -> None:
    assert difference("def bereken() -> None:\n    pass\n", "def compute() -> None:\n    pass\n") is not None


def test_rename_is_equal_with_the_table() -> None:
    detail = difference(
        "def bereken(waarde: int) -> int:\n    return waarde\n",
        "def compute(value: int) -> int:\n    return value\n",
        {"bereken": "compute", "waarde": "value"},
    )
    assert detail is None


def test_rename_table_does_not_cover_a_second_name() -> None:
    detail = difference(
        "def bereken(waarde: int) -> int:\n    return waarde\n",
        "def compute(value: int) -> int:\n    return value\n",
        {"bereken": "compute"},
    )
    assert detail is not None


def test_rename_covers_a_global_declaration() -> None:
    before = "wachtend = 0\n\n\ndef bump() -> None:\n    global wachtend\n    wachtend += 1\n"
    after = "waiting = 0\n\n\ndef bump() -> None:\n    global waiting\n    waiting += 1\n"
    assert difference(before, after, {"wachtend": "waiting"}) is None


def test_difference_names_the_method_it_is_in() -> None:
    before = "class A:\n    def m(self) -> int:\n        return 1\n"
    after = "class A:\n    def m(self) -> int:\n        return 2\n"
    assert difference(before, after) == "A.m.statement 1 (Return vs Return)"


def test_difference_reports_a_shorter_body() -> None:
    detail = difference("def f() -> None:\n    a = 1\n    b = 2\n", "def f() -> None:\n    a = 1\n")
    assert detail is not None
    assert "statements" in detail


def test_parse_renames_reads_pairs() -> None:
    assert parse_renames(["a=b", "c=d"]) == {"a": "b", "c": "d"}


@pytest.mark.parametrize("pair", ["a", "=b", "a="])
def test_parse_renames_refuses_a_half_pair(pair: str) -> None:
    with pytest.raises(SystemExit):
        parse_renames([pair])
