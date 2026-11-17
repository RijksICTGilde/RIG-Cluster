"""Polled fragments morph the DOM instead of replacing it, so their web components do not flicker."""

from pathlib import Path

import pytest

TEMPLATES = Path(__file__).parent.parent / "opi" / "templates_lotc"

POLLED_FRAGMENTS = [
    "bg/_task-progress.html.j2",
    "bg/_modal-wizard-progress.html.j2",
    "bg/_modal-wizard-progress-fragment.html.j2",
    "bg/_tasks.html.j2",
    "partials/task_progress_fragment.html.j2",
]


@pytest.mark.parametrize("template", POLLED_FRAGMENTS)
def test_polled_fragment_uses_morph_swap(template: str) -> None:
    source = (TEMPLATES / template).read_text()

    assert 'hx-ext="morph"' in source
    assert 'hx-swap="morph:outerHTML"' in source
    assert 'hx-swap="outerHTML"' not in source
