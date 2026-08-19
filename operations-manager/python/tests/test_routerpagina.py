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

from opi.core.dns_config import MANAGED_DNS_ZONES, ROUTER_HOSTNAMES, ROUTER_IPV4, ROUTER_IPV6

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def test_elke_beheerde_zone_heeft_een_routernaam() -> None:
    """Afgeleid uit de zones, zodat een nieuwe zone niet vergeten wordt."""
    assert ROUTER_HOSTNAMES == {f"router.{zone}" for zone in MANAGED_DNS_ZONES}
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
