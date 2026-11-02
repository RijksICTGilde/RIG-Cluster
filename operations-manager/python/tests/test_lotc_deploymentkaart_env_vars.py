"""Het omgevingsvariabelen-blok op de deploymentkaart (RC-388).

Net als de componentkaart toont ook deze kaart geen waarde, naam of aantal: de
overschrijvingen van een deployment kunnen evengoed geheimen zijn. De knop
"Bekijken en bewerken" opent het step-up-venster en is daarom de enige ingang; hij hangt
af van env-vars.manage en wijst naar de deployment-variant van de venster-route.

De componentkaart-kant dekt ``tests/test_lotc_componentkaart_waarden.py``; de route zelf
``tests/test_router_env_vars_web.py``. Hier staat de koppeling ertussen: het sjabloon
bouwt de juiste deployment-URL en houdt elke waarde buiten de HTML.
"""

from __future__ import annotations

from opi.core.templates_lotc import templates_lotc as templates

PROJECT = {
    "name": "proef",
    "deployments": [
        {
            "name": "prod",
            "cluster": "sandboxed-local",
            "components": [
                {"reference": "web", "image": "img/web:1", "user-env-var-namen": ["API_TOKEN", "VLG"]},
                {"reference": "worker", "image": "img/worker:1"},
            ],
        }
    ],
}


def _render(mag: bool = True) -> str:
    return templates.env.get_template("bg/_section-deployments.html.j2").render(
        {"project": PROJECT, "can": lambda *_a, **_k: mag}
    )


def _env_blok(html: str) -> str:
    return html.split("Omgevingsvariabelen", 1)[1]


def test_geen_naam_geen_teller_en_nooit_een_waarde() -> None:
    blok = _env_blok(_render())

    assert "API_TOKEN" not in blok
    assert "VLG" not in blok
    assert "overschrijving" not in blok
    assert "c-secret-field" not in blok
    assert "value=" not in blok, "een waarde-attribuut hoort niet in dit blok"


def test_de_knop_verwijst_naar_het_venster_van_deze_deployment() -> None:
    blok = _env_blok(_render())

    assert 'hx-get="/projects/proef/deployments/prod/componenten/web/env-vars"' in blok


def test_de_knop_staat_ook_bij_een_component_zonder_overschrijving() -> None:
    """Het venster is sinds RC-388 ook de enige weg om er een overschrijving te maken."""
    blok = _env_blok(_render())

    assert 'hx-get="/projects/proef/deployments/prod/componenten/worker/env-vars"' in blok


def test_zonder_bewerkrol_is_er_geen_knop() -> None:
    assert "Bekijken en bewerken" not in _render(mag=False)
