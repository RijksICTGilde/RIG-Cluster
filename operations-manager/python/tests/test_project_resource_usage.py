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

_TEMPLATES = Path(__file__).resolve().parents[1] / "opi" / "templates_lotc" / "bg"


def test_it_is_a_single_lazy_request() -> None:
    """One request, not one per deployment -- the lesson from the backup OOM.

    Het losse ``section-resource-usage.html.j2`` bestaat niet meer: op de nieuwe pagina
    staat het blok in ``bg/project-tabs.html.j2`` zelf. De uitspraak blijft dezelfde --
    er gaat EEN verzoek naar het meetfragment, niet een per deployment.
    """
    tab = (_TEMPLATES / "project-tabs.html.j2").read_text()
    assert tab.count('hx-get="/projects/details/{{ project.name }}/resource-usage"') == 1
    at = tab.index('hx-get="/projects/details/{{ project.name }}/resource-usage"')
    assert 'hx-trigger="load"' in tab[at : at + 200]


def test_it_sits_high_on_the_project_tab_under_the_actions() -> None:
    """Tussen Acties en Deployments op het tabblad Project.

    De oorspronkelijke uitspraak was "onder de acties, boven het team". De helft over het
    team is op de nieuwe pagina niet meer af te lezen: Team & Toegang is een eigen tabblad
    geworden en staat niet meer op dit tabblad. Die helft is als vervallen uitspraak
    genoteerd in docs/opruiming-inventarisatie-rc97.md (bak 3); wat hier overblijft is de
    plaatsing die de pagina zelf nog draagt.
    """
    tab = (_TEMPLATES / "project-tabs.html.j2").read_text()
    resource_at = tab.index('call panel("Resourcegebruik (heel project)"')
    assert tab.index('call panel("Acties"') < resource_at, "actions stay on top"
    assert resource_at < tab.index('call panel("Deployments"'), "resource usage above the deployments"


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
    fragment = (_TEMPLATES / "_resource-usage.html.j2").read_text()  # bg/, de kaart die de route rendert
    assert "usage_error" in fragment
    source = inspect.getsource(router.project_resource_usage_fragment)
    assert "usage_error" in source


def test_the_fragment_checks_authorization() -> None:
    """A separate entry point needs its own check, not the page's."""
    source = inspect.getsource(router.project_resource_usage_fragment)
    assert "is_user_authorized_for_project" in source
