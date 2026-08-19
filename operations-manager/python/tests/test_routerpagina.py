"""De routerpagina: wat `router.<zone>` toont, en waarom alleen daar.

`router.rijksapp.nl` en zijn broertjes zijn de kale ingang van het cluster: elke andere naam
die wij publiceren is een CNAME daarheen. Er draaide niets op, en daardoor kreeg wie de naam
over HTTPS opvroeg het wildcardcertificaat van de ODCN-zone terug, dat niet op deze naam
slaat. De internet.nl-toets zakte daarop, en de bezoeker kreeg geen antwoord op de enige
vraag die hij heeft: wat is dit en wat moet ik ermee.

Twee dingen kunnen hier stil kapotgaan:

1. **De pagina lekt naar het portaal.** De host bepaalt wat je krijgt. Raakt die voorwaarde
   los, dan krijgt elke ZAD-bezoeker de DNS-uitleg in plaats van zijn dashboard.
2. **De adressen verouderen.** Ze staan op de pagina omdat iemand ze overtypt in zijn eigen
   zone. Lopen ze uit de pas met wat er in TransIP staat, dan wijst iemand zijn domein naar
   een adres dat van ons was.
"""

from typing import TYPE_CHECKING

from opi.core.dns_config import (
    DEFAULT_ROUTER_HOSTNAME,
    MANAGED_DNS_ZONES,
    ROUTER_HOSTNAMES,
    ROUTER_IPV4,
    ROUTER_IPV6,
)
from opi.web.menu import get_menu_items
from opi.web.router import eigen_domein

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_elke_beheerde_zone_heeft_een_routernaam() -> None:
    """Afgeleid uit de zones, zodat een nieuwe zone niet vergeten wordt."""
    assert {f"router.{zone}" for zone in MANAGED_DNS_ZONES} == ROUTER_HOSTNAMES
    assert "router.rijksapp.nl" in ROUTER_HOSTNAMES


def test_de_routernaam_toont_de_dns_uitleg(test_client: TestClient) -> None:
    """Zonder sessie, op de routerhost: 200 met de uitleg, geen doorverwijzing."""
    response = test_client.get("/", headers={"host": "router.rijksapp.nl"}, follow_redirects=False)

    assert response.status_code == 200, response.text
    assert "router.rijksapp.nl" in response.text
    assert ROUTER_IPV4 in response.text
    assert ROUTER_IPV6 in response.text


def test_alle_drie_de_routernamen_werken(test_client: TestClient) -> None:
    """De pagina toont de naam waarop hij is opgevraagd, niet een vaste."""
    for host in sorted(ROUTER_HOSTNAMES):
        response = test_client.get("/", headers={"host": host}, follow_redirects=False)
        assert response.status_code == 200, f"{host}: {response.text}"
        assert host in response.text


def test_het_portaal_blijft_doorverwijzen(test_client: TestClient) -> None:
    """Op elke andere host verandert er niets: nog steeds naar de introductie.

    Dit is de kant die stil kan breken. Een te ruime hostvergelijking zou het hele portaal
    vervangen door een DNS-uitleg, en niemand die de routerpagina test merkt dat.
    """
    response = test_client.get("/", headers={"host": "zad.rijksapp.nl"}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/introductie"


def test_een_naam_die_op_de_routernaam_lijkt_krijgt_het_portaal(test_client: TestClient) -> None:
    """Geen prefix- of substringvergelijking: alleen de naam zelf telt."""
    for host in ("router.rijksapp.nl.example.com", "notrouter.rijksapp.nl", "router.rijksapp.n"):
        response = test_client.get("/", headers={"host": host}, follow_redirects=False)
        assert response.status_code == 302, f"{host} kreeg de routerpagina"


def test_de_getoonde_adressen_zijn_die_van_de_configuratie() -> None:
    """De pagina put uit dezelfde bron als de rest; geen tweede lijst om te verouderen."""
    assert ROUTER_IPV4 == "147.181.48.71"
    assert ROUTER_IPV6 == "2a04:9a00:1007:4000:0:2:0:8"


def test_de_uitleg_heeft_een_eigen_adres(test_client: TestClient) -> None:
    """Het menu staat op zad.<zone>, dus de pagina moet ook zonder de routernaam te halen zijn."""
    response = test_client.get("/eigen-domein", headers={"host": "zad.rijksapp.nl"})

    assert response.status_code == 200, response.text
    assert ROUTER_IPV4 in response.text


def test_het_eigen_adres_vraagt_geen_rechten() -> None:
    """Wie een domein aanwijst is vaak een DNS-beheerder van buiten, zonder account hier.

    Rechtstreeks op de functie: een 200 hierboven bewijst dit niet, want bij een ontbrekende
    testconfiguratie kan een route om een heel andere reden doorlaten.
    """
    assert not getattr(eigen_domein, "_requires_sso", False)


def test_de_genoemde_routernaam_volgt_de_zone(test_client: TestClient) -> None:
    """Wie op rijks.app kijkt heeft niets aan router.rijksapp.nl."""
    response = test_client.get("/eigen-domein", headers={"host": "zad.rijks.app"})

    assert response.status_code == 200
    assert "router.rijks.app" in response.text


def test_een_onbekende_host_krijgt_de_standaardnaam(test_client: TestClient) -> None:
    response = test_client.get("/eigen-domein", headers={"host": "localhost"})

    assert response.status_code == 200
    assert DEFAULT_ROUTER_HOSTNAME in response.text


def test_het_menu_wijst_naar_de_uitleg() -> None:
    """Onder de API-docs, zoals afgesproken."""
    links = [item["link"] for item in get_menu_items(None)]

    assert "/eigen-domein" in links
    assert links.index("/eigen-domein") == links.index("/docs") + 1
