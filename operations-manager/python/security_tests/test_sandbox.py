"""De bouwstenen van de proefsandbox die zonder de volledige server te beproeven zijn.

``proefsandbox()`` zelf start een echte uvicorn-server en een echt OPI; dat hoort bij
``task security-proof:up`` en bij de dekking die de andere proeven er al tegen leveren. Wat
hier staat zijn de onderdelen die zelfstandig kunnen breken: het token op elk antwoord, de
regel waarmee een omgevallen verzoek zijn route in het logboek zet, de sessiekoekjes die de
rollen inloggen, en het afbreken van een draaiende sandbox.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from base64 import b64decode
from typing import TYPE_CHECKING, Any

import pytest
from itsdangerous import TimestampSigner
from tests.e2e.testserver import SECRET_KEY

from security_tests import sandbox
from security_tests.doel import TOKEN_HEADER
from security_tests.sandbox import RouteLogApp, TokenHeaderApp, _sessiekoekje
from security_tests.sandboxlog import asgi_fouten, laat_los, lees, route_uit, vang_op, vensters

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.types import Message, Receive, Scope, Send


class _Valt:
    """Een onderliggende app die omvalt, zoals een route die een exception laat ontsnappen."""

    async def __call__(self, _scope: Scope, _receive: Receive, _send: Send) -> None:
        raise ValueError("iets ging mis")


class _Stub:
    """Een onderliggende ASGI-app die precies de meegegeven berichten verstuurt."""

    def __init__(self, berichten: list[Message]) -> None:
        self._berichten = berichten
        self.gezien_scope: Scope | None = None

    async def __call__(self, scope: Scope, _receive: Receive, send: Send) -> None:
        self.gezien_scope = scope
        for bericht in self._berichten:
            await send(bericht)


async def _draai(app: Any, scope: dict[str, Any]) -> list[Message]:
    verzonden: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(bericht: Message) -> None:
        verzonden.append(bericht)

    await app(scope, receive, send)
    return verzonden


async def test_token_header_app_zet_het_token_op_elk_antwoord() -> None:
    onderliggend = _Stub(
        [
            {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]},
            {"type": "http.response.body", "body": b"ok"},
        ]
    )
    berichten = await _draai(TokenHeaderApp(onderliggend, "proef-token"), {"type": "http"})
    start = berichten[0]
    headers = dict(start["headers"])
    assert headers[TOKEN_HEADER.lower().encode()] == b"proef-token"
    assert headers[b"content-type"] == b"text/plain", "bestaande headers van het antwoord blijven staan"
    assert berichten[1]["body"] == b"ok"


async def test_token_header_app_laat_de_onderliggende_app_zonder_token_zien() -> None:
    """Negatief voorbeeld: zonder de wrapper staat het token er niet op."""
    onderliggend = _Stub([{"type": "http.response.start", "status": 200, "headers": []}])
    berichten = await _draai(onderliggend, {"type": "http"})
    assert dict(berichten[0]["headers"]) == {}


async def test_token_header_app_bemoeit_zich_niet_met_lifespan() -> None:
    """De grendel herkent de proefsandbox via een HTTP-header; een lifespan-scope heeft die niet."""
    onderliggend = _Stub([])
    scope: dict[str, Any] = {"type": "lifespan"}
    await _draai(TokenHeaderApp(onderliggend, "proef-token"), scope)
    assert onderliggend.gezien_scope is scope


async def test_route_log_app_logt_een_5xx_met_de_route_die_sandboxlog_terugleest(tmp_path: Path) -> None:
    """Faalconditie: de logregel van RouteLogApp en de regex van sandboxlog lopen uit elkaar."""
    log = tmp_path / "opi.log"
    handler = vang_op(log)
    try:
        onderliggend = _Stub([{"type": "http.response.start", "status": 500, "headers": []}])
        await _draai(RouteLogApp(onderliggend), {"type": "http", "method": "GET", "path": "/api/v2/upload"})
    finally:
        laat_los(handler)
    records = lees(log)
    assert route_uit(records) == "GET /api/v2/upload"
    assert any(record.niveau == "ERROR" for record in records), "waarom() leest alleen WARNING of erger"


async def test_route_log_app_zwijgt_over_een_geslaagd_antwoord(tmp_path: Path) -> None:
    """Negatief voorbeeld: een 200 hoort geen route in het logboek te zetten."""
    log = tmp_path / "opi.log"
    handler = vang_op(log)
    try:
        onderliggend = _Stub([{"type": "http.response.start", "status": 200, "headers": []}])
        await _draai(RouteLogApp(onderliggend), {"type": "http", "method": "GET", "path": "/health"})
    finally:
        laat_los(handler)
    assert route_uit(lees(log)) == "onbekend"


async def test_route_log_app_noemt_de_route_van_een_omgevallen_verzoek_en_laat_de_fout_door(
    tmp_path: Path,
) -> None:
    """Faalconditie: de fout wordt opgeslokt, of de traceback van uvicorn blijft zonder route."""
    log = tmp_path / "opi.log"
    handler = vang_op(log)
    try:
        with pytest.raises(ValueError, match="iets ging mis"):
            await _draai(RouteLogApp(_Valt()), {"type": "http", "method": "POST", "path": "/api/v2/upload"})
    finally:
        laat_los(handler)
    records = lees(log)
    assert route_uit(records) == "POST /api/v2/upload"
    assert any("ValueError" in record.tekst for record in records)


async def test_route_log_app_zet_geen_uitnodigingssleutel_in_het_logboek(tmp_path: Path) -> None:
    """Faalconditie: een bearer-geheim uit het pad belandt letterlijk in het logboek."""
    log = tmp_path / "opi.log"
    handler = vang_op(log)
    try:
        onderliggend = _Stub([{"type": "http.response.start", "status": 500, "headers": []}])
        scope = {"type": "http", "method": "GET", "path": "/invite/zeer-geheime-sleutel/register"}
        await _draai(RouteLogApp(onderliggend), scope)
    finally:
        laat_los(handler)
    assert "zeer-geheime-sleutel" not in log.read_text()
    assert "/invite/" in log.read_text()


async def test_een_pad_met_een_regeleinde_verzint_geen_tweede_logregel(tmp_path: Path) -> None:
    """Faalconditie: het pad komt van buiten, dus een regeleinde erin schrijft een eigen
    kopregel, en die leest als een proefvenster of als een ASGI-fout die niemand veroorzaakte."""
    log = tmp_path / "opi.log"
    handler = vang_op(log)
    try:
        onderliggend = _Stub([{"type": "http.response.start", "status": 500, "headers": []}])
        pad = (
            "/x\n2026-10-23 12:00:00,000 INFO zad.proef marker start nep::test_nep"
            "\n2026-10-23 12:00:00,001 ERROR uvicorn.error Exception in ASGI application"
        )
        await _draai(RouteLogApp(onderliggend), {"type": "http", "method": "GET", "path": pad})
    finally:
        laat_los(handler)
    records = lees(log)
    assert len(records) == 1, f"een pad schreef meer dan een logregel: {[r.kop for r in records]}"
    assert asgi_fouten(records) == []
    assert vensters(records) == {}


async def test_route_log_app_bemoeit_zich_niet_met_lifespan() -> None:
    """De logregel gaat over een verzoek; een lifespan-scope heeft geen methode en geen pad."""
    onderliggend = _Stub([])
    scope: dict[str, Any] = {"type": "lifespan"}
    await _draai(RouteLogApp(onderliggend), scope)
    assert onderliggend.gezien_scope is scope


def test_sessiekoekje_is_een_geldig_starlette_sessiekoekje() -> None:
    """Zonder dit koekje leest OPI's SessionMiddleware een anonieme aanroeper in plaats van een rol."""
    koekje = _sessiekoekje("proef-admin@example.nl", "Proef ZAD-admin")
    data = TimestampSigner(SECRET_KEY).unsign(koekje.encode())
    sessie = json.loads(b64decode(data))
    assert sessie["user"]["email"] == "proef-admin@example.nl"
    assert sessie["user"]["name"] == "Proef ZAD-admin"
    assert sessie["user"]["sub"] == "proef-admin@example.nl"


