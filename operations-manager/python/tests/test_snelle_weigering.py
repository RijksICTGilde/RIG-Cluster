"""De snelle weigering op de sleutelpaden en de grove grens op de API (RC-368).

Wat hier gemeten wordt:

1. Een reeks foute pogingen van dezelfde client loopt vast op een 429 met ``Retry-After``.
   Dezelfde foute sleutel opnieuw aanbieden weigert vóór de vergelijking, zodat de
   projectlezing niet meer draait. Een geldige sleutel slaagt gewoon, ook vanaf het IP
   waarop net geraden is: de emmer per client weigert pas na een mislukte vergelijking.
2. De grove grens op ``/api/`` en ``/auth/`` weigert een burst en laat normaal verkeer
   door, en een stortvloed van verzonnen client-ids sluit een nieuwe bezoeker niet buiten.
3. De grenzen komen uit de settings, en de ``Retry-After`` is afgekapt.
4. Het verwisselen van alleen de header-vingerafdruk loopt op hetzelfde IP vast op de
   tweede emmer, en het verwisselen van de ``X-Forwarded-For`` levert geen verse emmer op:
   het IP komt van ``client_ip`` (tests/test_client_ip.py).
"""

import itertools
import math
import time
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from opi.api import rate_limiter
from opi.api.endpoint_util import validate_api_token
from opi.api.rate_limiter import (
    API_FLOOD_LIMITER,
    AUTH_FAILURE_PER_CLIENT,
    AUTH_FAILURE_PER_KEY,
    IPRateLimiter,
    retry_after_seconds,
)
from opi.core.config import settings
from opi.middleware.rate_limit import RateLimitMiddleware
from starlette.requests import Request
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import Iterator

#: De grens waarop de verse faaltellers in deze tests staan. Laag, zodat een test hem met
#: een handvol pogingen raakt.
FAALGRENS = 5

#: Elke test krijgt een eigen client-adres: de tellers staan op moduleniveau.
_adressen = itertools.count(1)


def _adres() -> str:
    return f"10.1.0.{next(_adressen)}"


_FAALTELLERS = ("AUTH_FAILURE_PER_CLIENT", "AUTH_FAILURE_PER_KEY")


@contextmanager
def _verse_faaltellers(grens: int = FAALGRENS) -> Iterator[None]:
    """Verse faaltellers met een lage grens, met de vulsnelheid die de code echt gebruikt."""
    with ExitStack() as stack:
        for naam in _FAALTELLERS:
            stack.enter_context(
                patch.object(
                    rate_limiter,
                    naam,
                    IPRateLimiter(requests_per_minute=settings.AUTH_FAILURE_LIMIT_PER_MINUTE, burst=grens),
                )
            )
        yield


def _request(ip: str, user_agent: str = "toets", api_key: str | None = None) -> Request:
    """Een echt Request-object, zodat het client-id langs dezelfde weg loopt als in productie.

    Het adres is het peer-adres van de verbinding, want dat is waarop de rem telt. Een
    gespoofte ``X-Forwarded-For`` levert geen eigen emmer op: tests/test_client_ip.py.
    """
    headers = [(b"user-agent", user_agent.encode()), (b"accept-language", b"nl")]
    if api_key is not None:
        headers.append((b"x-api-key", api_key.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/projects/demo",
            "query_string": b"",
            "root_path": "",
            "server": ("testserver", 80),
            "client": (ip, 1234),
            "headers": headers,
        }
    )


def _projectstore(api_key: str = "goede-sleutel") -> MagicMock:
    project = MagicMock()
    project.api_key = api_key
    project.name = "demo"
    store = MagicMock()
    store.get.return_value = project
    return store


@validate_api_token
async def _route(request: Request, project_name: str = "") -> dict[str, Any]:
    return {"project": project_name}


