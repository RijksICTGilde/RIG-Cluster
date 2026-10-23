"""Het client-IP achter de router (RC-368).

De OpenShift-router (HAProxy, ``forwardedHeaderPolicy: Append``) zet zijn waarde als
APARTE tweede ``X-Forwarded-For``-regel achter die van de client. Gemeten in rig-prd-test,
zie features/futures/client-ip-vertrouwen-achter-de-router.md. Wat hier gemeten wordt:

1. Twee headerregels zoals de router ze stuurt, met een gespoofte clientregel: de rem telt
   op de waarde die de router heeft toegevoegd, en niet op die van de aanvaller.
2. Een komma-lijst met een gespoofd adres links: idem de rechtse waarde bij hops=1.
3. Geen header: het peer-adres, ook als ``ProxyHeadersMiddleware`` ``scope["client"]``
   al heeft overschreven.
4. ``TRUSTED_PROXY_HOPS=0`` negeert elke header.
5. Door de echte middlewarestapel: een stortvloed foute sleutels met een wisselend
   gespoofd adres loopt vast op de emmer per IP, terwijl een geldige sleutel vanaf een
   ander echt adres blijft werken.
"""

import itertools
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from opi.api import rate_limiter
from opi.api.endpoint_util import validate_api_token
from opi.api.rate_limiter import IPRateLimiter
from opi.core.config import settings
from opi.middleware.client_ip import PeerAddressMiddleware
from opi.middleware.rate_limit import RateLimitMiddleware
from opi.utils.client_ip import PEER_SCOPE_KEY, client_ip, forwarded_chain
from starlette.requests import Request
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Het adres dat de router zelf toevoegt: het echte adres van de aanroeper.
ROUTER_WAARDE = "147.181.15.230"

#: Wat een aanvaller in zijn eigen headerregel zet.
GESPOOFT = "198.51.100.77"


@contextmanager
def _hops(aantal: int) -> Iterator[None]:
    with patch.object(settings, "TRUSTED_PROXY_HOPS", aantal):
        yield


def _request(regels: list[str], peer: str = "10.0.0.1", gespoofde_peer: str | None = None) -> Request:
    """Een Request met de ruwe headerregels zoals ze op de draad aankomen.

    ``gespoofde_peer`` zet ``scope["client"]`` op een andere waarde dan het peer-adres in
    de scope, zoals ``ProxyHeadersMiddleware`` dat doet.
    """
    headers = [(b"x-forwarded-for", regel.encode()) for regel in regels]
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/ping",
        "query_string": b"",
        "root_path": "",
        "server": ("testserver", 80),
        "client": (gespoofde_peer or peer, 1234),
        "headers": headers,
        PEER_SCOPE_KEY: peer,
    }
    return Request(scope)


class TestDeKetenLezen:
    def test_twee_headerregels_zoals_de_router_ze_stuurt(self):
        """Toets 1: de clientregel staat vooraan, de router-waarde erachter."""
        request = _request([GESPOOFT, ROUTER_WAARDE])

        assert forwarded_chain(request) == [GESPOOFT, ROUTER_WAARDE]
        assert request.headers.get("x-forwarded-for") == GESPOOFT, "Starlette geeft de EERSTE regel"
        with _hops(1):
            assert client_ip(request) == ROUTER_WAARDE

    def test_een_komma_lijst_met_gespoofd_adres_links(self):
        """Toets 2: ook binnen een regel geldt de rechtse waarde."""
        with _hops(1):
            assert client_ip(_request([f"{GESPOOFT}, {ROUTER_WAARDE}"])) == ROUTER_WAARDE

    def test_de_gemengde_vorm(self):
        """Een komma-lijst van de client met de regel van de router erachter."""
        request = _request([f"{GESPOOFT}, 203.0.113.9", ROUTER_WAARDE])

        with _hops(1):
            assert client_ip(request) == ROUTER_WAARDE
        with _hops(2):
            assert client_ip(request) == "203.0.113.9"

    def test_zonder_header_het_peer_adres(self):
        """Toets 3: en ook als ProxyHeadersMiddleware scope["client"] al heeft overschreven."""
        with _hops(1):
            assert client_ip(_request([], peer="192.168.1.100")) == "192.168.1.100"
            assert client_ip(_request([], peer="192.168.1.100", gespoofde_peer=GESPOOFT)) == "192.168.1.100"

    def test_hops_nul_negeert_elke_header(self):
        """Toets 4: zonder vertrouwde proxy komt er geen header aan te pas."""
        with _hops(0):
            assert client_ip(_request([GESPOOFT, ROUTER_WAARDE], peer="10.0.0.9")) == "10.0.0.9"
            assert client_ip(_request([f"{GESPOOFT}, {ROUTER_WAARDE}"], peer="10.0.0.9")) == "10.0.0.9"

    def test_minder_hops_dan_ingesteld_valt_terug_op_de_peer(self):
        """Een verzoek dat de ingress omzeilt mag geen adres naar keuze meekrijgen."""
        with _hops(2):
            assert client_ip(_request([GESPOOFT], peer="10.0.0.5")) == "10.0.0.5"

    def test_een_poort_achter_het_adres_valt_eraf(self):
        """Azure's Application Gateway stuurt deze vorm."""
        with _hops(1):
            assert client_ip(_request([f"{ROUTER_WAARDE}:56789"])) == ROUTER_WAARDE
            assert client_ip(_request(["[2001:db8::1]:443"])) == "2001:db8::1"
            assert client_ip(_request(["2001:db8::1"])) == "2001:db8::1"

    def test_een_waarde_die_geen_ip_is_valt_terug_op_de_peer(self):
        with _hops(1):
            assert client_ip(_request(["localhost"], peer="10.0.0.7")) == "10.0.0.7"
            assert client_ip(_request(["unknown"], peer="10.0.0.7")) == "10.0.0.7"

    def test_het_client_id_telt_op_de_router_waarde(self):
        """De emmer van de limiter krijgt het echte adres, niet dat van de aanvaller."""
        with _hops(1):
            client_id = IPRateLimiter.get_client_identifier(_request([GESPOOFT, ROUTER_WAARDE]))

        assert client_id.split("|")[0] == ROUTER_WAARDE


