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

from opi.services.catalog.shared import backups

_TEMPLATES = Path(__file__).resolve().parents[1] / "opi" / "services" / "catalog" / "shared"


def test_the_page_handler_does_not_list_snapshots() -> None:
    from opi.web import router

    source = inspect.getsource(router.project_details)
    assert "list_snapshots" not in source, (
        "project_details fetches backup snapshots again; that is a 2.5s Kopia round trip "
        "on every page view. Let the /backups/{deployment} fragment do it."
    )


def test_the_fragment_does_list_snapshots() -> None:
    source = inspect.getsource(backups.backups_fragment)
    assert "list_snapshots" in source
    assert "is_user_authorized_for_project" in source, "the fragment is a separate entry point and needs its own check"


def test_the_section_fires_exactly_one_lazy_request() -> None:
    """One request for the whole section, never one per deployment.

    Per-deployment triggers OOM-killed the pod: 'wies' has 18 deployments in one
    namespace, so 18 per-deployment blocks opened 18 parallel Kopia connects to the
    same repository. This is the regression guard for that.

    'intersect once', not 'load': the section has its own tab since RC-100, so the reason
    to HIDE it is gone but the reason to load it lazily is not -- opening a Kopia
    repository over S3 costs 2.5s, and the page should be there before that.
    """
    section = (_TEMPLATES / "section-backups.html.j2").read_text()

    assert section.count("hx-trigger=") == 1, "exactly one loader for the whole backups section"
    assert 'hx-trigger="intersect once"' in section
    assert 'hx-get="/projects/details/{{ ctx.project_name }}/backups"' in section
    # The loader sits behind a flag rather than being unconditional: the block that
    # carries it is decided in Python (one per PAGE since RC-100), not by the template.
    assert "{% if ctx.loads_snapshots %}" in section
    # Not the per-deployment URL that caused the swarm.
    assert "/backups/{{ ctx.deployment_name }}" not in section
    assert "deployment_backups" not in section


def test_the_fragment_lists_each_namespace_once() -> None:
    """Deployments share a namespace, so the sweep must dedupe on namespace.

    18 wies deployments live in one namespace; listing per deployment would reopen
    the same Kopia repository 18 times even in a single request.
    """
    source = inspect.getsource(backups.backups_fragment)
    assert "per_namespace" in source
    assert "if k8s_namespace in per_namespace" in source, "must skip a namespace already listed"


def test_the_restore_button_waits_for_the_snapshot_list() -> None:
    """It may only appear once we know there is something to restore.

    The page is rendered before the snapshots are known, so the button is placed
    out-of-band by the fragment instead of guessed at render time.
    """
    section = (_TEMPLATES / "section-backups.html.j2").read_text()
    fragment = (_TEMPLATES / "_backup-snapshots.html.j2").read_text()

    assert 'id="restore-btn-{{ ctx.deployment_name }}"' in section
    assert "Herstellen" not in section, "the page must not decide this"

    restore_block = re.search(r"\{% if backups %\}.*?\{% endif %\}", fragment, re.DOTALL)
    assert restore_block is not None
    assert 'hx-swap-oob="true"' in restore_block.group(0)
    assert "Herstellen" in restore_block.group(0)


def test_every_deployment_block_is_filled_out_of_band() -> None:
    """One response fills all the per-deployment placeholders."""
    fragment = (_TEMPLATES / "_backup-snapshots.html.j2").read_text()
    assert 'id="backups-snapshots-{{ deployment.name }}" hx-swap-oob="true"' in fragment
    assert "for deployment in deployments" in fragment


def test_a_failed_backup_lookup_is_shown_not_swallowed() -> None:
    """An empty list and an unreachable repository look identical otherwise."""
    fragment = (_TEMPLATES / "_backup-snapshots-one.html.j2").read_text()
    assert "backups_error" in fragment

    source = inspect.getsource(backups.backups_fragment)
    assert "backups_error" in source or "error" in source
