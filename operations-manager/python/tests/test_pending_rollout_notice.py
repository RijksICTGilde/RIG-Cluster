"""The drift a deferred rollout leaves behind has to be visible (RC-46).

A flag that lets the project file run ahead of the cluster is only safe if someone can
see that it happened, since when, and can act on it from there. These tests guard the
three things the notice promises: it appears when there is drift, it says how long, and
it offers the one button that resolves it.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from opi.core.templates_lotc import templates_lotc

_SECTION = "bg/_pending-rollout.html.j2"
_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "opi" / "templates_lotc"


def _render(pending: dict | None, user_role: str = "admin") -> str:
    return templates_lotc.get_template(_SECTION).render(
        {"pending_rollout": pending, "user_role": user_role, "project": {"name": "wies"}}
    )


def test_nothing_is_shown_when_the_project_is_in_sync() -> None:
    assert _render({"count": 0, "since": None, "task_types": []}).strip() == ""


def test_nothing_is_shown_when_the_count_could_not_be_determined() -> None:
    """A task service that is down must not produce a scary empty warning."""
    assert _render(None).strip() == ""


def test_the_notice_says_how_many_and_since_when() -> None:
    html = _render({"count": 3, "since": "2026-08-01T09:30:00+00:00", "task_types": ["add_component"]})

    assert "3 wijzigingen" in html
    # Formatted, not a raw ISO timestamp: "sinds wanneer" is the point.
    assert "1 augustus 2026" in html
    assert "2026-08-01T09:30:00" not in html


def test_one_change_reads_as_one_change() -> None:
    html = _render({"count": 1, "since": "2026-08-01T09:30:00+00:00", "task_types": ["add_component"]})

    assert "1 wijziging " in html
    assert "1 wijzigingen" not in html


def test_the_notice_offers_the_button_that_resolves_it() -> None:
    html = _render({"count": 1, "since": None, "task_types": []})

    assert "Nu verwerken" in html
    assert "/projects/wies/actions/refresh-project/confirm" in html


def test_a_reader_sees_the_drift_but_not_the_button() -> None:
    html = _render({"count": 1, "since": None, "task_types": []}, user_role="developer")

    assert "nog niet uitgerold" in html
    assert "Nu verwerken" not in html


def test_a_running_rollout_says_so_instead_of_claiming_nothing_happened() -> None:
    """De teller staat er tijdens het uitrollen nog; de tekst mag dan niet liegen."""
    html = _render(
        {
            "count": 2,
            "since": "2026-08-01T09:30:00+00:00",
            "task_types": ["add_component"],
            "rollout_in_progress": True,
        }
    )

    assert "op dit moment uitgerold" in html
    assert "er is niets naar het cluster gegaan" not in html
    assert "nog niet uitgerold" not in html
    # De knop vraagt om precies de verwerking die al loopt.
    assert "Nu verwerken" not in html


def test_one_change_being_rolled_out_reads_as_one_change() -> None:
    html = _render({"count": 1, "since": None, "task_types": [], "rollout_in_progress": True})

    assert "1 wijziging uit het projectbestand" in html
    assert "wordt nu verwerkt" in html
    assert "wijzigingen" not in html


def test_without_a_running_rollout_the_warning_stays() -> None:
    html = _render({"count": 2, "since": None, "task_types": [], "rollout_in_progress": False})

    assert "nog niet uitgerold" in html
    assert "er is niets naar het cluster gegaan" in html
    assert "op dit moment uitgerold" not in html


def test_the_notice_is_rendered_above_the_tabs() -> None:
    """Drift is about the whole project; it must not hide behind the right tab."""
    page = (_TEMPLATE_DIR / "bg" / "project-tabs.html.j2").read_text()

    assert _SECTION in page
    assert page.index(_SECTION) < page.index("<c-tabs")


def test_the_page_handler_measures_the_drift() -> None:
    from opi.web import router

    # De projectpagina wordt door twee routes bediend (met en zonder deployment in het
    # pad); ze delen dezelfde renderfunctie, en daar staat de meting in.
    source = inspect.getsource(router.render_project_page)
    assert "get_deferred_rollouts" in source
    assert '"pending_rollout": pending_rollout' in source
