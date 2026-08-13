"""Het tabblad Backups: een deployment per pagina, en het blok laadt zichzelf lui.

Het backupblok stond op het tabblad Deployments, als een van de blokken die de diensten
per deployment leveren. Het is een eigen tabblad geworden (RC-100), met dezelfde vorm als
Metrics: de deploymentnaam in het PAD, dezelfde kiezer, en een keuze die meereist als je
van tabblad wisselt.

Wat hier gemeten wordt is het GEDRAG, niet het beeld - dat is met een schermafbeelding
beoordeeld:

1. de pagina toont het blok van de deployment uit het pad, en niet dat van de andere;
2. de dialogen die met het blok mee verhuisd zijn (schema instellen, backup aanmaken)
   werken hier - een verhuisde knop die niets meer aanroept ziet er precies zo uit als een
   werkende;
3. de snapshotlijst wordt LUI opgehaald, precies een keer. Dat aantal is geen detail: per
   deployment een verzoek opende evenzoveel Kopia-verbindingen en sloopte de pod.

De fixture heeft TWEE deployments ('default' en 'tweede'); met een enkele bewijst punt 1
niets.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.htmx import scroll_backupblok_in_beeld

if TYPE_CHECKING:
    from playwright.sync_api import Page, Request, Route

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
BACKUPS_URL = f"/projects/{PROJECT}/backups"
DEPLOYMENTS_URL = f"/projects/{PROJECT}/deployments"


def test_de_pagina_toont_het_blok_van_de_deployment_uit_het_pad(app_server: str, auth_page: Page) -> None:
    """Een deployment per pagina, zoals op Deployments en Metrics.

    Niet verborgen maar afwezig: elk blok draagt zijn eigen lazy-lader, en die van een
    deployment die niemand bekijkt hoort niet te vertrekken.
    """
    auth_page.goto(f"{app_server}{BACKUPS_URL}/tweede")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.locator("#backups-snapshots-tweede").count() == 1
    assert auth_page.locator("#backups-snapshots-default").count() == 0


def test_zonder_deployment_in_het_pad_opent_er_een_en_zegt_de_url_welke(app_server: str, auth_page: Page) -> None:
    """Zelfde afspraak als op de andere twee tabbladen: de server kiest de eerste op naam
    en verwijst door, zodat het adres daarna zegt wat je ziet."""
    auth_page.goto(f"{app_server}{BACKUPS_URL}")
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.url.endswith(f"{BACKUPS_URL}/default")
    assert auth_page.locator("#backups-snapshots-default").count() == 1


def test_de_dialogen_van_het_blok_zijn_meeverhuisd(app_server: str, auth_page: Page) -> None:
    """Schema instellen en backup aanmaken horen bij het blok, dus staan ze hier.

    Uit de ECHTE DOM: onder LOTC gaat de aanroep via de :attrs-spread, en of hij in de
    uitvoer landt is aan de markup niet te zien.
    """
    auth_page.goto(f"{app_server}{BACKUPS_URL}/tweede")
    auth_page.wait_for_load_state("networkidle")

    aanroepen = auth_page.eval_on_selector_all(
        "#tab-backups [onclick]", "els => els.map(el => el.getAttribute('onclick')).sort()"
    )

    assert "openEditModal('modal-backup', 'Backup aanmaken', {deployment: 'tweede'})" in aanroepen
    # De schemadialoog wordt aangeduid met de INDEX van de deployment in het projectbestand
    # ('tweede' is de tweede), niet met zijn naam.
    assert "openEditModal('modal-edit-backup-schedule-1', 'Backup schema instellen')" in aanroepen


def test_het_blok_vuurt_zijn_ene_verzoek_af(app_server: str, auth_page: Page) -> None:
    """Lui, en precies een keer.

    De reden om het blok te VERBERGEN is met een deployment per pagina vervallen, de reden
    om het lui te laden niet: een snapshotlijst opent een Kopia-repository over S3 en dat
    kost seconden die de pagina anders laat wachten. Het verzoek wordt onderschept en
    bereikt de server niet; wat getoetst wordt is dat het vertrekt, en hoe vaak.
    """
    verzoeken: list[str] = []

    def handler(route: Route, request: Request) -> None:
        verzoeken.append(request.url)
        route.abort()

    auth_page.route("**/backups", handler)

    auth_page.goto(f"{app_server}{BACKUPS_URL}/default")
    auth_page.wait_for_load_state("networkidle")
    scroll_backupblok_in_beeld(auth_page)

    deadline = time.time() + 10
    while time.time() < deadline and not verzoeken:
        time.sleep(0.1)

    assert verzoeken == [f"{app_server}/projects/details/{PROJECT}/backups"], (
        f"het backupblok haalde niet precies een keer de snapshots op: {verzoeken}"
    )


def test_elke_deployment_haalt_zijn_eigen_lijst_op(app_server: str, auth_page: Page) -> None:
    """De tweede pagina laadt ook.

    Het blok van de EERSTE deployment droeg de lader, omdat alle blokken op een pagina
    stonden. Met een deployment per pagina zou die regel betekenen dat de pagina van elke
    andere deployment voor eeuwig "Backups worden opgehaald..." toont.
    """
    verzoeken: list[str] = []

    def handler(route: Route, request: Request) -> None:
        verzoeken.append(request.url)
        route.abort()

    auth_page.route("**/backups", handler)

    auth_page.goto(f"{app_server}{BACKUPS_URL}/tweede")
    auth_page.wait_for_load_state("networkidle")
    scroll_backupblok_in_beeld(auth_page)

    deadline = time.time() + 10
    while time.time() < deadline and not verzoeken:
        time.sleep(0.1)

    assert verzoeken == [f"{app_server}/projects/details/{PROJECT}/backups"]


def test_de_keuze_blijft_staan_bij_het_wisselen_van_tabblad(app_server: str, auth_page: Page) -> None:
    """Van Deployments naar Backups: dezelfde deployment, want de tabbalk draagt de naam."""
    auth_page.goto(f"{app_server}{DEPLOYMENTS_URL}/tweede")
    auth_page.wait_for_load_state("networkidle")

    auth_page.locator(f'a[href$="/{PROJECT}/backups/tweede"]').first.click()
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.url.endswith(f"{BACKUPS_URL}/tweede")
    assert auth_page.locator("#backups-snapshots-tweede").count() == 1


def test_de_kiezer_navigeert_binnen_het_tabblad(app_server: str, auth_page: Page) -> None:
    """Kiezen is navigeren, en de kiezer blijft op het tabblad waar je bent."""
    auth_page.goto(f"{app_server}{BACKUPS_URL}/default")
    auth_page.wait_for_load_state("networkidle")

    auth_page.select_option("#global-deployment-selector", f"{BACKUPS_URL}/tweede")
    auth_page.wait_for_url(f"**{BACKUPS_URL}/tweede", timeout=5000)
    auth_page.wait_for_load_state("networkidle")

    assert auth_page.locator("#backups-snapshots-tweede").count() == 1
    assert auth_page.locator("#backups-snapshots-default").count() == 0