_sleutels = itertools.count(1)


def _projectstore(api_key: str = "goede-sleutel") -> MagicMock:
    project = MagicMock()
    project.api_key = api_key
    project.name = "demo"
    store = MagicMock()
    store.get.return_value = project
    return store


def _app(peer: str) -> TestClient:
    """De echte middlewarestapel: peer-adres, ProxyHeaders, de grove grens, de sleutelpoort."""
    app = FastAPI()

    @app.get("/api/v1/projects/{project_name}")
    @validate_api_token
    async def project(request: Request, project_name: str) -> dict[str, str]:
        return {"project": project_name}

    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])  # type: ignore[arg-type]
    app.add_middleware(PeerAddressMiddleware)
    return TestClient(app, client=(peer, 1234))


@contextmanager
def _verse_faaltellers(grens: int = 5) -> Iterator[None]:
    with ExitStack() as stack:
        for naam in ("AUTH_FAILURE_PER_CLIENT", "AUTH_FAILURE_PER_KEY"):
            stack.enter_context(patch.object(rate_limiter, naam, IPRateLimiter(requests_per_minute=grens, burst=grens)))
        yield


def _kop(gespoofd: str, router: str, sleutel: str) -> list[tuple[str, str]]:
    """Twee aparte X-Forwarded-For-regels, zoals HAProxy ze stuurt."""
    return [
        ("x-forwarded-for", gespoofd),
        ("x-forwarded-for", router),
        ("x-api-key", sleutel),
    ]


class TestDoorDeEchteMiddlewarestapel:
    """Toets 5: de rem telt op het juiste adres, ook met ProxyHeadersMiddleware ervoor."""

    def test_een_stortvloed_met_wisselend_gespoofd_adres_loopt_vast(self):
        peer = "147.181.15.231"
        client = _app(peer)
        with _hops(1), _verse_faaltellers():
            codes = set()
            for poging in range(500):
                antwoord = client.get(
                    "/api/v1/projects/demo",
                    headers=_kop(f"198.51.100.{poging % 254 + 1}", peer, f"fout-{next(_sleutels)}"),
                )
                codes.add(antwoord.status_code)
                if antwoord.status_code == 429:
                    break

            assert codes == {401, 429}, "de emmer per IP houdt, ook al wisselt het gespoofte adres"

            # Een geldige sleutel vanaf een ander echt adres blijft werken.
            ander = _app("147.181.15.232")
            with patch("opi.api.endpoint_util.get_project_store", return_value=_projectstore()):
                goed = ander.get(
                    "/api/v1/projects/demo",
                    headers=_kop(GESPOOFT, "147.181.15.232", "goede-sleutel"),
                )

        assert goed.status_code == 200

    def test_zonder_de_header_telt_de_rem_op_de_peer(self):
        """Een aanroeper die de ingress omzeilt kan geen vers adres kiezen."""
        client = _app("10.42.0.9")
        with _hops(0), _verse_faaltellers():
            codes = set()
            for poging in range(50):
                antwoord = client.get(
                    "/api/v1/projects/demo",
                    headers=[("x-forwarded-for", f"198.51.100.{poging % 254 + 1}"), ("x-api-key", f"f-{poging}")],
                )
                codes.add(antwoord.status_code)
                if antwoord.status_code == 429:
                    break

        assert 429 in codes


@pytest.mark.parametrize("hops", [0, 1])
def test_de_hopteller_komt_uit_de_setting(hops: int):
    request = _request([GESPOOFT, ROUTER_WAARDE], peer="10.0.0.3")
    with _hops(hops):
        assert client_ip(request) == (ROUTER_WAARDE if hops else "10.0.0.3")
