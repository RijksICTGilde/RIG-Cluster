"""De uploadroutes houden een te grote, een trage en een gelijktijdige stortvloed tegen.

Een bijlage mag 64 KB (``MAX_ATTACHMENT_BYTES``), maar de multipart-parser van Starlette
heeft de body dan al binnen. ``BodyLimitMiddleware`` weigert daarom op de ontvangen bytes,
per route op zijn eigen grens (``attachment_body_limit``, ``image_push_body_limit``). Deze
proef meet dat die weigering er is, dat hij de pod niet laat groeien, en dat een trage of
gelijktijdige upload niet in een 5xx of een hang eindigt.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from _pytest.outcomes import Failed, Skipped
from opi.middleware.body_limit import attachment_body_limit

from security_tests.conftest import (
    TIJDBUDGET,
    GeenGeheugenmeting,
    leeft,
    proefclient,
    rss_bytes,
    sleutelclient,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpx

    from security_tests.doel import Doel

#: Ruim boven het plafond, en klein genoeg om er tien tegelijk te sturen. De grens komt uit
#: dezelfde ``attachment_body_limit()`` die de sandbox hanteert, niet uit een eigen getal.
TE_GROOT = attachment_body_limit() * 3

#: Groot genoeg om op te vallen in het geheugenbeeld, klein genoeg om snel te sturen. Het
#: plafond van de image-push zelf ligt in gigabytes, dus daarboven komen we niet.
PUSHBODY_BYTES = 2 * 1024 * 1024

#: Hoeveel de pod mag groeien over de hele proef heen. Een upload die in het geheugen
#: landt in plaats van geweigerd te worden, zet hier meteen een veelvoud van.
GROEIGRENS_BYTES = 64 * 1024 * 1024

#: Het aantal uploads dat tegelijk de deur in gaat.
GELIJKTIJDIG = 10

#: Een pid die niet bestaat, zodat een omgevallen proefsandbox te beproeven is.
_ONBESTAANDE_PID = 2**30


def bijlageroutes(project: str) -> list[tuple[str, str]]:
    """De routes die een bijlage aannemen: beide wegen naar hetzelfde plafond van 64 KB."""
    return [
        ("POST", "/forms/wizard/proef-flow/attachments/stage"),
        ("POST", f"/api/v2/projects/{project}/services/attachments/attachment"),
    ]


def image_pushroute(project: str) -> str:
    """De image-push, met een eigen plafond van IMAGE_UPLOAD_MAX_SIZE_MB."""
    return f"/api/v1/projects/{project}/images/push?image_name=proef&tag=proef"


def geheugenbeeld(pid: int) -> int:
    """Het geheugenbeeld van de proefsandbox.
    Skip of fail naar gelang of de pod nog leeft."""
    try:
        return rss_bytes(pid)
    except GeenGeheugenmeting as fout:
        if not leeft(pid):
            pytest.fail(f"de proefsandbox (pid {pid}) leeft niet meer: {fout}")
        pytest.skip(f"NIET GEMETEN: het geheugenbeeld van de proefsandbox: {fout}")


def _multipart(aantal_bytes: int) -> dict[str, tuple[str, bytes, str]]:
    return {"file": ("groot.bin", b"A" * aantal_bytes, "application/octet-stream")}


def beoordeel_te_groot(status: int) -> str | None:
    """Melding als dit antwoord op een te grote body niet klopt.
    Goed: 400/401/403/413/422/503 (geweigerd); slecht: 2xx (aangenomen) of ander 5xx."""
    if status in (400, 401, 403, 413, 422, 503):
        return None
    if status >= 500:
        return f"5xx ({status}) waar 413 hoort"
    return f"aangenomen met {status} waar 413 hoort"


def test_een_te_grote_body_wordt_geweigerd(proefproject: str, anoniem: httpx.Client) -> None:
    """Faalconditie: een body ver boven het plafond wordt aangenomen of geeft 5xx."""
    bevindingen = []
    for methode, pad in bijlageroutes(proefproject):
        antwoord = anoniem.request(methode, pad, files=_multipart(TE_GROOT))
        melding = beoordeel_te_groot(antwoord.status_code)
        if melding:
            bevindingen.append(f"{methode} {pad}: {melding}")
    assert not bevindingen, "\n".join(bevindingen)


def test_een_te_grote_body_met_projectsleutel_wordt_geweigerd(doel: Doel, proefproject: str) -> None:
    """Dezelfde grens, maar met een geldige sleutel: niet alleen de auth-laag weigert hem."""
    project = proefproject
    bevindingen = []
    with sleutelclient(doel, project) as client:
        for methode, pad in bijlageroutes(project)[1:]:
            antwoord = client.request(methode, pad, files=_multipart(TE_GROOT))
            melding = beoordeel_te_groot(antwoord.status_code)
            if melding:
                bevindingen.append(f"{methode} {pad}: {melding}")
    assert not bevindingen, "\n".join(bevindingen)


def test_de_image_push_weigert_zonder_sleutel_voor_de_body(
    doel: Doel, proefproject: str, anoniem: httpx.Client
) -> None:
    """Faalconditie: een push zonder geldige sleutel wordt pas na de body geweigerd.

    ``BodyLimitMiddleware`` doet deze controle vóór de multipart-parser, want een tarball van
    gigabytes zonder sleutel zou anders eerst de tempschijf kosten. De proef meet dat de 401
    er is en dat de pod er niet van groeit.
    """
    voor = geheugenbeeld(doel.pid)
    antwoord = anoniem.post(image_pushroute(proefproject), files=_multipart(PUSHBODY_BYTES))
    # 429 ook: na een reeks mislukte pogingen weigert de limiter van RC-368 nog eerder dan de
    # sleutelcontrole. Beide weigeren voor de body, en dat is wat deze proef meet.
    assert antwoord.status_code in (401, 429), f"image-push zonder sleutel gaf {antwoord.status_code}"
    groei = geheugenbeeld(doel.pid) - voor
    assert groei < GROEIGRENS_BYTES, f"de pod groeide {groei // 1024 // 1024} MB van een geweigerde push"


def test_de_image_push_weigert_voor_de_body_in_het_geheugen_landt(doel: Doel, proefproject: str) -> None:
    """Deze route heeft een eigen plafond van gigabytes, dus een body erboven is niet te sturen.

    Wat hier wel te meten is: de weigering komt voor de body (in de proefsandbox is geen
    registry geconfigureerd, dus 501) en kost geen geheugen. Een echte push naar een registry
    valt buiten de proef: de skopeo-binary is nagebootst.
    """
    project = proefproject
    voor = geheugenbeeld(doel.pid)
    with sleutelclient(doel, project) as client:
        begin = time.monotonic()
        antwoord = client.post(image_pushroute(project), files=_multipart(PUSHBODY_BYTES))
        duur = time.monotonic() - begin
    assert antwoord.status_code in (400, 401, 403, 413, 501), f"image-push gaf {antwoord.status_code}"
    assert duur < TIJDBUDGET, f"image-push hield de route {duur:.1f}s bezig"
    groei = geheugenbeeld(doel.pid) - voor
    assert groei < GROEIGRENS_BYTES, f"de pod groeide {groei // 1024 // 1024} MB van een image-push"


def test_een_trage_upload_blijft_niet_hangen(doel: Doel) -> None:
    """Faalconditie: een drip-upload houdt de route langer bezig dan het tijdbudget."""

    def drip() -> Iterator[bytes]:
        for _ in range(8):
            yield b"A" * 1024
            time.sleep(0.25)

    with proefclient(doel) as client:
        begin = time.monotonic()
        antwoord = client.post(
            "/forms/wizard/proef-flow/attachments/stage",
            content=drip(),
            headers={"Content-Type": "multipart/form-data; boundary=proefgrens"},
        )
        duur = time.monotonic() - begin
    assert antwoord.status_code < 500, f"trage upload gaf {antwoord.status_code}"
    assert duur < TIJDBUDGET, f"trage upload hield de route {duur:.1f}s bezig"


def test_gelijktijdige_te_grote_uploads_laten_de_pod_staan(doel: Doel) -> None:
    """Faalconditie: de pod groeit boven de grens, geeft 5xx, of antwoordt daarna niet meer."""
    voor = geheugenbeeld(doel.pid)
    pad = "/forms/wizard/proef-flow/attachments/stage"

    def upload(_: int) -> int:
        with proefclient(doel) as client:
            return client.post(pad, files=_multipart(TE_GROOT)).status_code

    with ThreadPoolExecutor(max_workers=GELIJKTIJDIG) as pool:
        statussen = list(pool.map(upload, range(GELIJKTIJDIG)))

    assert all(beoordeel_te_groot(status) is None for status in statussen), f"statussen: {statussen}"
    na = geheugenbeeld(doel.pid)
    groei = na - voor
    assert groei < GROEIGRENS_BYTES, f"de pod groeide {groei // 1024 // 1024} MB tijdens de stortvloed"
    with proefclient(doel) as client:
        assert client.get("/health").status_code == 200, "de pod antwoordt niet meer na de stortvloed"


@pytest.mark.parametrize(
    ("status", "is_bevinding"),
    [(413, False), (401, False), (422, False), (503, False), (200, True), (201, True), (500, True), (502, True)],
)
def test_de_uploadproef_merkt_een_aangenomen_body_op(status: int, is_bevinding: bool) -> None:
    """Negatief voorbeeld: een route die de body aanneemt of eraan stukgaat, is een bevinding."""
    assert (beoordeel_te_groot(status) is not None) is is_bevinding


def test_een_niet_te_meten_geheugenbeeld_is_niet_gemeten_en_geen_bevinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negatief voorbeeld: op een platform zonder geheugenbeeld hoort een skip, geen bevinding."""

    def geen_meting(_pid: int) -> int:
        raise GeenGeheugenmeting("ps is hier niet te draaien")

    monkeypatch.setattr("security_tests.test_uploads.rss_bytes", geen_meting)
    with pytest.raises(Skipped) as overgeslagen:
        geheugenbeeld(os.getpid())
    assert "NIET GEMETEN" in str(overgeslagen.value)


def test_een_sandbox_die_niet_meer_leeft_is_een_bevinding() -> None:
    """Faalconditie: de pod ging onder de stortvloed om, en de proef slaat zich over.

    Beide uitkomsten worden gevangen, want een overslag die hier doorschiet zou deze proef
    zelf overslaan in plaats van rood zetten.
    """
    with pytest.raises((Failed, Skipped)) as uitkomst:
        geheugenbeeld(_ONBESTAANDE_PID)
    assert isinstance(uitkomst.value, Failed), f"een dode sandbox werd {type(uitkomst.value).__name__}"
    assert "leeft niet meer" in str(uitkomst.value)


def test_rss_bytes_meet_het_eigen_proces() -> None:
    """Faalconditie: een vergeten *1024 geeft de rss in KiB terug in plaats van in bytes."""
    groei = rss_bytes(os.getpid())
    # ``ps`` geeft nooit een pythonproces onder een paar MB; een waarde onder de 1 MiB is de
    # KiB-waarde die ``rss_bytes`` zonder de omrekening zou teruggeven.
    assert 1024 * 1024 < groei < 10 * 1024 * 1024 * 1024, f"rss van het eigen proces: {groei} bytes"
