"""The project detail page shows one total for the whole project.

A project's deployments -- every PR environment included -- share one namespace, so
the per-deployment metrics blocks never add up to the project's real footprint. This
card sums across the project's namespaces, so a project with 18 PRs shows one honest
number. Memory is the working set (what is resident), not the limit.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from opi.web import router

_TEMPLATES = Path(__file__).resolve().parents[1] / "opi" / "templates" / "project-details"


def test_it_is_a_single_lazy_request() -> None:
    """One request, not one per deployment -- the lesson from the backup OOM."""
    section = (_TEMPLATES / "section-resource-usage.html.j2").read_text()
    assert section.count('hx-trigger="load"') == 1
    assert 'hx-get="/projects/details/{{ project.name }}/resource-usage"' in section


def test_it_is_the_first_card_on_the_project_tab() -> None:
    page = (Path(__file__).resolve().parents[1] / "opi" / "templates" / "project-details.html.j2").read_text()
    tab = page.split('id="tab-project"', 1)[1]
    resource_at = tab.index("section-resource-usage")
    actions_at = tab.index("section-actions")
    assert resource_at < actions_at, "resource usage must come before the rest of the project tab"


def test_it_sums_across_namespaces_not_per_deployment() -> None:
    source = inspect.getsource(router.project_resource_usage_fragment)
    # Aggregated with a namespace regex, the same shape the dashboard uses.
    assert 'namespace=~"{ns_regex}"' in source
    assert "sum(" in source


def test_memory_is_the_working_set_not_the_limit() -> None:
    """'wat echt in gebruik is' -- resident memory, not the configured limit."""
    source = inspect.getsource(router.project_resource_usage_fragment)
    assert "container_memory_working_set_bytes" in source


def test_a_prometheus_failure_is_shown_not_swallowed() -> None:
    fragment = (_TEMPLATES / "_resource-usage.html.j2").read_text()
    assert "usage_error" in fragment
    source = inspect.getsource(router.project_resource_usage_fragment)
    assert "usage_error" in source


def test_the_fragment_checks_authorization() -> None:
    """A separate entry point needs its own check, not the page's."""
    source = inspect.getsource(router.project_resource_usage_fragment)
    assert "is_user_authorized_for_project" in source
