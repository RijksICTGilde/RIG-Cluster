"""WP2: services deliver their own project-details sections.

The read-only counterpart of ``config_form_section``. A service owns the presentation
of its own data on the detail page (``Service.detail_page_sections``); the view collects
these across the project's selected services (``collect_detail_page_sections``) instead
of the general template hardcoding a per-service include. Keycloak is the first mover:
its realm-admin block used to be a hardcoded include reading a context var the view set,
which drifted away from the config in RC-5 and silently stopped rendering.
"""

from __future__ import annotations

from opi.core.templates import templates
from opi.services.catalog.base import DetailPageSection
from opi.services.registry import collect_detail_page_sections

REALMS = [{"realm": "proj-realm", "host": "https://keycloak.example", "username": "admin", "password": "s3cret"}]


def _project(services: list, components: list | None = None) -> dict:
    return {"services": services, "components": components or []}


def test_keycloak_section_for_admin_with_realms() -> None:
    project = _project([{"name": "keycloak", "config": {"realms": REALMS}}])
    sections = collect_detail_page_sections(project, "admin")
    assert len(sections) == 1
    assert sections[0].template == "keycloak/section-detail.html.j2"
    assert sections[0].context["realms"] == REALMS


def test_keycloak_section_reads_all_three_entry_formats() -> None:
    """The realm data is found whether the entry is a record, a legacy single-key dict,
    or however the config carries it -- the reader is format-agnostic."""
    record = _project([{"name": "keycloak", "config": {"realms": REALMS}}])
    legacy = _project([{"keycloak": {"config": {"realms": REALMS}}}])
    for project in (record, legacy):
        sections = collect_detail_page_sections(project, "admin")
        assert [s.context["realms"] for s in sections] == [REALMS]


def test_no_section_for_non_privileged_role() -> None:
    project = _project([{"name": "keycloak", "config": {"realms": REALMS}}])
    assert collect_detail_page_sections(project, "developer") == []


def test_no_section_when_no_realms() -> None:
    project = _project([{"name": "keycloak", "config": {}}])
    assert collect_detail_page_sections(project, "admin") == []


def test_no_section_when_keycloak_not_selected() -> None:
    assert collect_detail_page_sections(_project(["publish-on-web"]), "admin") == []


def test_component_referenced_service_counts_as_selected() -> None:
    """A service selected only via a component reference still contributes its section."""
    project = _project(
        [{"name": "keycloak", "config": {"realms": REALMS}}],
        components=[{"name": "c1", "services": [{"reference": "keycloak"}]}],
    )
    assert len(collect_detail_page_sections(project, "admin")) == 1


def test_section_template_renders_through_the_app_env() -> None:
    """The service-owned template resolves via the catalog search path and renders with
    the ROOS components (guards the loader wiring in opi/core/templates.py)."""
    section = DetailPageSection(template="keycloak/section-detail.html.j2", context={"realms": REALMS})
    html = templates.env.get_template(section.template).render(section=section)
    assert "proj-realm" in html
    assert "Keycloak" in html