async def _poging(store: MagicMock, ip: str, sleutel: str, user_agent: str = "toets") -> int:
    """Doet een poging en geeft de statuscode terug (200 als de route werd bereikt)."""
    with patch("opi.api.endpoint_util.get_project_store", return_value=store):
        try:
            await _route(request=_request(ip, user_agent, sleutel), project_name="demo")
        except HTTPException as fout:
            return fout.status_code
    return 200


class TestDeFaaltellerOpDeSleutelpaden:
    """Toets 1 van het plan."""

    @pytest.mark.asyncio
    async def test_na_de_grens_volgt_429_met_retry_after(self):
        """De N+1e foute poging van dezelfde client geeft 429 met Retry-After."""
        store = _projectstore()
        ip = _adres()
        with _verse_faaltellers():
            for poging in range(FAALGRENS):
                assert await _poging(store, ip, f"fout-{poging}") == 401

            with (
                patch("opi.api.endpoint_util.get_project_store", return_value=store),
                pytest.raises(HTTPException) as fout,
            ):
                await _route(request=_request(ip, api_key="fout-weer"), project_name="demo")

        assert fout.value.status_code == 429
        assert int(fout.value.headers["Retry-After"]) >= 1

    @pytest.mark.asyncio
    async def test_dezelfde_foute_sleutel_weigert_zonder_sleutelvergelijking(self):
        """De emmer per sleutel weigert vóór de vergelijking, dus zonder projectlezing."""
        store = _projectstore()
        ip = _adres()
        with _verse_faaltellers():
            for _ in range(FAALGRENS):
                assert await _poging(store, ip, "dezelfde-foute-sleutel") == 401

            lezingen = store.get.call_count
            assert lezingen == FAALGRENS
            assert await _poging(store, ip, "dezelfde-foute-sleutel") == 429

        assert store.get.call_count == lezingen, "een geweigerde poging mag geen projectbestand lezen"

    @pytest.mark.asyncio
    async def test_een_geldige_sleutel_slaagt_ook_vanaf_het_ip_van_een_rader(self):
        """De foute pogingen van een ander mogen de houder van een geldige sleutel niets kosten.

        Een rader op het IP van zijn slachtoffer zou die anders buitensluiten zonder zelf
        een sleutel te hebben: de emmer per client telt immers op het IP.
        """
        store = _projectstore()
        rader = _adres()
        with _verse_faaltellers():
            for poging in range(FAALGRENS * 3 + 1):
                await _poging(store, rader, f"fout-{poging}")

            assert await _poging(store, rader, "goede-sleutel") == 200
            assert await _poging(store, _adres(), "goede-sleutel") == 200

    @pytest.mark.asyncio
    async def test_de_rader_mag_weer_raden_zodra_het_venster_verstreken_is(self):
        """Na de grens is het 429; zodra de emmers bijgevuld zijn weer de gewone 401."""
        store = _projectstore()
        ip = _adres()
        with _verse_faaltellers():
            for poging in range(FAALGRENS + 1):
                await _poging(store, ip, f"fout-{poging}")
            assert await _poging(store, ip, "fout-nog-een") == 429

            verschoven = time.monotonic() + 600
            with patch("time.monotonic", return_value=verschoven):
                assert await _poging(store, ip, "fout-na-het-venster") == 401

    @pytest.mark.asyncio
    async def test_dezelfde_foute_sleutel_vanaf_wisselende_clients_loopt_ook_vast(self):
        """De tweede teller staat per aangeboden sleutel, voor een rader die van client wisselt."""
        store = _projectstore()
        with _verse_faaltellers():
            for _ in range(FAALGRENS):
                assert await _poging(store, _adres(), "een-gestolen-sleutel", user_agent=f"ua-{_}") == 401

            assert await _poging(store, _adres(), "een-gestolen-sleutel", user_agent="ua-nieuw") == 429
            assert await _poging(store, _adres(), "goede-sleutel", user_agent="ua-nog-nieuwer") == 200


