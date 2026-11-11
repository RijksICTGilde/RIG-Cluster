"""Tests for the test-suite inventory report (`task test-inventaris`).

What must stay correct: the kind a file gets (so a real-database file never reads as
"eigen logica"), the module a file is attributed to, and that the report's numbers add up
to what the junit-xml said.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from scripts.suite_inventory import FileStats, classify, file_of, module_of, read_junit, report

if TYPE_CHECKING:
    from pathlib import Path


def test_real_database_wins_over_render() -> None:
    """A file that starts a real database AND renders a template is real_database first."""
    source = "def test_x(orm_db):\n    render_template('x.html')\n"
    assert classify(source) == "real_database"


def test_real_binary_from_subprocess_call() -> None:
    assert classify("subprocess.run(['sops', '-d', path])") == "real_binary"


def test_http_client_detected() -> None:
    assert classify("def test_x(test_client: TestClient) -> None:\n    test_client.get('/x')\n") == "http_client"


def test_static_code_on_ast_walk() -> None:
    assert classify("tree = ast.parse(source)\nfor node in ast.walk(tree):\n    pass\n") == "static_code"


def test_render_detected_case_insensitively() -> None:
    assert classify("html = templaterenderer.render_template('x.html')\n") == "render"


def test_mock_interaction_when_assert_called_dominates() -> None:
    source = "mock.assert_called_once_with(x)\nmock.assert_called_with(y)\nassert mock.called\n"
    assert classify(source) == "mock_interaction"


def test_own_logic_when_no_pattern_and_few_assert_called() -> None:
    source = "assert add(1, 2) == 3\nassert add(0, 0) == 0\nassert add(-1, 1) == 0\n"
    assert classify(source) == "own_logic"


def test_own_logic_on_empty_source() -> None:
    """A source that could not be read (``source_of`` swallows OSError) classifies as own_logic."""
    assert classify("") == "own_logic"


def test_module_of_picks_the_most_common_opi_import() -> None:
    source = "from opi.manager.project_manager import ProjectManager\nfrom opi.manager.project_manager import X\nimport opi.utils.naming\n"
    assert module_of(source) == "opi.manager.project_manager"


def test_module_of_falls_back_to_dash_without_opi_import() -> None:
    assert module_of("import os\nimport sys\n") == "-"


def test_file_of_prefers_the_junit_file_attribute() -> None:
    assert file_of("tests/test_x.py", "whatever.classname") == "tests/test_x.py"


def test_file_of_walks_classname_back_to_an_existing_file() -> None:
    """junit only names the dotted class; it must resolve to the real test file on disk."""
    classname = "tests.test_suite_inventory.SomeClass"
    assert file_of(None, classname) == "tests/test_suite_inventory.py"


def test_file_of_falls_back_to_the_classname_when_no_file_matches() -> None:
    assert file_of(None, "tests.does_not_exist.Nested") == "tests/does_not_exist/Nested.py"


def _junit_xml(tmp_path: Path) -> Path:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    case_ok = ET.SubElement(
        suite, "testcase", file="tests/test_a.py", classname="tests.test_a", name="test_ok", time="0.5"
    )
    case_slow = ET.SubElement(
        suite, "testcase", file="tests/test_a.py", classname="tests.test_a", name="test_slow", time="2.5"
    )
    case_broken = ET.SubElement(
        suite, "testcase", file="tests/test_b.py", classname="tests.test_b", name="test_broken", time="0.1"
    )
    ET.SubElement(case_broken, "failure", message="boom")
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(path)
    return path


def test_read_junit_groups_by_file_and_sums_time(tmp_path: Path) -> None:
    per_file = read_junit(_junit_xml(tmp_path))

    assert per_file["tests/test_a.py"].tests == 2
    assert per_file["tests/test_a.py"].seconds == 3.0
    assert per_file["tests/test_b.py"].failures == 1


def test_report_totals_match_the_input_stats() -> None:
    per_file = {
        "tests/test_a.py": FileStats("tests/test_a.py", tests=2, seconds=3.0, slowest=[(2.5, "test_slow")]),
        "tests/test_b.py": FileStats(
            "tests/test_b.py", tests=1, seconds=0.1, failures=1, slowest=[(0.1, "test_broken")]
        ),
    }

    text = report(per_file, slowest_files=10, slowest_tests=10)

    assert "3 toetsen" in text
    assert "`tests/test_a.py`" in text
    assert "## Bestanden met rood in deze run" in text
    assert "`tests/test_b.py`" in text


def test_report_omits_the_broken_section_when_everything_is_green() -> None:
    per_file = {"tests/test_a.py": FileStats("tests/test_a.py", tests=1, seconds=0.1)}

    text = report(per_file, slowest_files=10, slowest_tests=10)

    assert "Bestanden met rood" not in text
