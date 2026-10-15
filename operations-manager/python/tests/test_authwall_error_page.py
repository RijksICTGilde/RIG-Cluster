"""De authorization wall levert een eigen foutpagina mee.

Zonder ``error.html`` in de templates-configmap valt oauth2-proxy terug op zijn
ingebouwde pagina: Engelstalig, met een grote statuscode en een generieke
"Oops! Something went wrong". Een afgewezen callback (bijvoorbeeld een token
waarvan ``email_verified`` niet true is) komt daar als kale 500 uit.

oauth2-proxy geeft de reden niet aan het template: de callback roept
``ErrorPage(rw, req, 500, err.Error())`` zonder ``Messages``, en die AppError
gebruikt het alleen in debug-mode. Het template moet het dus met de statuscode
doen, en mag geen reden beweren die het niet kent.
"""

import io

from opi.generation.manifests import render_template
from ruamel.yaml import YAML


def _configmap_data() -> dict[str, str]:
    rendered = render_template(
        "sidecar-authorization-wall.yaml.jinja",
        {
            "section": "configmap",
            "name": "productie-app",
            "namespace": "rig-prd-demo",
            "project": {"name": "demo"},
            "extra_labels": {},
            "authorization_wall": {
                "issuer_url": "https://keycloak.example.com/realms/demo",
                "client_id": "app",
                "keycloak_secret_name": "productie-keycloak",
                "cookie_secret_name": "productie-cookie",
            },
            "application_port": 8080,
            "hostname": "app.example.com",
        },
    )
    return YAML(typ="safe").load(io.StringIO(rendered))["data"]


def test_configmap_levert_beide_templates() -> None:
    """oauth2-proxy overschrijft per bestand, dus sign_in.html moet blijven."""
    data = _configmap_data()
    assert sorted(data) == ["error.html", "sign_in.html"]


def test_error_page_heeft_een_tak_per_statuscode() -> None:
    page = _configmap_data()["error.html"]
    # De Jinja-escaping moet kale Go-tags opleveren, geen {{"{{"}}-resten.
    assert '{{"' not in page
    for branch in (
        "{{ if eq .StatusCode 403 }}",
        "{{ else if eq .StatusCode 500 }}",
        "{{ else if eq .StatusCode 502 }}",
        "{{ else }}",
        "{{ end }}",
    ):
        assert branch in page, f"tak ontbreekt: {branch}"


def test_error_page_is_nederlands_en_noemt_geen_onbekende_reden() -> None:
    page = _configmap_data()["error.html"]
    assert "Inloggen is niet gelukt" in page
    assert "Je hebt geen toegang tot deze website" in page
    # .Message is in productie de Engelse standaardtekst van oauth2-proxy.
    assert ".Message" not in page
    assert "Oops" not in page
    # De 500 mag de oorzaak niet als feit brengen: hij dekt ook een verlopen code.
    assert "omdat je e-mailadres" not in page


def test_error_page_geeft_de_gebruiker_een_weg_terug_en_een_code() -> None:
    page = _configmap_data()["error.html"]
    assert "{{ if .Redirect }}" in page
    assert "{{ .ProxyPrefix }}/sign_in" in page
    assert "Opnieuw inloggen" in page
    # Het access-logregel draagt dezelfde id, dus dit is de brug naar de logs.
    assert "{{ if .RequestID }}" in page
    assert "{{ .RequestID }}" in page