class TestDeGroveGrens:
    """Toets 2 van het plan."""

    @staticmethod
    def _app(limiter: IPRateLimiter, ip: str = "10.2.0.1") -> TestClient:
        app = FastAPI()

        @app.get("/api/ping")
        async def api_ping() -> dict[str, bool]:
            return {"ok": True}

        @app.get("/portaal/ping")
        async def portaal_ping() -> dict[str, bool]:
            return {"ok": True}

        app.add_middleware(RateLimitMiddleware, limiter=limiter)
        return TestClient(app, client=(ip, 1234))

    def test_een_burst_boven_de_grens_krijgt_429_en_herstelt(self):
        limiter = IPRateLimiter(requests_per_minute=60, burst=3, max_new_clients_per_minute=None)
        client = self._app(limiter, _adres())
        kop = {"user-agent": "toets"}

        codes = [client.get("/api/ping", headers=kop).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]

        geweigerd = client.get("/api/ping", headers=kop)
        assert int(geweigerd.headers["Retry-After"]) >= 1
        assert client.get("/portaal/ping", headers=kop).status_code == 200, "buiten /api/ en /auth/ geldt de grens niet"

        with patch("time.monotonic", return_value=time.monotonic() + 600):
            assert client.get("/api/ping", headers=kop).status_code == 200

    def test_normaal_verkeer_blijft_onder_de_ingestelde_grens(self):
        """Zestig verzoeken in een minuut, de ingestelde grens: niets geweigerd."""
        limiter = IPRateLimiter(
            requests_per_minute=settings.API_RATE_LIMIT_PER_MINUTE,
            burst=settings.API_RATE_LIMIT_BURST,
            max_new_clients_per_minute=None,
        )
        client = self._app(limiter, _adres())
        kop = {"user-agent": "toets"}

        codes = {client.get("/api/ping", headers=kop).status_code for _ in range(60)}
        assert codes == {200}

    def test_een_stortvloed_verzonnen_clients_sluit_een_nieuwe_bezoeker_niet_buiten(self):
        """De nieuwe-clientgrens staat uit: anders is de grove grens zelf de weigering.

        Met de limiter die de middleware in productie gebruikt, want die instelling is het
        onderwerp: honderd verzonnen client-ids zouden anders elke nieuwe bezoeker weigeren,
        en ``/auth/login`` zit erachter. Verzinnen gaat via de vingerafdruk: het IP in het
        client-id komt van ``client_ip`` en valt niet meer te kiezen.
        """
        for nummer in range(150):
            client = self._app(API_FLOOD_LIMITER, _adres())
            client.get("/api/ping", headers={"user-agent": f"ua-{nummer}"})

        nieuwe_bezoeker = self._app(API_FLOOD_LIMITER, _adres())
        assert nieuwe_bezoeker.get("/api/ping", headers={"user-agent": "ua-nieuw"}).status_code == 200


class TestDeGroveGrensInDeEchteApp:
    """De middlewarevolgorde uit ``server.py`` (commit 75eec255e).

    ``RateLimitMiddleware`` moet BINNEN ``SecurityHeadersMiddleware`` draaien: anders mist
    een 429 van de grove grens de veiligheidsheaders die elk ander antwoord wel draagt, en
    is hij daaraan te onderscheiden.
    """

    def test_een_429_van_de_grove_grens_draagt_de_veiligheidsheaders(self, test_client: TestClient) -> None:
        eerste = test_client.get("/api/een-onbestaand-pad")
        assert eerste.status_code != 429, "deze poging moet nog door de grove grens heen"

        # Direct de emmer van deze client leeghalen in plaats van hem met honderden
        # verzoeken te verzadigen: de vulsnelheid maakt een lus anders onvoorspelbaar traag.
        client_id = next(iter(API_FLOOD_LIMITER._tokens))
        API_FLOOD_LIMITER._tokens[client_id] = 0.0

        geweigerd = test_client.get("/api/een-onbestaand-pad")

        assert geweigerd.status_code == 429
        assert geweigerd.headers.get("X-Content-Type-Options") == "nosniff"
        assert "Content-Security-Policy" in geweigerd.headers


