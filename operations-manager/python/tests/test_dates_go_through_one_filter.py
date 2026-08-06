"""Dates shown to a user go through ``dutch_date``, not through string slicing.

Three templates used to render ``entry.date[:10]``. That bypasses the one place that
knows about time zones, and it is wrong on its own: an ISO timestamp is UTC, so slicing
the first ten characters shows the UTC date. At 23:30 UTC that is yesterday here.

    format_dutch_date("2026-08-05T23:30:00+00:00")  ->  6 augustus 2026
    "2026-08-05T23:30:00+00:00"[:10]                ->  2026-08-05

The filter converts to Europe/Amsterdam and writes the month in Dutch, so it is both
correct and consistent with the rest of the interface.
"""

from __future__ import annotations

import pathlib

import opi
from opi.core.templates import format_dutch_date

_TEMPLATES = pathlib.Path(opi.__file__).parent / "templates"


def test_the_filter_converts_to_our_own_timezone() -> None:
    """The case that made slicing wrong: late UTC is already the next day here."""
    assert format_dutch_date("2026-08-05T23:30:00+00:00", include_time=False) == "6 augustus 2026"


def test_no_template_slices_a_date_string() -> None:
    """``[:10]`` on a timestamp is the shape this replaced; it must not come back."""
    offenders = [
        path.relative_to(_TEMPLATES)
        for path in _TEMPLATES.rglob("*.j2")
        if "date[:10]" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"these render a date by slicing instead of via dutch_date: {offenders}"


def test_the_three_converted_templates_use_the_filter() -> None:
    for name in (
        "wizard/partials/approval_items.html.j2",
        "admin/approvals.html.j2",
        "project-details/section-deployments.html.j2",
    ):
        assert "dutch_date" in (_TEMPLATES / name).read_text(encoding="utf-8"), name
