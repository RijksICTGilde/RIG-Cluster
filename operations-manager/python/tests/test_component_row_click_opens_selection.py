"""Een klik op een component in de deploymentkaart opent de modal op een selectie (RC-378).

``_section-deployments.html.j2`` zette tot deze wijziging alleen de deployment-modal open,
zonder te zeggen om welk component het ging; de klik opende de hele lijst. Dit pint dat de
JS-aanroep nu ``{components: '<reference>'}`` meegeeft, zodat de modal alleen dat component
toont (``opi.forms.wizard.selection``), en dat de knop voor het ANDERE component in
dezelfde deployment zijn eigen reference meegeeft en niet die van de eerste.
"""

from __future__ import annotations

import re

from opi.core.templates_lotc import templates_lotc as templates

PROJECT = {
    "name": "proef",
    "deployments": [
        {
            "name": "d1",
            "cluster": "sandboxed-local",
            "components": [
                {"reference": "alpha", "image": "img/alpha:1"},
                {"reference": "beta", "image": "img/beta:1"},
            ],
        }
    ],
}


def _render(user_role: str = "admin") -> str:
    return templates.env.get_template("bg/_section-deployments.html.j2").render(
        {"project": PROJECT, "user_role": user_role}
    )


def _click_call(html: str, reference: str) -> str:
    """De ``onclick``-aanroep van het component, met HTML-entities teruggezet naar quotes."""
    match = re.search(rf'onclick="(openEditModal\([^"]*?components: &#39;{reference}&#39;\}}\))"', html)
    assert match, f"geen klik-aanroep gevonden voor component '{reference}'"
    return match.group(1).replace("&#39;", "'")


def test_klik_geeft_alleen_dat_component_mee() -> None:
    html = _render()

    alpha_call = _click_call(html, "alpha")
    assert "{components: 'alpha'}" in alpha_call
    assert "modal-edit-deployment-0" in alpha_call


def test_het_andere_component_in_dezelfde_deployment_krijgt_zijn_eigen_naam() -> None:
    html = _render()

    beta_call = _click_call(html, "beta")
    assert "{components: 'beta'}" in beta_call
    assert "'alpha'" not in beta_call


def test_zonder_bewerkrol_is_de_rij_niet_klikbaar() -> None:
    html = _render(user_role="member")

    assert "openEditModal(" not in html
