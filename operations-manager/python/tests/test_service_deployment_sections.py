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

import re
from typing import Any

import pytest
from opi.core.templates_lotc import templates_lotc as templates
from opi.services.catalog.base import DeploymentPageContext
from opi.services.registry import collect_deployment_page_sections
from opi.services.services_enums import ServiceType

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
        with the components (guards the loader wiring in opi/core/templates_lotc.py)."""
        section = collect_deployment_page_sections(_ctx([{"name": "metrics-scraper"}]))[0]
        html = templates.env.get_template(section.template).render(section=section)
        assert "/projects/details/proj/metrics/dep-1" in html
        assert "Resource Metrics" in html


BACKUPS_TEMPLATE = "shared/section-backups.html.j2"


class TestBackups:
    """Backups are not a service of their own: the block belongs to every service with a
    ``backup_label``. It used to render for any project with a deployment."""

    def test_section_for_a_project_that_can_back_something_up(self) -> None:
        section = collect_deployment_page_sections(_ctx(["postgresql-database"]))[0]
        assert section.template == BACKUPS_TEMPLATE
        assert section.context["available"] is True
        assert section.context["deployment_name"] == "dep-1"

    def test_no_section_for_a_project_with_nothing_to_back_up(self) -> None:
        assert _templates(_ctx(["publish-on-web", "redis"])) == []

    def test_every_backupable_service_is_an_owner(self) -> None:
        for service_name in ("persistent-storage", "postgresql-database", "minio-storage"):
            assert BACKUPS_TEMPLATE in _templates(_ctx([service_name])), service_name

    def test_several_owners_still_render_one_block(self) -> None:
        seen = _templates(_ctx(["persistent-storage", "postgresql-database", "minio-storage"]))
        assert seen.count(BACKUPS_TEMPLATE) == 1

    def test_no_section_for_a_deployment_on_another_cluster(self) -> None:
        assert _templates(_ctx(["postgresql-database"], cluster="other-cluster")) == []

    def test_only_the_first_deployment_carries_the_single_loader(self) -> None:
        """One request for the whole project: per-deployment loaders each open a Kopia
        repository, which OOM-killed the pod once."""
        ctx = _ctx(["postgresql-database"])
        ctx.project_data["deployments"] = [
            {"name": "b-dep", "cluster": "test-cluster", "namespace": "proj"},
            {"name": "a-dep", "cluster": "test-cluster", "namespace": "proj"},
        ]
        loaders = {}
        for deployment in ctx.project_data["deployments"]:
            ctx.deployment = deployment
            loaders[deployment["name"]] = collect_deployment_page_sections(ctx)[0].context["loads_snapshots"]
        assert loaders == {"a-dep": True, "b-dep": False}

    def test_unavailable_backend_gives_one_notice_not_one_per_deployment(self) -> None:
        ctx = _ctx(["postgresql-database"])
        ctx.backend_available["backups"] = False
        ctx.project_data["deployments"] = [
            {"name": "a-dep", "cluster": "test-cluster", "namespace": "proj"},
            {"name": "b-dep", "cluster": "test-cluster", "namespace": "proj"},
        ]
        ctx.deployment = ctx.project_data["deployments"][0]
        assert [s.context["available"] for s in collect_deployment_page_sections(ctx)] == [False]
        ctx.deployment = ctx.project_data["deployments"][1]
        assert collect_deployment_page_sections(ctx) == []

    def test_template_renders_through_the_app_env(self) -> None:
        ctx = _ctx(["postgresql-database"])
        ctx.deployment["backup"] = {"schedule": "FREQ=DAILY"}
        section = collect_deployment_page_sections(ctx)[0]
        html = templates.env.get_template(section.template).render(section=section)
        assert "/projects/details/proj/backups" in html
        assert "backups-snapshots-dep-1" in html
        assert "Backups" in html


SERVICE_ROUTES = (
    "/projects/details/{project_name}/backups",
    "/projects/{project_name}/db-console/{deployment_name}/modal",
    "/projects/{project_name}/jobs/{deployment_name}/modal",
)


def test_service_owned_routes_are_mounted_exactly_once() -> None:
    """Services that share a block hand back the SAME router object, so a shared route
    is registered once instead of once per owner."""
    from opi.web.router import web_router

    paths = [route.path for route in web_router.routes]
    for path in SERVICE_ROUTES:
        assert paths.count(path) == 1, path


#: Places in the general layer that still name a SYSTEM service, with the move not yet done.
#:
#: A USER service is chosen per project, so the general layer must never know it: that is
#: what this guard is for. A SYSTEM service is always there (ServiceKind.SYSTEM, "always
#: runs, never in the list"), so a generic component card showing "3 omgevingsvariabelen"
#: is not the same mistake. It is still debt though, and the plan has it moving into the
#: service, so the exemption is a named list rather than a blanket rule: a NEW occurrence
#: fails, and this list shrinks to nothing when step 3 of
#: plans/env-vars-en-aliassen-als-systeemdienst.md lands.
#: RC-97: de map project-details/ is weg (de pagina die hem rendeerde bestond niet meer).
#: Deze poort meet nu op bg/, waar dezelfde markup staat; de schuld verhuisde mee en werd
#: niet ingelost, dus de regels blijven staan onder hun nieuwe bestandsnaam.
_SYSTEM_SERVICE_DEBT = {
    ("project-tabs.html.j2", "user-env-vars"),
    ("_env-vars.html.j2", "user-env-vars"),
    ("_section-deployments.html.j2", "user-env-vars"),
}


def test_the_general_templates_name_no_service() -> None:
    """The point of the move: a service name in the general project-page layer is
    knowledge that belongs to the service, and it goes stale silently when the service
    changes. Comments are allowed to explain who owns what; code is not.

    System services are exempt only where _SYSTEM_SERVICE_DEBT says so, so the
    remaining spots stay visible and cannot quietly multiply."""
    from pathlib import Path

    import opi
    from opi.services.registry import SERVICES
    from opi.services.services_enums import ServiceKind

    def is_system(name: str) -> bool:
        service = SERVICES.get(ServiceType(name))
        definition = getattr(service, "definition", None)
        return getattr(definition, "kind", None) is ServiceKind.SYSTEM

    service_names = [service_type.value for service_type in ServiceType]
    offenders: list[str] = []
    for path in (Path(opi.__file__).parent / "templates_lotc" / "bg").glob("*.j2"):
        source = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.DOTALL)
        for name in service_names:
            if f"'{name}'" not in source and f'"{name}"' not in source:
                continue
            if is_system(name) and (path.name, name) in _SYSTEM_SERVICE_DEBT:
                continue
            offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_the_debt_list_has_no_stale_entries() -> None:
    """An entry that no longer occurs means the move happened; drop it, so the list keeps
    telling the truth about what is left."""
    from pathlib import Path

    import opi

    stale = []
    for template_name, service_name in _SYSTEM_SERVICE_DEBT:
        path = Path(opi.__file__).parent / "templates_lotc" / "bg" / template_name
        source = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.DOTALL) if path.exists() else ""
        if f"'{service_name}'" not in source and f'"{service_name}"' not in source:
            stale.append(f"{template_name}: {service_name}")
    assert stale == [], f"remove these from _SYSTEM_SERVICE_DEBT: {stale}"


class TestDatabaseModalActions:
    """The database console and the job runner are the PostgreSQL services' buttons.
    The page used to render them itself, one behind its own hardcoded service-name
    check and one unconditionally."""

    def _actions(self, services: list[Any]) -> list[Any]:
        from opi.services.registry import collect_deployment_actions

        return collect_deployment_actions({"name": "proj", "services": services}, "dep-1")

    def test_buttons_for_a_project_with_a_database(self) -> None:
        actions = self._actions(["postgresql-database"])
        assert [a.label for a in actions] == ["Databaseconsole", "Job uitvoeren"]
        assert actions[0].modal_endpoint == "/projects/proj/db-console/dep-1/modal"
        assert actions[1].modal_endpoint == "/projects/proj/jobs/dep-1/modal"
        assert all(a.modal_title for a in actions)

    def test_no_buttons_without_a_database(self) -> None:
        assert self._actions(["publish-on-web"]) == []

    def test_the_namespace_variant_is_an_owner_too(self) -> None:
        assert [a.label for a in self._actions(["namespace-postgresql-database"])] == [
            "Databaseconsole",
            "Job uitvoeren",
        ]

    def test_both_database_services_still_give_one_pair_of_buttons(self) -> None:
        labels = [a.label for a in self._actions(["postgresql-database", "namespace-postgresql-database"])]
        assert labels == ["Databaseconsole", "Job uitvoeren"]

    def test_an_action_cannot_be_both_a_post_and_a_modal(self) -> None:
        from opi.services.services import DeploymentAction

        with pytest.raises(ValueError, match="exactly one"):
            DeploymentAction(label="x", icon="i", kind="secondary", endpoint="/a", modal_endpoint="/b")
        with pytest.raises(ValueError, match="exactly one"):
            DeploymentAction(label="x", icon="i", kind="secondary")
