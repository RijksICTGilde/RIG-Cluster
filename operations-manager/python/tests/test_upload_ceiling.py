"""Een upload wordt gelezen met een plafond, niet eerst helemaal (RC-283).

Het plafond op een bijlage (64 KB, ``MAX_ATTACHMENT_BYTES``) werd pas getoetst nadat het
hele bestand in het geheugen stond. Een upload van een gigabyte kostte de pod dan een
gigabyte, en OPI heeft weinig marge voordat hij OOMKilled wordt. Wat hier gemeten wordt:

* ``read_capped`` breekt af op het blok dat het plafond passeert, dus het aantal
  leesaanroepen is begrensd en niet de grootte van de upload;
* beide wegen naar een bijlage (de wizard-upload en de API-actie) weigeren een te groot
  bestand met dezelfde melding die ze altijd gaven;
* een verzoek naar een bijlageroute met een body boven de bovengrens wordt geweigerd
  voordat de route draait.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.testclient import TestClient
from opi.forms.wizard.session import save_modal_state_by_token
from opi.forms.wizard.state import WizardState
from opi.middleware.body_limit import MAX_ATTACHMENT_BODY_BYTES, BodyLimitMiddleware
from opi.services.catalog.attachments.catalog_model import MAX_ATTACHMENT_BYTES
from opi.utils.uploads import CHUNK_SIZE, read_capped
from opi.web.router_wizard_attachments import stage_attachment
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpx
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.responses import Response

FLOW_ID = "modal-edit-attachments"
TOKEN = "1" * 32


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
    state = WizardState(flow_id=FLOW_ID, current_step="attachments", project_name="bijlage-plafond")
    state.base_data = {"services": []}
    save_modal_state_by_token(TOKEN, state)
    upload, _ = _upload(b"x" * (MAX_ATTACHMENT_BYTES + 1))

    response = await stage_attachment(
        Request({"type": "http", "method": "POST", "path": "/", "headers": [], "session": {}}),
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
BLOKKEN_BOVEN_DE_GRENS = MAX_ATTACHMENT_BODY_BYTES // CHUNK_SIZE + 2


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
    response = client.post("/forms/wizard/flow/attachments/stage", content=b"x" * (MAX_ATTACHMENT_BODY_BYTES + 1))

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
    response = client.post("/api/v2/images/upload", content=b"x" * (MAX_ATTACHMENT_BODY_BYTES + 1))

    assert response.status_code == 200


def test_create_app_bedraadt_de_body_limit_middleware() -> None:
    """Regressiewacht: zonder deze bedrading beschermt de middleware alleen de testapp hierboven."""
    from opi.server import create_app

    app = create_app()

    assert any(mw.cls is BodyLimitMiddleware for mw in app.user_middleware)
