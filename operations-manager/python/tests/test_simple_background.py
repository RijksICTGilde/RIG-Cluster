"""
Tests for opi.core.simple_background module.

Tests _continuous_monitoring to verify event/log data is stored after collection.
"""

import ast
import inspect
import textwrap

from opi.core.simple_background import _continuous_monitoring


class TestContinuousMonitoringCodeStructure:
    """Tests that _continuous_monitoring has correct code structure."""

    def test_event_log_update_code_is_reachable(self):
        """Bug: event/log update code must not be after a 'continue' statement.

        The original code had the update block inside an 'else' branch
        after a 'continue', making it dead code. Events and logs were
        collected but never stored in _projects.
        """
        source = inspect.getsource(_continuous_monitoring)
        # Dedent the source so AST can parse it
        source = textwrap.dedent(source)
        tree = ast.parse(source)

        # Walk the AST looking for any 'continue' statement that has
        # siblings after it (dead code)
        dead_code_found = False
        for node in ast.walk(tree):
            # Check statement lists in all compound statements
            for _field_name, field_value in ast.iter_fields(node):
                if isinstance(field_value, list):
                    for i, item in enumerate(field_value):
                        if isinstance(item, ast.Continue):
                            # Check if there are statements after the continue
                            remaining = field_value[i + 1 :]
                            if remaining:
                                dead_code_found = True

        assert not dead_code_found, (
            "Found dead code after 'continue' statement in _continuous_monitoring. "
            "Event/log update code is unreachable."
        )

    def test_projects_events_assignment_exists_in_reachable_code(self):
        """Verify that _projects[task_id].events assignment is in reachable code."""
        source = inspect.getsource(_continuous_monitoring)
        lines = source.split("\n")

        # Find lines that update _projects events/logs
        update_lines = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if "_projects[task_id].events" in stripped or "_projects[task_id].logs" in stripped:
                update_lines.append((i, stripped))

        assert len(update_lines) > 0, (
            "No event/log update code found in _continuous_monitoring. "
            "Events and logs collected during monitoring would never be stored."
        )

        # Verify none of the update lines appear after a 'continue' in the same block
        for line_idx, line_content in update_lines:
            # Check preceding lines in the same block for a 'continue'
            for j in range(line_idx - 1, max(0, line_idx - 5), -1):
                preceding = lines[j].strip()
                if preceding == "continue":
                    # Check indentation: if continue is at same or lower indent, it blocks this code
                    continue_indent = len(lines[j]) - len(lines[j].lstrip())
                    update_indent = len(lines[line_idx]) - len(lines[line_idx].lstrip())
                    if continue_indent <= update_indent:
                        raise AssertionError(
                            f"Event/log update on line {line_idx} is after 'continue' on line {j}, "
                            f"making it dead code: {line_content}"
                        )
