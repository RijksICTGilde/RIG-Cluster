"""De pagina ``/security``: SECURITY.md als HTML, bereikbaar zonder inloggen."""

from typing import TYPE_CHECKING

from opi.web.security_policy import REPO_BLOB_URL, render_security_policy

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_security_is_bereikbaar_zonder_sessie(test_client: TestClient) -> None:
    """Wie een kwetsbaarheid meldt is meestal niet ingelogd."""
    response = test_client.get("/security")

    assert response.status_code == 200, response.text
    assert "Een kwetsbaarheid melden" in response.text


def test_de_route_draagt_geen_sso_eis() -> None:
    from opi.web.router import security_policy

    assert getattr(security_policy, "_requires_sso", False) is False


def test_de_voettekst_linkt_naar_security(test_client: TestClient) -> None:
    response = test_client.get("/introductie")

    assert 'href="/security"' in response.text


def test_een_repopad_wordt_een_github_link() -> None:
    html = render_security_policy("[actor](operations-manager/python/opi/core/actor.py)")

    assert f'href="{REPO_BLOB_URL}operations-manager/python/opi/core/actor.py"' in html


def test_absolute_links_en_ankers_blijven_staan() -> None:
    html = render_security_policy("[a](https://www.ncsc.nl/x) [b](#de-poort) [c](mailto:x@example.org)")

    assert 'href="https://www.ncsc.nl/x"' in html
    assert 'href="#de-poort"' in html
    assert 'href="mailto:x@example.org"' in html


def test_een_kop_krijgt_het_anker_van_github() -> None:
    """SECURITY.md linkt naar zijn eigen koppen, met het anker dat GitHub maakt."""
    html = render_security_policy("## De toeleverketen van de images")

    assert '<h2 id="de-toeleverketen-van-de-images">' in html


def test_ruwe_html_uit_de_bron_wordt_tekst() -> None:
    html = render_security_policy("<script>alert(1)</script>")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
