"""The state a service reports is shown on the deployment (RC-28 step 4).

A user looking at a sleeping deployment saw nothing running and no reason why. The block
renders exactly the facts the services report -- the same contribution the health check
weighs, only rendered instead of judged -- so a second service that parks a deployment in
a situation gets the block for free and no service name appears in the template.
"""

from __future__ import annotations

from opi.core.templates_lotc import templates_lotc as templates
from opi.services.catalog.sleep_mode.state import STATE_SLEEPING, STATE_WAKING, SleepState, write
from opi.services.deployment_state import collect_deployment_state
from opi.services.services import ServiceAdapter

TEMPLATE = "project-details/section-deployment-state.html.j2"


def _project(sleep_state: str | None = None) -> dict:
    project_data: dict = {
        "name": "productie",
        "services": ["sleep-mode"],
        "components": [{"name": "frontend"}],
        "deployments": [{"name": "productie", "cluster": "odcn-production", "namespace": "productie"}],
    }
    if sleep_state is not None:
        write(project_data, "productie", SleepState(state=sleep_state))
    return project_data


def _render(project_data: dict) -> str:
    facts = {
        deployment["name"]: collect_deployment_state(project_data, deployment["name"]).facts
        for deployment in project_data["deployments"]
    }
    return templates.env.get_template(TEMPLATE).render(
        project={"deployments": project_data["deployments"]},
        deployment_state_facts=facts,
        ServiceAdapter=ServiceAdapter,
    )


def test_a_sleeping_deployment_says_so_on_the_page() -> None:
    html = _render(_project(STATE_SLEEPING))

    assert "slaapt" in html
    assert "Slaapstand" in html, "the block names the service that reports the fact"
    assert 'data-deployment="productie"' in html, "the block must follow the deployment switcher"


def test_a_waking_deployment_says_so_too() -> None:
    assert "gewekt" in _render(_project(STATE_WAKING))


def test_nothing_is_rendered_for_a_deployment_no_service_reports_on() -> None:
    """An awake deployment gets no block at all -- an empty card would be noise on every
    project page."""
    assert _render(_project()).strip() == ""