class TestDeGrenzenKomenUitDeSettings:
    """Toets 3 van het plan."""

    def test_de_faaltellers_lezen_hun_grens_uit_de_setting(self):
        for teller in (AUTH_FAILURE_PER_CLIENT, AUTH_FAILURE_PER_KEY):
            assert teller.burst == settings.AUTH_FAILURE_LIMIT_PER_MINUTE
            assert teller.rate == pytest.approx(settings.AUTH_FAILURE_LIMIT_PER_MINUTE / 60.0)

    def test_de_grove_grens_leest_zijn_grens_uit_de_settings(self):
        assert API_FLOOD_LIMITER.burst == settings.API_RATE_LIMIT_BURST
        assert API_FLOOD_LIMITER.rate == pytest.approx(settings.API_RATE_LIMIT_PER_MINUTE / 60.0)

    def test_de_retry_after_is_afgekapt_op_de_setting(self):
        """Een emmer die langzaam vult geeft anders een wachttijd waarin niemand terugkomt."""
        traag = IPRateLimiter(requests_per_minute=0.5, burst=4)
        for _ in range(6):
            traag.is_allowed("client")

        assert traag.seconds_until_token("client") > settings.RATE_LIMIT_RETRY_AFTER_MAX_SECONDS
        assert retry_after_seconds(traag, "client") == settings.RATE_LIMIT_RETRY_AFTER_MAX_SECONDS

    def test_een_emmer_die_nooit_vult_geeft_het_plafond_en_geen_overflow(self):
        """Een grens op 0 geeft math.inf als wachttijd, en math.ceil(inf) is een OverflowError."""
        stil = IPRateLimiter(requests_per_minute=0, burst=0)

        assert stil.seconds_until_token("client") == math.inf
        assert retry_after_seconds(stil, "client") == settings.RATE_LIMIT_RETRY_AFTER_MAX_SECONDS


class TestDeClientidentificatie:
    """Toets 4 van het plan."""

    @pytest.mark.asyncio
    async def test_alleen_de_vingerafdruk_verwisselen_loopt_vast_op_de_tweede_emmer(self):
        """Dezelfde X-Forwarded-For, elke poging een andere user-agent: de emmer per IP houdt."""
        store = _projectstore()
        ip = _adres()
        with _verse_faaltellers():
            doorgelaten = 0
            for poging in range(FAALGRENS * 10):
                if await _poging(store, ip, f"fout-{poging}", user_agent=f"ua-{poging}") == 401:
                    doorgelaten += 1
                else:
                    break

            assert doorgelaten <= FAALGRENS * 3, "de tweede emmer staat op driemaal de grens"
            assert await _poging(store, ip, "fout-nog-een", user_agent="ua-nieuw") == 429

    @pytest.mark.asyncio
    async def test_een_gespoofte_x_forwarded_for_geeft_geen_verse_emmer(self):
        """Het adres komt van ``client_ip``, dus wisselen helpt de rader niet meer.

        Tot de herziening van RC-368 nam de limiter de linkse ``X-Forwarded-For``, en dan
        had wie zijn adres en zijn sleutel elke poging verwisselde elke poging een verse
        emmer. De keten en de hopteller staan in tests/test_client_ip.py.
        """
        store = _projectstore()
        peer = _adres()
        with _verse_faaltellers():
            codes = set()
            for poging in range(FAALGRENS * 3):
                request = _request(peer, api_key=f"fout-{poging}")
                request.scope["headers"] = [
                    *request.scope["headers"],
                    (b"x-forwarded-for", f"198.51.100.{poging + 1}".encode()),
                ]
                with patch("opi.api.endpoint_util.get_project_store", return_value=store):
                    try:
                        await _route(request=request, project_name="demo")
                    except HTTPException as fout:
                        codes.add(fout.status_code)

        assert codes == {401, 429}
