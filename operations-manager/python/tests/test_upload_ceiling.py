"""Een upload wordt gelezen met een plafond, niet eerst helemaal (RC-283).

Het plafond op een bijlage (64 KB, ``MAX_ATTACHMENT_BYTES``) werd pas getoetst nadat het
hele bestand in het geheugen stond. Een upload van een gigabyte kostte de pod dan een
gigabyte, en OPI heeft weinig marge voordat hij OOMKilled wordt. Wat hier gemeten wordt:

* ``read_capped`` breekt af op het blok dat het plafond passeert, dus het aantal
  leesaanroepen is begrensd en niet de grootte van de upload;
* beide wegen naar een bijlage (de wizard-upload en de API-actie) weigeren een te groot
  bestand met dezelfde melding die ze altijd gaven;
* een verzoek naar een bijlageroute met een body boven de bovengrens wordt geweigerd
  voordat de route draait;
* een upload boven het plafond van gelijktijdige uploads wacht in een begrensde wachtrij,
  en wordt pas geweigerd als die vol is (RC-367);
* de image-push krijgt zijn sleutelcontrole en zijn plek in het slot vóór de parser, dus
  een verzoek zonder geldige sleutel en een verzoek op een vol slot laten geen byte body
  binnen (RC-367);
* die sleutelcontrole loopt langs dezelfde faaltellers als de routes, want deze weg staat
  buiten ``RateLimitMiddleware`` (RC-368);
* die voorcontrole weegt ook de ROL en niet alleen het bewijsmiddel, dus een token dat
  ``image.push`` niet opent krijgt zijn 403 voordat de tarball de tempschijf raakt, met
  dezelfde body en dezelfde auditrij als de route erachter zou geven (RC-410);
* die voorcontrole telt zelf af in de grove grens per client en legt zijn auditrij als
  taak weg, want anders is een stortvloed van weigeringen hier ongeremd en wacht elke
  weigering op een verbinding uit de ORM-pool van de pod (RC-410).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.testclient import TestClient
from opi.api.image_router import image_router
from opi.api.rate_limiter import API_FLOOD_LIMITER, AUTH_FAILURE_PER_KEY, FLOOD_COUNTED_IN_SCOPE
from opi.core.authz.engine import Cause
from opi.core.config import settings
from opi.core.leden import LidSoort
from opi.forms.wizard.session import save_modal_state_by_token
from opi.forms.wizard.state import WizardState
from opi.middleware.body_limit import (
    BodyLimitMiddleware,
    attachment_body_limit,
    body_limit_for,
    image_push_body_limit,
)
from opi.middleware.rate_limit import RateLimitMiddleware
from opi.services.catalog.attachments.catalog_model import MAX_ATTACHMENT_BYTES
from opi.services.project_leden import hash_van
from opi.utils import uploads
from opi.utils.uploads import CHUNK_SIZE, read_capped, spill_capped, upload_slot
from opi.web.router_wizard_attachments import stage_attachment
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    import httpx
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.responses import Response
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

FLOW_ID = "modal-edit-attachments"
TOKEN = "1" * 32
# De wizard geeft zijn state alleen aan de gebruiker die hem opende.
OWNER_EMAIL = "bouwer@rijksoverheid.nl"


class _TellendeBuffer(io.BytesIO):
    """Een buffer die bijhoudt hoe vaak eruit gelezen is."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.reads = 0

    def read(self, size: int | None = -1) -> bytes:
        self.reads += 1
        return super().read(size)


def _upload(data: bytes) -> tuple[UploadFile, _TellendeBuffer]:
    buffer = _TellendeBuffer(data)
    return UploadFile(file=buffer, filename="server.pem"), buffer


# --------------------------------------------------------------------------------------
# 1. De lezer
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_precies_op_het_plafond_komt_er_volledig_uit() -> None:
    data = b"x" * MAX_ATTACHMENT_BYTES
    upload, _ = _upload(data)

    assert await read_capped(upload, MAX_ATTACHMENT_BYTES) == data


@pytest.mark.asyncio
async def test_een_byte_erover_geeft_niets_terug() -> None:
    upload, _ = _upload(b"x" * (MAX_ATTACHMENT_BYTES + 1))

    assert await read_capped(upload, MAX_ATTACHMENT_BYTES) is None


@pytest.mark.asyncio
async def test_een_leeg_bestand_blijft_leeg_en_is_geen_weigering() -> None:
    """Leeg is een eigen fout, eentje die de aanroeper van de lezer geeft."""
    upload, _ = _upload(b"")

    assert await read_capped(upload, MAX_ATTACHMENT_BYTES) == b""


