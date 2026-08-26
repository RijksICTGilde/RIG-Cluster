"""The deployment card no longer says "uitgeschakeld" and "Healthy" on one line (RC-31).

Step 2 of the plan, and the sharpest of the three displays: a component that is off gets
``replicas: 0``, ArgoCD calls zero replicas healthy, and the card printed the green Healthy
badge next to the red "Uitgeschakeld" chip. That contradiction cannot be resolved by
looking harder at it.

The replacement is deliberately not unconditional. A component that is switched off AND
broken must keep showing that it is broken -- the same asymmetry RC-28 built into the
health check.
"""

from __future__ import annotations

from opi.core.templates_lotc import templates_lotc as templates
from opi.services.deployment_state import collect_deployment_state

TEMPLATE = "bg/_argocd-deployment-card.html.j2"
CLUSTER = "odcn-production"


def _project(*disabled: bool) -> dict:
    return {
        "name": "productie",
        "components": [{"name": f"component-{i}"} for i in range(len(disabled))],
        "deployments": [
            {
                "name": "productie",
                "cluster": CLUSTER,
                "components": [
                    {"reference": f"component-{i}", "image": "ghcr.io/x/y:1", "disabled": flag}
                    for i, flag in enumerate(disabled)
                ],
            }
        ],
    }


def _render(project_data: dict, *, health: str = "Healthy", sync: str = "Synced") -> str:
    deployment = project_data["deployments"][0]
    return templates.env.get_template(TEMPLATE).render(
        deployment=deployment,
        project={"name": project_data["name"]},
        argocd_status={deployment["name"]: {"health": health, "sync": sync, "errors": []}},
        current_cluster=CLUSTER,
        deployment_states={deployment["name"]: collect_deployment_state(project_data, deployment["name"])},
    )


def test_a_switched_off_deployment_is_not_called_healthy() -> None:
    html = _render(_project(True, True))

    assert "Uitgeschakeld" in html
    assert "Healthy" not in html, "the green badge is the untruth this replaces"


def test_a_running_deployment_still_shows_its_health() -> None:
    html = _render(_project(False, False))

    assert "Healthy" in html
    assert "Uitgeschakeld" not in html


def test_a_real_failure_survives_being_switched_off() -> None:
    """The RC-28 rule, applied here: a state may explain absence, never excuse a
    problem. Off AND degraded shows both."""
    html = _render(_project(True), health="Degraded")

    assert "Uitgeschakeld" in html
    assert "Degraded" in html


def test_partly_off_keeps_the_health_badge_and_says_how_much_is_off() -> None:
    """One of four components off is a different situation than all four off: the rest is
    still serving traffic, so its health is still the thing to report."""
    html = _render(_project(True, False, False, False))

    assert "Healthy" in html
    assert "1 van 4 componenten uitgeschakeld" in html


def test_a_switched_off_deployment_on_another_cluster_still_says_so() -> None:
    """No ArgoCD badge row is rendered there, so the header chip has to carry it."""
    project_data = _project(True)
    project_data["deployments"][0]["cluster"] = "een-ander-cluster"

    assert "Uitgeschakeld" in _render(project_data)


def test_the_badge_is_not_printed_twice_on_one_card() -> None:
    """The header chip and the badge row would otherwise both carry it."""
    assert _render(_project(True)).count("Uitgeschakeld") == 1


def test_the_general_message_yields_to_the_component_listing() -> None:
    """Wat de kaart per component al toont, zegt het algemene dienstenbericht niet nog eens.

    De opsomming noemt de OORZAAK per component; het algemene bericht doet dat niet. Twee
    meldingen over dezelfde gebeurtenis, waarvan de vage bovenaan, is wat hier weg moet.
    """
    html = _render(_project(True, True))

    assert "Uitgeschakeld" in html, "de badge blijft, alleen de herhaalde zin gaat weg"
    assert "blijft uit tot iemand" not in html


def test_the_deduplication_survives_a_reworded_summary() -> None:
    """De ontdubbeling hangt aan een veld op het feit, niet aan de bewoording ervan.

    Hier stond een check op de woorden 'uitgeschakeld' en 'staat uit' in het sjabloon.
    Wie de zin in DeploymentHealthService herschreef, zette daarmee stil de ontdubbeling
    uit: geen fout, geen falende test, alleen dezelfde melding weer twee keer. Deze test
    herschrijft die zin tot iets dat geen van beide woorden bevat en houdt de kaart aan
    hetzelfde gedrag.
    """
    project_data = _project(True, True)
    deployment = project_data["deployments"][0]
    state = collect_deployment_state(project_data, deployment["name"])

    fact = next(f for f in state.facts if f.superseded_by_component_details)
    fact.summary = "Deze deployment is gedeactiveerd en wacht op iemand die hem terugzet."

    html = templates.env.get_template(TEMPLATE).render(
        deployment=deployment,
        project={"name": project_data["name"]},
        argocd_status={deployment["name"]: {"health": "Healthy", "sync": "Synced", "errors": []}},
        current_cluster=CLUSTER,
        deployment_states={deployment["name"]: state},
    )

    assert "gedeactiveerd" not in html, "de herformulering mag de ontdubbeling niet uitzetten"


def test_a_fact_that_is_not_a_repetition_is_shown() -> None:
    """Alleen dat ene bericht wijkt, niet alle. Slaapstand weet ArgoCD niets van, en die
    stil laten verdwijnen zou een echte toestand onzichtbaar maken."""
    from opi.services.catalog.sleep_mode.state import STATE_SLEEPING, SleepState, write

    project_data = _project(True, True)
    write(project_data, "productie", SleepState(state=STATE_SLEEPING))

    html = _render(project_data)

    assert "slaapt" in html