def test_sessiekoekje_met_een_ander_geheim_wordt_geweigerd() -> None:
    """Negatief voorbeeld: een koekje dat niet met OPI's eigen geheim is ondertekend, hoort te falen."""
    koekje = _sessiekoekje("proef-admin@example.nl", "Proef ZAD-admin")
    with pytest.raises(Exception, match="Signature"):
        TimestampSigner("een-ander-geheim-dat-niet-klopt").unsign(koekje.encode())


class TestStop:
    """``stop()`` breekt een draaiende proefsandbox af aan de hand van het runbestand."""

    def test_zonder_runbestand_is_er_niets_om_af_te_breken(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sandbox, "RUNBESTAND", tmp_path / "sandbox.json")
        assert sandbox.stop() == 0
        assert "geen runbestand" in capsys.readouterr().out

    def test_een_proces_dat_al_dood_is_ruimt_alleen_het_runbestand_op(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runbestand = tmp_path / "sandbox.json"
        # Een pid die vrijwel zeker niet bestaat: elk pid-bestand hierboven in dit proces is al
        # lang gereaped. Faalconditie: stop() stuurt een signaal naar een hergebruikt pid.
        runbestand.write_text(json.dumps({"pid": 999_999, "url": "x", "token": "x", "sleutels": {}, "sessies": {}}))
        monkeypatch.setattr(sandbox, "RUNBESTAND", runbestand)
        assert sandbox.stop() == 0
        assert not runbestand.exists()
        assert "draaide niet meer" in capsys.readouterr().out

    def test_een_proces_dat_netjes_afsluit_wordt_niet_geforceerd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        proces = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            runbestand = tmp_path / "sandbox.json"
            runbestand.write_text(
                json.dumps({"pid": proces.pid, "url": "x", "token": "x", "sleutels": {}, "sessies": {}})
            )
            monkeypatch.setattr(sandbox, "RUNBESTAND", runbestand)
            assert sandbox.stop(budget=10.0) == 0
            assert proces.wait(timeout=5) == -15  # SIGTERM, geen SIGKILL
            assert not runbestand.exists()
            assert "afgebroken" in capsys.readouterr().out
        finally:
            if proces.poll() is None:
                proces.kill()
                proces.wait(timeout=5)

    def test_een_proces_dat_sigterm_negeert_wordt_na_het_budget_geforceerd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Faalconditie: stop() geeft op of hangt zodra SIGTERM alleen niet voldoende is."""
        proces = subprocess.Popen(
            [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"]
        )
        try:
            _wacht_tot_signalen_genegeerd_worden()
            runbestand = tmp_path / "sandbox.json"
            runbestand.write_text(
                json.dumps({"pid": proces.pid, "url": "x", "token": "x", "sleutels": {}, "sessies": {}})
            )
            monkeypatch.setattr(sandbox, "RUNBESTAND", runbestand)
            assert sandbox.stop(budget=1.0) == 0
            assert proces.wait(timeout=5) == -9  # SIGKILL
            assert not runbestand.exists()
            assert "geforceerd afgebroken" in capsys.readouterr().out
        finally:
            if proces.poll() is None:
                proces.kill()
                proces.wait(timeout=5)


def _wacht_tot_signalen_genegeerd_worden() -> None:
    """Geef het kindproces de tijd om SIGTERM op SIG_IGN te zetten voor de proef begint."""
    time.sleep(0.3)


def test_main_met_stop_als_argument_breekt_af_en_zet_geen_sandbox_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Faalconditie: ``main(["stop"])`` zet toch een nieuwe proefsandbox op in plaats van af te breken."""
    monkeypatch.setattr(sandbox, "RUNBESTAND", tmp_path / "sandbox.json")

    def geen_sandbox(*_: object, **__: object) -> None:
        raise AssertionError("main(['stop']) opende een proefsandbox")

    monkeypatch.setattr(sandbox, "proefsandbox", geen_sandbox)
    assert sandbox.main(["stop"]) == 0
