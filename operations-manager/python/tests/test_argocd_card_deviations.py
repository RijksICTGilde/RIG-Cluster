"""De kaart legt uit waarom hij niet groen is (plans/status-uitleg-bij-afwijking.md).

Bij mb-docs-helmfile stond de kaart op OutOfSync/Progressing zonder enige verklaring,
terwijl alles draaide en alleen twee Jobs in verwijdering hingen. De afwijkingenlijst
maakt dat zichtbaar - en alleen dan: een gezonde kaart rendert byte-gelijk aan voorheen.
"""

from __future__ import annotations

from typing import Any

from opi.core.templates_lotc import templates_lotc as templates
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


def _zombie_jobs_status() -> dict[str, Any]:
    """Het mb-docs-geval: sync geslaagd, maar twee restanten wijken af."""
    return {
        "health": "Progressing",
        "sync": "OutOfSync",
        "operation_phase": "Succeeded",
        "last_sync": "2026-08-20T18:10:57Z",
        "errors": [],
        "deviations": [
            {
                "resource": "Job/docs-backend-createsuperuser-1786315497",
                "kind": "Job",
                "reason": "is verwijderd, maar het cluster maakt de verwijdering niet af",
            },
            {
                "resource": "Job/docs-backend-migrate-1786315497",
                "kind": "Job",
                "reason": "is verwijderd, maar het cluster maakt de verwijdering niet af",
            },
        ],
    }


class TestDeviationsOnTheCard:
    def test_zombie_jobs_are_explained(self) -> None:
        html = _render(_zombie_jobs_status())
        assert "Job/docs-backend-createsuperuser-1786315497: is verwijderd" in html
        assert "Job/docs-backend-migrate-1786315497: is verwijderd" in html

    def test_succeeded_sync_gets_the_connecting_line(self) -> None:
        """De verwarrende combinatie: "Sync OK" naast OutOfSync, nu met verbindende zin."""
        html = _render(_zombie_jobs_status())
        assert "Laatste sync geslaagd, maar 2 resources wijken nog af" in html

    def test_without_succeeded_sync_the_header_is_neutral(self) -> None:
        status = _zombie_jobs_status()
        status["operation_phase"] = "Running"
        html = _render(status)
        assert "Waarom niet groen" in html
        assert "Laatste sync geslaagd" not in html

    def test_more_than_five_deviations_are_summarised(self) -> None:
        status = _zombie_jobs_status()
        status["deviations"] = [{"resource": f"Job/rest-{i}", "kind": "Job", "reason": "nog bezig"} for i in range(7)]
        html = _render(status)
        assert "Job/rest-4: nog bezig" in html
        assert "Job/rest-5" not in html
        assert "en 2 meer" in html

    def test_green_card_renders_as_before(self) -> None:
        """Geen ruis: zonder afwijkingen is de output byte-gelijk aan een kaart van voor
        deze feature (statusdict zonder 'deviations'-sleutel)."""
        green = {"health": "Healthy", "sync": "Synced", "errors": []}
        assert _render(green) == _render({**green, "deviations": []})
        assert "Waarom niet groen" not in _render(green)
