"""The project detail page must not list backup snapshots itself.

Listing them opens a Kopia repository over S3. Measured on production for the
'wies' page: 2.09s to connect plus 0.41s to list, out of 3.4s total -- 70% of the
render, paid by every visitor whether or not they open the Backups tab. The
snapshots now load per deployment through their own fragment, like the ArgoCD
blocks already did.

These tests guard the split itself rather than the HTML: a future edit that
re-adds the fetch to the page handler is exactly the regression worth catching,
and it is invisible in a screenshot.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from opi.web import router

_TEMPLATES = Path(__file__).resolve().parents[1] / "opi" / "templates" / "project-details"


def test_the_page_handler_does_not_list_snapshots() -> None:
    source = inspect.getsource(router.project_details)
    assert "list_snapshots" not in source, (
        "project_details fetches backup snapshots again; that is a 2.5s Kopia round trip "
        "on every page view. Let the /backups/{deployment} fragment do it."
    )


def test_the_fragment_does_list_snapshots() -> None:
    source = inspect.getsource(router.backups_fragment)
    assert "list_snapshots" in source
    assert "is_user_authorized_for_project" in source, "the fragment is a separate entry point and needs its own check"


def test_the_section_defers_to_the_fragment() -> None:
    section = (_TEMPLATES / "section-backups.html.j2").read_text()

    assert 'hx-get="/projects/details/{{ project.name }}/backups/{{ deployment.name }}"' in section
    assert 'hx-trigger="load"' in section
    # deployment_backups is gone from the page context; a leftover reference would
    # silently render an empty list instead of failing.
    assert "deployment_backups" not in section


def test_the_restore_button_waits_for_the_snapshot_list() -> None:
    """It may only appear once we know there is something to restore.

    The page is rendered before the snapshots are known, so the button is placed
    out-of-band by the fragment instead of guessed at render time.
    """
    section = (_TEMPLATES / "section-backups.html.j2").read_text()
    fragment = (_TEMPLATES / "_backup-snapshots.html.j2").read_text()

    assert 'id="restore-btn-{{ deployment.name }}"' in section
    assert "Herstellen" not in section, "the page must not decide this"

    restore_block = re.search(r"\{% if backups %\}.*?\{% endif %\}", fragment, re.DOTALL)
    assert restore_block is not None
    assert 'hx-swap-oob="true"' in restore_block.group(0)
    assert "Herstellen" in restore_block.group(0)


def test_a_failed_backup_lookup_is_shown_not_swallowed() -> None:
    """An empty list and an unreachable repository look identical otherwise."""
    fragment = (_TEMPLATES / "_backup-snapshots.html.j2").read_text()
    assert "backups_error" in fragment

    source = inspect.getsource(router.backups_fragment)
    assert "backups_error" in source or "error" in source
