"""WP2: services deliver their own project-details sections.

The read-only counterpart of ``config_form_section``. A service owns the presentation
of its own data on the detail page (a ``@on(UIEvent.PROJECT_SECTIONS)`` handler since
RC-39); the view collects these across the project's selected services
(``collect_detail_page_sections``) instead
of the general template hardcoding a per-service include. Keycloak is the first mover:
its realm-admin block used to be a hardcoded include reading a context var the view set,
which drifted away from the config in RC-5 and silently stopped rendering.
"""

from __future__ import annotations

from typing import ClassVar

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


def test_a_v1_component_service_list_counts_as_selected() -> None:
    """An unmigrated component carries its services under ``uses-services``. Reading only
    ``services`` loses them, and every block owned by such a service disappears without a
    trace -- which is how the backups block vanished from the detail-page E2E project."""
    from opi.services.registry import selected_services

    v1 = _project([], components=[{"name": "c1", "uses-services": ["persistent-storage"]}])
    v2 = _project([], components=[{"name": "c1", "services": [{"reference": "persistent-storage"}]}])
    names = [{s.service_type.value for s in selected_services(project)} for project in (v1, v2)]
    assert names == [{"persistent-storage"}, {"persistent-storage"}]


class TestAttachmentsSection:
    """RC-24: the Bijlagen block is the attachments service's, including whether it
    shows at all -- the general template used to carry that ``{% if %}`` itself."""

    CATALOG: ClassVar[list[dict]] = [{"id": "keystore", "filename": "keystore.p12", "content": "<age>"}]

    def _project(self) -> dict:
        return _project(
            [{"attachments": {"data": self.CATALOG}}],
            components=[
                {
                    "name": "c1",
                    "services": [{"reference": "attachments", "config": [{"reference": "keystore"}]}],
                }
            ],
        )

    def test_section_for_a_project_that_uses_attachments(self) -> None:
        sections = collect_detail_page_sections(self._project(), "admin")
        assert [s.template for s in sections] == ["attachments/section-detail.html.j2"]
        assert sections[0].context["attachments"][0]["filename"] == "keystore.p12"
        assert sections[0].context["can_edit"] is True

    def test_no_section_without_the_service(self) -> None:
        assert collect_detail_page_sections(_project(["publish-on-web"]), "admin") == []

    def test_section_shows_for_a_developer_without_the_edit_buttons(self) -> None:
        sections = collect_detail_page_sections(self._project(), "developer")
        assert len(sections) == 1
        assert sections[0].context["can_edit"] is False

    def test_section_shows_when_the_catalog_is_still_empty(self) -> None:
        """Selecting the service is what makes the block appear -- otherwise a user who
        just switched it on has nowhere to click "Toevoegen"."""
        sections = collect_detail_page_sections(_project([{"attachments": {"data": []}}]), "admin")
        assert [s.context["attachments"] for s in sections] == [[]]

    def test_template_renders_through_the_app_env(self) -> None:
        section = collect_detail_page_sections(self._project(), "admin")[0]
        html = templates.env.get_template(section.template).render(section=section)
        assert "keystore.p12" in html
        assert "Bijlagen" in html


def test_section_template_renders_through_the_app_env() -> None:
    """The service-owned template resolves via the catalog search path and renders with
    the ROOS components (guards the loader wiring in opi/core/templates.py)."""
    section = DetailPageSection(template="keycloak/section-detail.html.j2", context={"realms": REALMS})
    html = templates.env.get_template(section.template).render(section=section)
    assert "proj-realm" in html
    assert "Keycloak" in html
