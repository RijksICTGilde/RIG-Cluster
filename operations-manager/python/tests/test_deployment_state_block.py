"""The state a service reports is shown on the deployment (RC-28 step 4).

A user looking at a sleeping deployment saw nothing running and no reason why. What is
rendered is exactly the facts the services report -- the same contribution the health
check weighs, only rendered instead of judged -- so a second service that parks a
deployment in a situation gets it for free and no service name appears in the template.

WAAR DIT MEET, EN WAAROM DAT VERANDERD IS (RC-97). Deze test rendeerde
``project-details/section-deployment-state.html.j2``: een apart blok onder de kaart, op
een pagina die niet meer bestaat. Het herontwerp zette de dienstenberichten IN de kaart
(``bg/_argocd-deployment-card.html.j2``), en dat is wat de route rendert. De drie
uitspraken zijn hier ongewijzigd; alleen de plek waar ze gemeten worden klopt weer.

Wat wel wegvalt: het oude blok droeg ``data-deployment=`` zodat de deploymentwisselaar
het kon tonen en verbergen. De kaart is zelf al per deployment, dus dat is geen verlies
maar een gevolg van het herontwerp.
"""

from __future__ import annotations

from opi.core.templates_lotc import templates_lotc as templates
from opi.services.catalog.sleep_mode.state import STATE_SLEEPING, STATE_WAKING, SleepState, write
from opi.services.deployment_state import collect_deployment_state
from opi.services.services import ServiceAdapter

TEMPLATE = "bg/_argocd-deployment-card.html.j2"
CLUSTER = "odcn-production"


def _project(sleep_state: str | None = None) -> dict:
    project_data: dict = {
        "name": "productie",
        "services": ["sleep-mode"],
        "components": [{"name": "frontend"}],
        "deployments": [{"name": "productie", "cluster": CLUSTER, "namespace": "productie"}],
    }
    if sleep_state is not None:
        write(project_data, "productie", SleepState(state=sleep_state))
    return project_data


def _render(project_data: dict) -> str:
    deployment = project_data["deployments"][0]
    return templates.env.get_template(TEMPLATE).render(
        deployment=deployment,
        project={"name": project_data["name"]},
        argocd_status={deployment["name"]: {"health": "Healthy", "sync": "Synced", "errors": []}},
        current_cluster=CLUSTER,
        deployment_states={deployment["name"]: collect_deployment_state(project_data, deployment["name"])},
        ServiceAdapter=ServiceAdapter,
    )


def test_a_sleeping_deployment_says_so_on_the_page() -> None:
    html = _render(_project(STATE_SLEEPING))

    assert "slaapt" in html
    assert "Slaapstand" in html, "the card names the service that reports the fact"


def test_a_waking_deployment_says_so_too() -> None:
    assert "gewekt" in _render(_project(STATE_WAKING))


def test_nothing_is_rendered_for_a_deployment_no_service_reports_on() -> None:
    """An awake deployment gets no service message at all -- an empty line would be noise
    on every project page."""
    html = _render(_project())

    assert "slaapt" not in html
    assert "gewekt" not in html
    assert "Slaapstand" not in html
