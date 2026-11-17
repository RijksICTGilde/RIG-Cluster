"""The card shows what ArgoCD does NOT roll out, even when the rest is green (RC-501).

A resource kind that is not on the allowlist lets ArgoCD keep the app Synced and Healthy
while that object is missing (features/argocd-resource-allowlist.md). Without this
warning a tenant would never see that.
"""

from __future__ import annotations

from typing import Any

from opi.http.render.templates_lotc import templates_lotc as templates
from opi.services.deployment_state import collect_deployment_state
from opi.services.services import ServiceAdapter

TEMPLATE = "bg/_argocd-deployment-card.html.j2"
CLUSTER = "odcn-production"


def _project() -> dict:
    return {
        "name": "productie",
        "components": [{"name": "web"}],
        "deployments": [
            {
                "name": "productie",
                "cluster": CLUSTER,
                "components": [{"reference": "web", "image": "ghcr.io/x/y:1"}],
            }
        ],
    }


def _render(status: dict[str, Any]) -> str:
    project_data = _project()
    deployment = project_data["deployments"][0]
    return templates.env.get_template(TEMPLATE).render(
        deployment=deployment,
        project={"name": project_data["name"]},
        argocd_status={deployment["name"]: status},
        current_cluster=CLUSTER,
        deployment_states={deployment["name"]: collect_deployment_state(project_data, deployment["name"])},
        ServiceAdapter=ServiceAdapter,
    )


class _ExcludedResource:
    """What deployment_diagnostics.ExcludedResource hands the template, nothing more."""

    def __init__(self, message: str) -> None:
        self.message = message


def _healthy_status(excluded: list[_ExcludedResource] | None = None) -> dict[str, Any]:
    return {
        "health": "Healthy",
        "sync": "Synced",
        "errors": [],
        "excluded_resources": excluded or [],
    }


class TestExcludedResourcesOnTheCard:
    def test_a_healthy_card_shows_the_warning(self) -> None:
        html = _render(
            _healthy_status([_ExcludedResource("HTTPRoute web wordt niet uitgerold: dit type staat niet op de lijst.")])
        )
        assert "1 resource wordt niet uitgerold" in html
        assert "HTTPRoute web wordt niet uitgerold" in html

    def test_it_is_a_warning_alert_not_an_error(self) -> None:
        html = _render(
            _healthy_status([_ExcludedResource("HTTPRoute web wordt niet uitgerold: dit type staat niet op de lijst.")])
        )
        assert 'variant="warning"' in html

    def test_multiple_excluded_resources_get_the_plural_heading(self) -> None:
        html = _render(
            _healthy_status(
                [
                    _ExcludedResource("HTTPRoute web wordt niet uitgerold: dit type staat niet op de lijst."),
                    _ExcludedResource("ConfigMap extra wordt niet uitgerold: dit type staat niet op de lijst."),
                ]
            )
        )
        assert "2 resources worden niet uitgerold" in html
        assert "HTTPRoute web wordt niet uitgerold" in html
        assert "ConfigMap extra wordt niet uitgerold" in html

    def test_a_card_without_excluded_resources_renders_as_before(self) -> None:
        """No noise: without the key the output is byte-identical to an empty list, and
        the warning does not appear."""
        green = {"health": "Healthy", "sync": "Synced", "errors": []}
        assert _render(green) == _render({**green, "excluded_resources": []})
        assert "wordt niet uitgerold" not in _render(green)
        assert "worden niet uitgerold" not in _render(green)
