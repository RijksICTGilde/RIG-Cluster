"""De kaart noemt een component dat zijn image niet kan ophalen, zonder ``disabled``.

Dit is de tegenprestatie voor het weghalen van de auto-disable (RC-243). Daarvoor stond
zo'n component in de lijst "uitgeschakeld" met zijn reden, en die bron valt weg: er wordt
niets meer op nul gezet. Zonder dit blok ruilen we een storing die zichzelf verbergt in
voor een pod die stilletjes blijft hangen, en dat is geen verbetering.

Maatstaf zijn de drie gevallen die op 5 oktober 2026 op productie stonden en die straks
hangende pods zijn in plaats van een schone nul:

    rig-prd-ai-3rt    api-frontend        image zonder tag
    rig-prd-toets-hn7 pr-22-frontend      tag bestaat niet meer
    rig-prd-tva-d62   productie-frontend  image ``github.io/test/niet``
"""

from __future__ import annotations

from opi.core.templates_lotc import templates_lotc as templates
from opi.handlers.project_file_handler import IMAGE_PULL_ABSENT, IMAGE_PULL_CAPACITY, IMAGE_PULL_UNDIAGNOSED
from opi.services.deployment_diagnostics import ComponentImagePullFailure
from opi.services.deployment_state import collect_deployment_state

TEMPLATE = "bg/_argocd-deployment-card.html.j2"
CLUSTER = "odcn-production"

QUOTA_WOORDEN = "denied: Quota has been exceeded on namespace"


def _project(*, disabled: bool = False) -> dict:
    return {
        "name": "toets-hn7",
        "components": [{"name": "frontend"}],
        "deployments": [
            {
                "name": "pr-22",
                "cluster": CLUSTER,
                "namespace": "toets-hn7",
                "components": [
                    {
                        "reference": "frontend",
                        "image": "ghcr.io/minbzk/toetsingskader-archiefwet:pr-22",
                        **({"disabled": True, "disabled-reason": "OOMKilled"} if disabled else {}),
                    }
                ],
            }
        ],
    }


def _render(project_data: dict, failures: list[ComponentImagePullFailure], *, health: str = "Degraded") -> str:
    deployment = project_data["deployments"][0]
    return templates.env.get_template(TEMPLATE).render(
        deployment=deployment,
        project={"name": project_data["name"]},
        argocd_status={
            deployment["name"]: {
                "health": health,
                "sync": "Synced",
                "errors": [],
                "image_pull_failures": failures,
            }
        },
        current_cluster=CLUSTER,
        deployment_states={deployment["name"]: collect_deployment_state(project_data, deployment["name"])},
    )


def _failure(
    *,
    reference: str = "frontend",
    image: str | None = "ghcr.io/minbzk/toetsingskader-archiefwet:pr-22",
    message: str = "ImagePullBackOff: manifest unknown",
    reason_class: str = IMAGE_PULL_ABSENT,
    since: str = "2026-10-03T08:00:00Z",
) -> ComponentImagePullFailure:
    return ComponentImagePullFailure(
        reference=reference,
        image=image,
        registry_message=message,
        reason_class=reason_class,
        since=since,
    )


def test_het_component_de_image_en_sinds_wanneer_staan_er_alle_drie() -> None:
    html = _render(_project(), [_failure()])

    assert "frontend" in html
    assert "ghcr.io/minbzk/toetsingskader-archiefwet:pr-22" in html
    # Door dutch_date, want een ISO-tijdstempel is UTC en een kale slice staat er een dag
    # naast tussen 23:00 en 00:00 hier.
    assert "3 oktober 2026" in html
    assert "kan zijn image niet ophalen" in html


def test_de_melding_komt_zonder_dat_disabled_gezet_is() -> None:
    """De bron van de oude melding was ``comp.disabled``; die vlag staat hier NIET."""
    project_data = _project()
    assert "disabled" not in project_data["deployments"][0]["components"][0]

    html = _render(project_data, [_failure()])

    assert "kunnen hun image niet ophalen" in html or "kan zijn image niet ophalen" in html
    assert "uitgeschakeld" not in html


def test_blijft_aan_staan_is_te_onderscheiden_van_uitgeschakeld() -> None:
    """Twee toestanden naast elkaar, met een ander antwoord op "wat doe ik eraan".

    Een component dat OPI uitzette (OOM, crash loop) is weg tot er een uitrol komt; dit
    component staat aan en probeert het zelf opnieuw. Staan ze allebei op de kaart, dan
    moeten de twee meldingen niet door elkaar te halen zijn.
    """
    html = _render(_project(disabled=True), [_failure(reference="frontend")])

    # De oude melding over het uitgeschakelde component staat er nog.
    assert "uitgeschakeld" in html
    # En de nieuwe zegt expliciet het tegenovergestelde van "deploy opnieuw om te
    # heractiveren".
    assert "blijft aan staan" in html


def test_de_drie_klassen_zeggen_elk_wie_kan_handelen() -> None:
    absent = _render(_project(), [_failure(reason_class=IMAGE_PULL_ABSENT)])
    assert "de registry zegt dat de image er niet is" in absent
    assert "Push een geldige image" in absent

    capacity = _render(_project(), [_failure(reason_class=IMAGE_PULL_CAPACITY, message=QUOTA_WOORDEN)])
    assert "image-cache van het platform zit vol" in capacity
    assert "Hier hoef je zelf niets voor te doen" in capacity

    undiagnosed = _render(_project(), [_failure(reason_class=IMAGE_PULL_UNDIAGNOSED)])
    assert "de registry gaf geen antwoord" in undiagnosed
    assert "zegt niets over je image" in undiagnosed


def test_de_verbatim_tekst_van_de_registry_staat_er_volledig_in() -> None:
    """Afgekapt op 120 tekens zou een CRI-O-melding alleen zijn eigen aanloop tonen: op
    productie gemeten is zo'n bericht 762 tekens, omdat dezelfde fout er twee keer in
    staat. Dus niet afkappen, maar in een uitklapper."""
    lang = "ImagePullBackOff: " + QUOTA_WOORDEN + " " + ("x" * 400)

    html = _render(_project(), [_failure(reason_class=IMAGE_PULL_CAPACITY, message=lang)])

    assert lang in html
    assert "Wat de registry antwoordde" in html


def test_een_gezonde_kaart_zegt_hier_niets() -> None:
    html = _render(_project(), [], health="Healthy")

    assert "image niet ophalen" not in html


def test_een_component_zonder_image_in_de_verwijzing_valt_niet_om() -> None:
    """ai-3rt/api-frontend wijst naar een image ZONDER tag, en tva-d62 naar iets dat geen
    image is. Beide moeten gewoon te lezen zijn."""
    html = _render(
        _project(),
        [
            _failure(reference="api-frontend", image="par-dpia-form/experiment/backend", since=""),
            _failure(reference="productie-frontend", image="github.io/test/niet"),
        ],
    )

    assert "api-frontend" in html
    assert "par-dpia-form/experiment/backend" in html
    assert "github.io/test/niet" in html
    assert "2 componenten kunnen hun image niet ophalen" in html