@pytest.mark.asyncio
async def test_een_grote_upload_wordt_niet_uitgelezen() -> None:
    """De kern van de taak: het aantal blokken is begrensd, niet de upload."""
    groot = 64 * MAX_ATTACHMENT_BYTES
    upload, buffer = _upload(b"x" * groot)

    assert await read_capped(upload, MAX_ATTACHMENT_BYTES) is None
    # Een blok voorbij het plafond is genoeg om te weten dat het eroverheen gaat.
    assert buffer.reads == MAX_ATTACHMENT_BYTES // CHUNK_SIZE + 1
    assert buffer.tell() < groot


# --------------------------------------------------------------------------------------
# 2. De wizard-upload
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_de_wizard_weigert_een_te_groot_bestand_met_de_bestaande_melding() -> None:
    state = WizardState(
        flow_id=FLOW_ID, current_step="attachments", project_name="bijlage-plafond", owner_email=OWNER_EMAIL
    )
    state.base_data = {"services": []}
    save_modal_state_by_token(TOKEN, state)
    upload, _ = _upload(b"x" * (MAX_ATTACHMENT_BYTES + 1))

    response = await stage_attachment(
        Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [],
                "session": {},
                "state": {"user": {"email": OWNER_EMAIL}},
            }
        ),
        FLOW_ID,
        attachment_id="server-cert",
        file=upload,
        wizard_token=TOKEN,
        mode="",
    )

    assert "Bestand te groot (max 64 KB)" in response.body.decode()


# --------------------------------------------------------------------------------------
# 3. De bovengrens op de body
# --------------------------------------------------------------------------------------

BOUNDARY = "bijlagegrens"
BLOKKEN_BOVEN_DE_GRENS = attachment_body_limit() // CHUNK_SIZE + 2


