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
import re

import opi
from opi.core.templates import format_dutch_date

_TEMPLATES = pathlib.Path(opi.__file__).parent / "templates"


def test_the_filter_converts_to_our_own_timezone() -> None:
    """The case that made slicing wrong: late UTC is already the next day here."""
    assert format_dutch_date("2026-08-05T23:30:00+00:00", include_time=False) == "6 augustus 2026"


def test_no_template_slices_a_timestamp() -> None:
    """Any slice of a timestamp, not just ``[:10]``.

    The first version of this guard looked for ``date[:10]`` literally and missed
    ``status.last_sync[:19]|replace('T', ' ')`` on the deployment card, which rendered
    the sync time in UTC with "UTC" written after it. Same defect, two characters
    different. Matching the slice itself catches the shape rather than one spelling.
    """
    pattern = re.compile(r"\w+\[:\d+\]")
    offenders = [
        (path.relative_to(_TEMPLATES), match)
        for path in _TEMPLATES.rglob("*.j2")
        for match in pattern.findall(path.read_text(encoding="utf-8"))
        if any(word in match for word in ("date", "time", "sync", "_at"))
    ]
    assert offenders == [], f"these render a timestamp by slicing instead of via dutch_date: {offenders}"


def test_the_three_converted_templates_use_the_filter() -> None:
    for name in (
        "wizard/partials/approval_items.html.j2",
        "admin/approvals.html.j2",
        "project-details/section-deployments.html.j2",
    ):
        assert "dutch_date" in (_TEMPLATES / name).read_text(encoding="utf-8"), name
