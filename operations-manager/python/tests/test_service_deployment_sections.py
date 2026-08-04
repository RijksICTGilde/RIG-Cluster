"""RC-24: services deliver their own PER-DEPLOYMENT project-details blocks.

``detail_page_sections`` covers the project level; backups, metrics and the deployment
action buttons describe ONE deployment, so they get their own hook
(``Service.deployment_page_sections`` / ``registry.collect_deployment_page_sections``).

Every block moved here gets the same two guards, because the failure mode is silent: a
block that stops rendering looks exactly like a project that does not use the service.
So each service asserts BOTH that its section appears for a project that uses it and
that it stays away from a project that does not.
"""

from __future__ import annotations

from typing import Any

from opi.core.templates import templates
from opi.services.catalog.base import DeploymentPageContext
from opi.services.registry import collect_deployment_page_sections

DEPLOYMENT = {"name": "dep-1", "cluster": "test-cluster", "namespace": "proj"}


def _ctx(
    services: list[Any],
    *,
    components: list[dict[str, Any]] | None = None,
    prometheus: bool = True,
    cluster: str = "test-cluster",
    user_role: str = "admin",
) -> DeploymentPageContext:
    return DeploymentPageContext(
        project_data={
            "name": "proj",
            "services": services,
            "components": components or [],
            "deployments": [DEPLOYMENT],
        },
        deployment=dict(DEPLOYMENT, cluster=cluster),
        user_role=user_role,
        current_cluster="test-cluster",
        backend_available={"prometheus": prometheus, "backups": True},
    )


def _templates(ctx: DeploymentPageContext) -> list[str]:
    return [section.template for section in collect_deployment_page_sections(ctx)]


class TestMetricsScraper:
    """The Resource Metrics block is the metrics-scraper service's, not the page's."""

    def test_section_for_a_project_that_scrapes_metrics(self) -> None:
        ctx = _ctx([], components=[{"name": "c1", "services": [{"reference": "metrics-scraper"}]}])
        sections = collect_deployment_page_sections(ctx)
        assert [s.template for s in sections] == ["metrics_scraper/section-deployment.html.j2"]
        assert sections[0].context["available"] is True
        assert sections[0].context["deployment_name"] == "dep-1"

    def test_no_section_without_the_service(self) -> None:
        assert _templates(_ctx(["publish-on-web"])) == []

    def test_unavailable_when_prometheus_is_down(self) -> None:
        ctx = _ctx([{"name": "metrics-scraper"}], prometheus=False)
        section = collect_deployment_page_sections(ctx)[0]
        assert section.context["available"] is False
        assert "Prometheus" in section.context["unavailable_reason"]

    def test_unavailable_for_a_deployment_on_another_cluster(self) -> None:
        ctx = _ctx([{"name": "metrics-scraper"}], cluster="other-cluster")
        section = collect_deployment_page_sections(ctx)[0]
        assert section.context["available"] is False
        assert "test-cluster" in section.context["unavailable_reason"]

    def test_template_renders_through_the_app_env(self) -> None:
        """The service-owned template resolves via the catalog search path and renders
        with the ROOS components (guards the loader wiring in opi/core/templates.py)."""
        section = collect_deployment_page_sections(_ctx([{"name": "metrics-scraper"}]))[0]
        html = templates.env.get_template(section.template).render(section=section)
        assert "/projects/details/proj/metrics/dep-1" in html
        assert "Resource Metrics" in html


def test_same_template_from_several_owners_renders_once() -> None:
    """A block owned jointly (backups: every backupable service) renders once."""
    ctx = _ctx(["persistent-storage", "postgresql-database", "minio-storage"])
    templates_seen = _templates(ctx)
    assert len(templates_seen) == len(set(templates_seen))