def _multipart_blokken(blokken: int) -> Iterator[bytes]:
    """Een multipart-body in losse blokken, dus zonder ``Content-Length``."""
    yield (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="server.pem"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    for _ in range(blokken):
        yield b"x" * CHUNK_SIZE
    yield f"\r\n--{BOUNDARY}--\r\n".encode()


def _post_in_blokken(client: TestClient, pad: str, blokken: int) -> httpx.Response:
    return client.post(
        pad,
        content=_multipart_blokken(blokken),
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
    )


class _LeestDeBody(BaseHTTPMiddleware):
    """De vorm van ``CSRFMiddleware``: leest de body van een multipart-post voor de route."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        await request.body()
        await request.form()
        return await call_next(request)


def _app(*, leest_de_body: bool = False) -> FastAPI:
    """Een app met de middleware eromheen en de routevorm die een bijlageroute heeft."""
    app = FastAPI()
    app.state.gedraaid = []

    # Beide bijlageroutes declareren ``File(...)``, en die vorm bepaalt wat er van een
    # afgebroken body terugkomt: FastAPI leest hem dan zelf, voordat de handler draait.
    @app.post("/forms/wizard/flow/attachments/stage")
    async def _stage(request: Request, file: UploadFile = File(...)) -> dict[str, int]:
        request.app.state.gedraaid.append("stage")
        return {"gelezen": len(await file.read())}

    @app.post("/api/v2/images/upload")
    async def _image(request: Request) -> dict[str, int]:
        return {"gelezen": len(await request.body())}

    if leest_de_body:
        app.add_middleware(_LeestDeBody)
    app.add_middleware(BodyLimitMiddleware)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app())


def test_een_body_boven_de_bovengrens_wordt_geweigerd(client: TestClient) -> None:
    response = client.post("/forms/wizard/flow/attachments/stage", content=b"x" * (attachment_body_limit() + 1))

    assert response.status_code == 413
    assert "Body te groot" in response.json()["detail"]
    assert client.app.state.gedraaid == [], "de route heeft de body toch gelezen"


def test_een_gewone_bijlage_upload_komt_erdoor(client: TestClient) -> None:
    response = client.post(
        "/forms/wizard/flow/attachments/stage", files={"file": ("server.pem", b"x" * MAX_ATTACHMENT_BYTES)}
    )

    assert response.status_code == 200
    assert response.json() == {"gelezen": MAX_ATTACHMENT_BYTES}


def test_een_chunked_body_zonder_content_length_wordt_ook_geweigerd(client: TestClient) -> None:
    """Zonder ``Content-Length`` valt de grens pas tijdens het lezen, en dan nog met 413."""
    response = _post_in_blokken(client, "/forms/wizard/flow/attachments/stage", BLOKKEN_BOVEN_DE_GRENS)

    assert response.status_code == 413
    assert "Body te groot" in response.json()["detail"]
    assert client.app.state.gedraaid == [], "de route heeft de body toch gelezen"


def test_een_chunked_bijlage_onder_de_bovengrens_komt_erdoor(client: TestClient) -> None:
    response = _post_in_blokken(client, "/forms/wizard/flow/attachments/stage", 1)

    assert response.status_code == 200
    assert response.json() == {"gelezen": CHUNK_SIZE}


def test_een_middleware_die_de_body_leest_geeft_geen_serverfout() -> None:
    """De app heeft er een: ``CSRFMiddleware`` leest de body van een multipart-post.

    Die loopt op een afgebroken body in een ``ClientDisconnect``, en zonder dat die bij de
    grens hoort zou de client een 500 zien in plaats van de 413.
    """
    client = TestClient(_app(leest_de_body=True))

    response = _post_in_blokken(client, "/forms/wizard/flow/attachments/stage", BLOKKEN_BOVEN_DE_GRENS)

    assert response.status_code == 413
    assert "Body te groot" in response.json()["detail"]


def test_een_andere_route_houdt_zijn_eigen_plafond(client: TestClient) -> None:
    """De image-upload heeft een eigen, veel hoger plafond en gaat hier niet langs."""
    response = client.post("/api/v2/images/upload", content=b"x" * (attachment_body_limit() + 1))

    assert response.status_code == 200


def test_create_app_bedraadt_de_body_limit_middleware() -> None:
    """Regressiewacht: zonder deze bedrading beschermt de middleware alleen de testapp hierboven."""
    from opi.server import create_app

    app = create_app()

    assert any(mw.cls is BodyLimitMiddleware for mw in app.user_middleware)


def test_body_limit_for_kent_de_image_push_zijn_eigen_grens_toe() -> None:
    """De image-push hoort veel hoger te mogen dan een bijlage, en een onbekend pad niets."""
    assert body_limit_for("/forms/wizard/flow/attachments/stage") == attachment_body_limit()
    assert body_limit_for("/api/v1/projects/test-project/images/push") == image_push_body_limit()
    assert body_limit_for("/api/v2/images/upload") is None


def test_image_push_body_limit_volgt_image_upload_max_size_mb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "IMAGE_UPLOAD_MAX_SIZE_MB", 1)

    assert image_push_body_limit() == 1024 * 1024


def test_een_te_grote_image_push_wordt_geweigerd_voor_de_route_draait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zijn eigen, veel hogere grens geldt ook hier: geweigerd vóór de route, niet erna."""
    monkeypatch.setattr(settings, "IMAGE_UPLOAD_MAX_SIZE_MB", 1)
    app = FastAPI()
    app.state.gedraaid = []

    @app.post("/api/v1/projects/test-project/images/push")
    async def _push(request: Request) -> dict[str, int]:
        request.app.state.gedraaid.append("push")
        return {"gelezen": len(await request.body())}

    app.add_middleware(BodyLimitMiddleware)
    client = TestClient(app)

    response = client.post("/api/v1/projects/test-project/images/push", content=b"x" * (image_push_body_limit() + 1))

    assert response.status_code == 413
    assert client.app.state.gedraaid == [], "de route heeft de body toch gelezen"


# --------------------------------------------------------------------------------------
# 4. Het slot en zijn wachtrij
# --------------------------------------------------------------------------------------


@pytest.fixture
def slot_van_een(monkeypatch: pytest.MonkeyPatch) -> None:
    """Een slot met plek voor één upload en een wachtrij van één, zodat de derde weigert."""
    monkeypatch.setattr(settings, "MAX_CONCURRENT_UPLOADS", 1)
    monkeypatch.setattr(settings, "UPLOAD_QUEUE_SIZE", 1)
    monkeypatch.setattr(settings, "UPLOAD_QUEUE_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(uploads, "_slot", None)


@pytest.mark.asyncio
async def test_een_upload_boven_het_plafond_wacht_in_de_wachtrij(slot_van_een: None) -> None:
    """Een upload is meestal zo gelezen, dus de tweede wacht op zijn plek, hij wordt niet geweigerd."""
    volgorde: list[str] = []

    async def upload(naam: str, duur: float) -> None:
        async with upload_slot():
            volgorde.append(naam)
            await asyncio.sleep(duur)

    await asyncio.gather(upload("eerste", 0.05), upload("tweede", 0))

    assert volgorde == ["eerste", "tweede"]


@pytest.mark.asyncio
async def test_een_volle_wachtrij_weigert_met_503(slot_van_een: None) -> None:
    los = asyncio.Event()
    bezig = asyncio.Event()

    async def houd_de_plek_vast() -> None:
        async with upload_slot():
            bezig.set()
            await los.wait()

    async def wacht_in_de_rij() -> str:
        async with upload_slot():
            return "erdoor"

    vasthouder = asyncio.create_task(houd_de_plek_vast())
    await bezig.wait()
    wachter = asyncio.create_task(wacht_in_de_rij())
    await asyncio.sleep(0)

    with pytest.raises(HTTPException) as fout:
        async with upload_slot():
            pass

    assert fout.value.status_code == 503

    los.set()
    await vasthouder
    # Wie al in de rij stond komt er wel door, en daarna is het slot weer vrij.
    assert await wachter == "erdoor"
    async with upload_slot():
        pass


@pytest.mark.asyncio
async def test_het_slot_komt_vrij_na_een_fout(slot_van_een: None) -> None:
    with pytest.raises(RuntimeError):
        async with upload_slot():
            raise RuntimeError("upload afgebroken")

    async with upload_slot():
        pass


@pytest.fixture
def slot_met_korte_wachttijd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Een slot met plek voor een wachter, maar een wachttijd die meteen verloopt."""
    monkeypatch.setattr(settings, "MAX_CONCURRENT_UPLOADS", 1)
    monkeypatch.setattr(settings, "UPLOAD_QUEUE_SIZE", 10)
    monkeypatch.setattr(settings, "UPLOAD_QUEUE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(uploads, "_slot", None)


@pytest.mark.asyncio
async def test_een_wachter_die_de_wachttijd_overschrijdt_weigert_met_503(
    slot_met_korte_wachttijd: None,
) -> None:
    """Niet alleen een volle rij weigert: wie te lang wacht, krijgt ook een 503."""
    bezig = asyncio.Event()

    async def houd_de_plek_vast() -> None:
        async with upload_slot():
            bezig.set()
            await asyncio.sleep(1)

    vasthouder = asyncio.create_task(houd_de_plek_vast())
    await bezig.wait()

    with pytest.raises(HTTPException) as fout:
        async with upload_slot():
            pass

    assert fout.value.status_code == 503

    vasthouder.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await vasthouder
    # De plek is weer vrij: de timeout van de wachter houdt hem niet vast.
    async with upload_slot():
        pass


@pytest.fixture
def vol_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Een slot zonder plek en zonder wachtrij: de volgende upload wordt meteen geweigerd."""
    monkeypatch.setattr(settings, "MAX_CONCURRENT_UPLOADS", 0)
    monkeypatch.setattr(settings, "UPLOAD_QUEUE_SIZE", 0)
    monkeypatch.setattr(uploads, "_slot", None)


@pytest.fixture
def app_met_een_uploadroute() -> TestClient:
    """De echte app, met een route die het slot neemt zoals de drie uploadroutes dat doen.

    De 503 van het slot is een ``HTTPException`` en loopt dus langs de foutafhandeling van
    de app, waar de ``Retry-After`` eerder afviel; die wordt hier op het antwoord gemeten
    en niet op de uitzondering. Een GET, want een POST naar een pad zonder formulier
    strandt op de CSRF-controle voordat het slot in zicht komt. Zonder ``with``, want de
    lifespan van de echte app verbindt met de buitenwereld.
    """
    from opi.server import create_app

    app = create_app()

    @app.get("/slot-van-een-upload")
    async def _neem_het_slot() -> dict[str, bool]:
        async with upload_slot():
            return {"gelezen": True}

    return TestClient(app)


@pytest.mark.parametrize("accept", ["application/json", "text/html"])
def test_een_geweigerde_upload_geeft_een_retry_after_op_het_antwoord(
    app_met_een_uploadroute: TestClient, vol_slot: None, accept: str
) -> None:
    response = app_met_een_uploadroute.get("/slot-van-een-upload", headers={"Accept": accept})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(settings.UPLOAD_SLOT_RETRY_AFTER_SECONDS)


# --------------------------------------------------------------------------------------
# 5. spill_capped: in het geheugen tot de grens, daarboven naar een tempbestand
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spill_capped_blijft_in_het_geheugen_onder_spill_after() -> None:
    data = b"x" * 100
    upload, _ = _upload(data)

    staged = await spill_capped(upload, max_bytes=1000, spill_after=1000)

    assert staged is not None
    assert staged.content == data
    assert staged.path is None
    assert staged.size == len(data)


@pytest.mark.asyncio
async def test_spill_capped_valt_zonder_argument_terug_op_de_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Geen ``spill_after`` meegegeven: de grens komt dan uit ``UPLOAD_SPILL_AFTER_BYTES``."""
    monkeypatch.setattr(settings, "UPLOAD_SPILL_AFTER_BYTES", 10)
    data = b"x" * 100
    upload, _ = _upload(data)

    staged = await spill_capped(upload, max_bytes=len(data))

    assert staged is not None
    assert staged.path is not None
    assert staged.content is None
    staged.cleanup()


@pytest.mark.asyncio
async def test_spill_capped_schrijft_naar_een_tempbestand_boven_spill_after() -> None:
    data = b"x" * (CHUNK_SIZE * 3)
    upload, _ = _upload(data)

    staged = await spill_capped(upload, max_bytes=len(data), spill_after=CHUNK_SIZE)

    assert staged is not None
    assert staged.content is None
    assert staged.path is not None
    assert staged.size == len(data)
    with open(staged.path, "rb") as f:
        assert f.read() == data

    pad = staged.path
    staged.cleanup()

    assert staged.path is None
    assert not os.path.exists(pad)


@pytest.mark.asyncio
async def test_spill_capped_precies_op_het_plafond_komt_er_volledig_uit() -> None:
    data = b"x" * MAX_ATTACHMENT_BYTES
    upload, _ = _upload(data)

    staged = await spill_capped(upload, MAX_ATTACHMENT_BYTES, spill_after=0)

    assert staged is not None
    assert staged.size == MAX_ATTACHMENT_BYTES
    assert staged.path is not None
    with open(staged.path, "rb") as f:
        assert f.read() == data
    staged.cleanup()


@pytest.mark.asyncio
async def test_spill_capped_boven_het_plafond_geeft_none_en_ruimt_het_tempbestand_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Een half weggeschreven tempbestand mag niet blijven liggen als het plafond wordt gehaald."""
    monkeypatch.setattr(settings, "TEMP_DIR", str(tmp_path))
    upload, _ = _upload(b"x" * (MAX_ATTACHMENT_BYTES + 1))

    staged = await spill_capped(upload, MAX_ATTACHMENT_BYTES, spill_after=0)

    assert staged is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_spill_capped_ruimt_het_tempbestand_op_als_de_upload_afbreekt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Een client die wegvalt geeft een CancelledError, en die mag geen tarball achterlaten."""
    monkeypatch.setattr(settings, "TEMP_DIR", str(tmp_path))

    class _AfgebrokenUpload:
        def __init__(self) -> None:
            self.gelezen = 0

        async def read(self, size: int) -> bytes:
            self.gelezen += 1
            if self.gelezen > 2:
                raise asyncio.CancelledError
            return b"x" * size

    with pytest.raises(asyncio.CancelledError):
        await spill_capped(_AfgebrokenUpload(), max_bytes=10**9, spill_after=0)  # type: ignore[arg-type]

    assert list(tmp_path.iterdir()) == []


def test_attachment_body_limit_volgt_max_attachment_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """De bijlagegrens is afgeleid van het bijlageplafond, niet los ingetypt."""
    monkeypatch.setattr(settings, "ATTACHMENT_BODY_LIMIT_FACTOR", 2)

    assert attachment_body_limit() == MAX_ATTACHMENT_BYTES * 2


# --------------------------------------------------------------------------------------
# 6. De image-push: sleutel en slot vóór de multipart-parser (RC-367)
# --------------------------------------------------------------------------------------
#
# FastAPI parseert de body voordat de handler draait, dus een 401 of een 503 uit de route
# zelf valt pas nadat de hele tarball ontvangen is. Tien pushes van 5 GB zonder geldige
# sleutel zouden zo 50 GB tempschijf kosten. BodyLimitMiddleware is de enige plek die voor
# de parser draait, en doet die twee controles daarom daar.

PUSH_PAD = "/api/v1/projects/test-project/images/push?image_name=app&tag=v1"


class _TeltOntvangen:
    """Telt hoe vaak de app onder de middleware om een stuk body vraagt."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.ontvangen = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def tellend() -> Message:
            self.ontvangen += 1
            return await receive()

        await self.app(scope, tellend, send)


@pytest.fixture
def push_stack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[TestClient, _TeltOntvangen]:
    """De echte image-push achter de middleware, met skopeo en het projectregister gemokt."""
    monkeypatch.setattr(settings, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "IMAGE_UPLOAD_MAX_SIZE_MB", 10)
    monkeypatch.setattr(settings, "REGISTRY_URL", "rcr.rijksapps.nl")

    project = SimpleNamespace(name="test-project", api_key="de-juiste-sleutel", users=[])
    store = MagicMock()
    store.get.return_value = project
    monkeypatch.setattr("opi.api.endpoint_util.get_project_store", lambda: store)

    connector = MagicMock()
    connector.validate_push_target = MagicMock()
    connector.push_image = AsyncMock(return_value="rcr.rijksapps.nl/rig/zad:test-project_app-v1")
    monkeypatch.setattr("opi.api.image_router.SkopeoConnector", lambda: connector)

    app = FastAPI()
    app.include_router(image_router)
    teller = _TeltOntvangen(app)
    return TestClient(BodyLimitMiddleware(teller)), teller


def test_een_push_zonder_sleutel_leest_geen_body(
    push_stack: tuple[TestClient, _TeltOntvangen], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, teller = push_stack

    with caplog.at_level("WARNING", logger="opi.api.endpoint_util"):
        response = client.post(PUSH_PAD, files={"file": ("img.tar", b"x" * 4096)})

    assert response.status_code == 401
    assert any("Authenticatie mislukt" in r.getMessage() for r in caplog.records)
    assert teller.ontvangen == 0, "de body is toch gelezen"
    assert list(tmp_path.iterdir()) == []


def test_een_push_met_een_verkeerde_sleutel_leest_geen_body(
    push_stack: tuple[TestClient, _TeltOntvangen], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Een sleutel die niet bij het project in het pad hoort komt even ver als geen sleutel."""
    client, teller = push_stack

    with caplog.at_level("WARNING", logger="opi.api.endpoint_util"):
        response = client.post(
            PUSH_PAD, files={"file": ("img.tar", b"x" * 4096)}, headers={"X-API-Key": "de-sleutel-van-een-ander"}
        )

    assert response.status_code == 401
    assert any("Authenticatie mislukt" in r.getMessage() for r in caplog.records)
    assert not any("de-sleutel-van-een-ander" in r.getMessage() for r in caplog.records)
    assert response.json()["detail"] == "Invalid API key"
    assert teller.ontvangen == 0, "de body is toch gelezen"
    assert list(tmp_path.iterdir()) == []


def test_raden_op_de_push_loopt_vast_op_de_faalteller(
    push_stack: tuple[TestClient, _TeltOntvangen], tmp_path: Path
) -> None:
    """Deze weg doet de sleutelvergelijking zelf en staat buiten RateLimitMiddleware.

    Zonder de faaltellers kostte raden hier niets: een 401 per poging, met een
    projectlezing en een compare_digest erbij.
    """
    client, teller = push_stack
    grens = AUTH_FAILURE_PER_KEY.burst

    def push() -> httpx.Response:
        return client.post(PUSH_PAD, files={"file": ("img.tar", b"x" * 4096)}, headers={"X-API-Key": "raad-maar"})

    assert [push().status_code for _ in range(grens)] == [401] * grens

    geweigerd = push()
    assert geweigerd.status_code == 429
    assert int(geweigerd.headers["Retry-After"]) >= 1
    assert teller.ontvangen == 0, "de body is toch gelezen"
    assert list(tmp_path.iterdir()) == []


def test_een_push_op_een_vol_slot_wordt_geweigerd_voor_de_parser(
    push_stack: tuple[TestClient, _TeltOntvangen], vol_slot: None, tmp_path: Path
) -> None:
    """Met MAX_CONCURRENT_UPLOADS uploads in behandeling volgt de 503 vóór de body."""
    client, teller = push_stack

    response = client.post(
        PUSH_PAD, files={"file": ("img.tar", b"x" * 4096)}, headers={"X-API-Key": "de-juiste-sleutel"}
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(settings.UPLOAD_SLOT_RETRY_AFTER_SECONDS)
    assert teller.ontvangen == 0, "de body is toch gelezen"
    assert list(tmp_path.iterdir()) == []


def test_een_geldige_push_komt_er_nog_helemaal_door(
    push_stack: tuple[TestClient, _TeltOntvangen], tmp_path: Path
) -> None:
    client, _ = push_stack

    response = client.post(
        PUSH_PAD, files={"file": ("img.tar", b"x" * 4096)}, headers={"X-API-Key": "de-juiste-sleutel"}
    )

    assert response.status_code == 200
    assert response.json()["image"] == "rcr.rijksapps.nl/rig/zad:test-project_app-v1"
    assert list(tmp_path.iterdir()) == [], "de tarball is niet opgeruimd"


@pytest.mark.asyncio
async def test_de_route_neemt_het_slot_niet_een_tweede_keer(slot_van_een: None) -> None:
    """Dubbel nemen zou één push twee plekken kosten; met een slot van één loopt dat vast."""
    scope: dict[str, object] = {uploads.SLOT_HELD_IN_SCOPE: True}

    async def meting() -> None:
        async with upload_slot(scope), upload_slot(scope):
            pass

    await asyncio.wait_for(meting(), timeout=5)


# --- de ROL vóór de upload (RC-410) ----------------------------------------
#
# Een geldig token met een rol die de push weigert streamde tot IMAGE_UPLOAD_MAX_SIZE_MB
# naar de tempschijf voordat de 403 van de route viel, en hield zolang een uploadslot
# bezet. De voorcontrole weegt daarom image.push, met de handeling van de route zelf.

TOKEN_VAN_DE_PIJPLIJN = "zad_pijplijn"


def _project_met_rol(*rollen: str) -> SimpleNamespace:
    """Een project met één service account dat ``TOKEN_VAN_DE_PIJPLIJN`` draagt."""
    return SimpleNamespace(
        name="test-project",
        api_key=None,
        data={},
        users=[
            SimpleNamespace(
                type=LidSoort.SERVICE_ACCOUNT,
                name="bouwstraat",
                principaal="bouwstraat",
                email=None,
                roles=list(rollen),
                tokens=[
                    SimpleNamespace(
                        name="pijplijn",
                        expires=datetime.now(UTC) + timedelta(days=30),
                        hash=hash_van(TOKEN_VAN_DE_PIJPLIJN),
                    )
                ],
            )
        ],
    )


@pytest.fixture
def push_met_rol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Callable[[str], tuple[TestClient, _TeltOntvangen]]]:
    """De image-push achter de middleware, met een token waarvan de ROL instelbaar is."""
    monkeypatch.setattr(settings, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "IMAGE_UPLOAD_MAX_SIZE_MB", 10)
    monkeypatch.setattr(settings, "REGISTRY_URL", "rcr.rijksapps.nl")

    connector = MagicMock()
    connector.validate_push_target = MagicMock()
    connector.push_image = AsyncMock(return_value="rcr.rijksapps.nl/rig/zad:test-project_app-v1")
    monkeypatch.setattr("opi.api.image_router.SkopeoConnector", lambda: connector)

    def bouw(rol: str) -> tuple[TestClient, _TeltOntvangen]:
        store = MagicMock()
        store.get.return_value = _project_met_rol(rol)
        monkeypatch.setattr("opi.api.endpoint_util.get_project_store", lambda: store)
        app = FastAPI()
        app.include_router(image_router)
        teller = _TeltOntvangen(app)
        return TestClient(BodyLimitMiddleware(teller)), teller

    return bouw


def _push(client: TestClient) -> httpx.Response:
    return client.post(PUSH_PAD, files={"file": ("img.tar", b"x" * 4096)}, headers={"X-API-Key": TOKEN_VAN_DE_PIJPLIJN})


def test_een_te_smalle_rol_leest_geen_body(
    push_met_rol: Callable[[str], tuple[TestClient, _TeltOntvangen]], tmp_path: Path
) -> None:
    """Een viewer is herkend maar opent image.push niet, en hoort dat vóór de parser."""
    client, teller = push_met_rol("viewer")

    response = _push(client)

    assert response.status_code == 403
    # Dezelfde body als handhaaf geeft: de reden als eigen veld naast de tekst.
    assert response.json()["detail"]["cause"] == Cause.INSUFFICIENT_ROLE.value
    assert response.json()["detail"]["message"]
    assert teller.ontvangen == 0, "de body is toch gelezen"
    assert list(tmp_path.iterdir()) == []


def test_een_token_met_de_rol_komt_er_wel_door(
    push_met_rol: Callable[[str], tuple[TestClient, _TeltOntvangen]], tmp_path: Path
) -> None:
    client, teller = push_met_rol("developer")

    response = _push(client)

    assert response.status_code == 200
    assert response.json()["image"] == "rcr.rijksapps.nl/rig/zad:test-project_app-v1"
    assert teller.ontvangen > 0, "de body is niet gelezen"
    assert list(tmp_path.iterdir()) == [], "de tarball is niet opgeruimd"


def test_een_te_smalle_rol_telt_niet_als_mislukte_poging(
    push_met_rol: Callable[[str], tuple[TestClient, _TeltOntvangen]],
) -> None:
    """Een te smalle rol is geen raadpoging: de faaltellers blijven voor de 401.

    Zonder dit onderscheid zou een pijplijn met een verkeerde rol zichzelf op 429 zetten,
    en zou de emmer van de houder leeglopen door een fout in zijn eigen rollen.
    """
    client, _ = push_met_rol("viewer")

    uitkomsten = [_push(client).status_code for _ in range(AUTH_FAILURE_PER_KEY.burst + 1)]

    assert uitkomsten == [403] * (AUTH_FAILURE_PER_KEY.burst + 1)


def test_de_weigering_op_de_rol_krijgt_een_auditrij(
    push_met_rol: Callable[[str], tuple[TestClient, _TeltOntvangen]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """De audithook staat binnen de router, dus deze weigering legt zichzelf vast.

    Zonder die rij zou het naar voren halen van de 403 juist de vastlegging ervan kwijt
    maken, en elke weigering hoort de handelende partij te noemen (BIO2 8.15.01).
    """
    rijen = AsyncMock()
    monkeypatch.setattr("opi.middleware.audit.record_action", rijen)
    client, _ = push_met_rol("viewer")

    assert _push(client).status_code == 403

    rijen.assert_awaited_once()
    rij = rijen.await_args.kwargs
    assert rij["action"] == "image.push"
    assert rij["allowed"] is False
    assert rij["cause"] == Cause.INSUFFICIENT_ROLE.value
    assert rij["status_code"] == 403
    assert rij["project"] == "test-project"
    assert rij["actor"].identity == "test-project/bouwstraat"
    # De velden die @action(audit_fields=...) op deze route noemt, uit dezelfde bron als
    # de hook ze zou lezen.
    assert rij["fields"] == {"image_name": "app", "tag": "v1"}


def test_een_push_zonder_query_parameters_blijft_een_403(
    push_met_rol: Callable[[str], tuple[TestClient, _TeltOntvangen]], tmp_path: Path
) -> None:
    """De voorcontrole leest nooit een formulier, ook niet voor een auditveld.

    ``capture_fields`` valt bij een ontbrekend auditveld terug op het formulier van het
    verzoek. Hier draait dat vóór de router, op een verzoek dat nog geen byte mag leveren:
    zonder het lege ontvangkanaal viel deze weigering om in een RuntimeError, en daarmee
    in een 500 in plaats van de 403 die de rol verdient.
    """
    client, teller = push_met_rol("viewer")

    response = client.post(
        "/api/v1/projects/test-project/images/push",  # geen image_name, geen tag
        data={"image_name": "app"},  # dus een formulier-content-type
        headers={"X-API-Key": TOKEN_VAN_DE_PIJPLIJN},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["cause"] == Cause.INSUFFICIENT_ROLE.value
    assert teller.ontvangen == 0, "de body is toch gelezen"
    assert list(tmp_path.iterdir()) == []


class _ZegtDatHetGeteldIs:
    """Een buitenste middleware die de vlag zet die ``BodyLimitMiddleware`` ook zet."""

    def __init__(self, app: object) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        scope[FLOOD_COUNTED_IN_SCOPE] = True
        await self.app(scope, receive, send)  # type: ignore[operator]


def test_de_voorcontrole_valt_ook_onder_de_grove_grens(
    push_met_rol: Callable[[str], tuple[TestClient, _TeltOntvangen]],
) -> None:
    """De weigering op de rol komt nooit bij ``RateLimitMiddleware``, dus telt hier zelf af.

    Zonder dit is een stortvloed van 403's op dit pad ongeremd, terwijl elk ander
    ``/api/``-pad op de grove grens strandt. Elke weigering kost een auditrij.
    """
    client, _ = push_met_rol("viewer")
    assert _push(client).status_code == 403

    client_id = next(iter(API_FLOOD_LIMITER._tokens))
    API_FLOOD_LIMITER._tokens[client_id] = 0.0

    response = _push(client)

    assert response.status_code == 429
    assert response.json()["detail"] == "Too many requests"
    assert response.headers["Retry-After"]


def test_een_toegestane_push_kost_een_token(
    push_met_rol: Callable[[str], tuple[TestClient, _TeltOntvangen]],
) -> None:
    """Eenmaal tellen: de voorcontrole telt af en de middleware erbinnen niet opnieuw."""
    client, _ = push_met_rol("developer")

    assert _push(client).status_code == 200

    (tokens,) = API_FLOOD_LIMITER._tokens.values()
    assert tokens == pytest.approx(API_FLOOD_LIMITER.burst - 1, abs=0.01)


def test_de_vlag_houdt_de_tweede_telling_tegen() -> None:
    """``RateLimitMiddleware`` laat een verzoek door dat de buitenste al heeft geteld."""
    app = FastAPI()

    @app.post("/api/v1/iets")
    async def _iets() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(_ZegtDatHetGeteldIs(RateLimitMiddleware(app))) as client:
        assert client.post("/api/v1/iets").status_code == 200

    assert API_FLOOD_LIMITER._tokens == {}, "er is toch een tweede keer geteld"
