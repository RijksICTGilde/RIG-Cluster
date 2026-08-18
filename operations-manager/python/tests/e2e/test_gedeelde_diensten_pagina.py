"""Browsertest voor /admin/diensten: rendert de pagina, en rendert hij het GOEDE?

Een groene servertest bewijst niet dat een tabel als tabel op het scherm staat. Deze test
gaat er met een echte browser heen, wacht op de twee lui geladen blokken en kijkt naar wat
er staat: de volgorde van de PVC's, de toestand van de volste, en dat Redis en MinIO
BENOEMD worden in plaats van weggelaten.

De metrieken komen van een vaste connector in tests/e2e/testserver.py met de getallen van
de meting tegen productie van 18 augustus 2026.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

DIENSTEN_URL = "/admin/diensten"

RAW_LOTC_PATTERN = re.compile(r"<c-[a-z][a-z-]+")

#: De PVC uit de meting tegen productie van 18 augustus 2026 die op 92,7% stond.
VOLSTE_PVC = "production-typesense-data-pvc"

#: c-table rendert geen <table> maar custom elements; een selector op tbody/tr matcht
#: NIETS en laat een telling stilletjes op nul uitkomen.
OPSLAG_RIJEN = '#diensten-opslag nldd-table-row[data-lotc-component="table-row"]'


def _open_diensten(app_server: str, auth_page: Page) -> str:
    auth_page.goto(f"{app_server}{DIENSTEN_URL}")
    # Wachten op iets dat ALLEEN uit een fragment kan komen. Hier stond "text=Wachtend",
    # en die matchte de drempel verbindingen_wachtend op de pagina zelf: de test liep dus
    # door voordat er ook maar een blok binnen was en zag lege tabellen voor volle aan.
    auth_page.wait_for_selector(f"text={VOLSTE_PVC}", timeout=15_000)
    auth_page.wait_for_selector("text=Per instantie", timeout=15_000)
    return auth_page.content()


def test_de_pagina_laadt(app_server: str, auth_page: Page) -> None:
    response = auth_page.goto(f"{app_server}{DIENSTEN_URL}")
    assert response is not None
    assert response.ok
    assert "Gedeelde diensten" in auth_page.content()


def test_geen_onvertaalde_componenttags(app_server: str, auth_page: Page) -> None:
    """Ook in de lui geladen fragmenten: die renderen langs dezelfde weg."""
    html = _open_diensten(app_server, auth_page)
    resten = sorted(set(RAW_LOTC_PATTERN.findall(html)))
    assert not resten, f"onvertaalde componenttags op {DIENSTEN_URL}: {resten}"


def test_de_volste_pvc_staat_bovenaan_en_is_kritiek(app_server: str, auth_page: Page) -> None:
    """De aanleiding voor deze pagina: 92,7% die niemand zag, nu op regel een."""
    _open_diensten(app_server, auth_page)

    eerste_rij = auth_page.locator(OPSLAG_RIJEN).first
    assert VOLSTE_PVC in eerste_rij.inner_text()
    assert "92.7%" in eerste_rij.inner_text()
    # inner_html en niet inner_text: de toestand is een LOTC-tag die zijn tekst uit een
    # attribuut haalt en in een shadow root zet, en die telt niet mee in inner_text.
    assert "Kritiek" in eerste_rij.inner_html()


def test_de_pvcs_staan_op_volgorde_van_vulling(app_server: str, auth_page: Page) -> None:
    _open_diensten(app_server, auth_page)

    rijen = auth_page.locator(OPSLAG_RIJEN)
    aantal = rijen.count()
    # Anders is de sorteertest vacuum waar: een lege lijst is ook gesorteerd.
    assert aantal >= 2, f"te weinig rijen om een volgorde op te toetsen: {aantal}"
    teksten = [rijen.nth(index).inner_text() for index in range(aantal)]
    percentages = [float(re.search(r"(\d+\.\d)%", tekst).group(1)) for tekst in teksten]
    assert percentages == sorted(percentages, reverse=True), percentages


def test_de_databases_tonen_grootte_en_verbindingen(app_server: str, auth_page: Page) -> None:
    html = _open_diensten(app_server, auth_page)

    assert "forgejo" in html
    assert "keycloak" in html
    # Wachtende verbindingen staan in hun eigen tabel, want ze bestaan per instantie.
    assert "Per instantie" in html
    assert "rig-db-1" in html


def test_redis_en_minio_worden_benoemd(app_server: str, auth_page: Page) -> None:
    """Afwezigheid mag niet als 'in orde' lezen; ze horen op de pagina te STAAN."""
    html = _open_diensten(app_server, auth_page)

    assert "Redis is niet in beeld" in html
    assert "MinIO is niet in beeld" in html
    assert "redis-exporter" in html


def test_de_drempels_staan_op_de_pagina(app_server: str, auth_page: Page) -> None:
    """De volgende stap is alerting; de grenzen horen navraagbaar te zijn."""
    html = _open_diensten(app_server, auth_page)

    assert "Drempels" in html
    assert "pvc_vulling" in html
    assert "85" in html
    # De eenheid staat in een eigen kolom, anders leest een drempel als "1 verbindingen".
    assert "Eenheid" in html


def test_schermafdruk(app_server: str, auth_page: Page, tmp_path) -> None:
    """Een afdruk om NAAR TE KIJKEN; een groene test zegt niets over de opmaak."""
    _open_diensten(app_server, auth_page)
    doel = tmp_path / "admin-diensten.png"
    auth_page.screenshot(path=str(doel), full_page=True)
    assert doel.exists()
