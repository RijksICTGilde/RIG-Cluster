"""De registratiepagina van de tweede factor, door de echte route en sjablonen (RC-329).

De test kent het zaad niet uit de service maar LEEST het van de pagina (de Base32 die een
gebruiker met de hand zou invoeren) en rekent daar de code uit. Dat is precies de weg die
een authenticator-app ook neemt, dus het meet meteen dat de pagina het goede geheim toont.
"""

from __future__ import annotations

import asyncio
import base64
import re
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from opi.utils.totp import totp_now
from tests.e2e.testserver import get_in_memory_otp_service

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

OTP_URL = "/account/otp"
#: Het adres waarmee ``auth_page`` is ingelogd (``TEST_USER`` in conftest).
EMAIL = "test@example.com"
RAW_COMPONENT_PATTERN = re.compile(r"<c-[a-z][a-z-]+")


@pytest.fixture(autouse=True)
def lege_factor() -> None:
    """Elke test begint zonder factor; de stub is een singleton over de hele sessie."""
    get_in_memory_otp_service()._rows.clear()


def _code_van_de_pagina(page: Page) -> str:
    """De geldige code van het zaad dat de registratiepagina toont."""
    base32 = page.locator("code").first.inner_text().strip()
    padded = base32 + "=" * (-len(base32) % 8)
    return totp_now(base64.b32decode(padded).decode())[0]


def _start_registratie(page: Page, basis: str) -> None:
    page.goto(f"{basis}{OTP_URL}")
    page.locator("text=Tweede factor inrichten").last.click()
    page.wait_for_selector(".otp-qr svg")


def _reset_door_een_beheerder() -> None:
    """De reset die /admin/users uitvoert, op de stub van de service.

    In een eigen thread: Playwright draait zijn eigen loop in deze thread, en
    ``asyncio.run`` weigert daarnaast.
    """
    coroutine = get_in_memory_otp_service().reset(EMAIL, by="beheerder@rijksoverheid.nl")
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(asyncio.run, coroutine).result() is True


def _bevestig_registratie(page: Page, basis: str) -> None:
    """Van de lege stand naar een bevestigde factor, met de code van de pagina zelf."""
    _start_registratie(page, basis)
    page.locator("input[name='code']").fill(_code_van_de_pagina(page))
    page.locator("button[type='submit'], input[type='submit']").first.click()
    page.wait_for_load_state("networkidle")


class TestStatuspagina:
    def test_zonder_factor_staat_er_dat_hij_nog_niet_is_ingericht(self, app_server: str, auth_page: Page) -> None:
        response = auth_page.goto(f"{app_server}{OTP_URL}")
        assert response is not None
        assert response.ok
        assert "Nog niet ingericht" in auth_page.content()

    def test_geen_onverwerkte_componenttags(self, app_server: str, auth_page: Page) -> None:
        auth_page.goto(f"{app_server}{OTP_URL}")
        auth_page.wait_for_load_state("networkidle")
        assert RAW_COMPONENT_PATTERN.findall(auth_page.content()) == []

    def test_de_accountpagina_wijst_naar_de_tweede_factor(self, app_server: str, auth_page: Page) -> None:
        auth_page.goto(f"{app_server}/account")
        assert auth_page.locator("a[href='/account/otp']").first.is_visible()


class TestRegistreren:
    def test_de_pagina_toont_een_qr_en_een_sleutel(self, app_server: str, auth_page: Page) -> None:
        _start_registratie(auth_page, app_server)
        assert auth_page.locator(".otp-qr svg").is_visible()
        assert auth_page.locator("code").first.inner_text().strip()
        assert RAW_COMPONENT_PATTERN.findall(auth_page.content()) == []

    def test_bevestigen_meldt_de_factor_en_zet_hem_vast(self, app_server: str, auth_page: Page) -> None:
        _bevestig_registratie(auth_page, app_server)

        inhoud = auth_page.content()
        assert "Tweede factor ingericht" in inhoud
        assert "Ingericht" in inhoud

    def test_de_accountpagina_toont_de_registratie_na_bevestigen(self, app_server: str, auth_page: Page) -> None:
        _bevestig_registratie(auth_page, app_server)

        auth_page.goto(f"{app_server}/account")
        inhoud = auth_page.content()
        assert "Je hebt een tweede factor ingericht op" in inhoud
        assert "Tweede factor beheren" in inhoud
        assert "Tweede factor inrichten" not in inhoud

    def test_een_verkeerde_bevestigingscode_slaat_niets_op(self, app_server: str, auth_page: Page) -> None:
        _start_registratie(auth_page, app_server)
        auth_page.locator("input[name='code']").fill("000000")
        auth_page.locator("button[type='submit'], input[type='submit']").first.click()
        auth_page.wait_for_load_state("networkidle")

        # Dezelfde QR blijft staan, zodat je het opnieuw kunt proberen.
        assert "Die code klopt niet" in auth_page.content()
        assert auth_page.locator(".otp-qr svg").is_visible()
        auth_page.goto(f"{app_server}{OTP_URL}")
        assert "Nog niet ingericht" in auth_page.content()

    def test_opnieuw_registreren_lukt_niet_zonder_de_oude_code(self, app_server: str, auth_page: Page) -> None:
        _bevestig_registratie(auth_page, app_server)

        auth_page.goto(f"{app_server}{OTP_URL}")
        auth_page.locator("input[name='huidige_code']").fill("000000")
        auth_page.locator("text=Opnieuw inrichten").last.click()
        auth_page.wait_for_load_state("networkidle")

        inhoud = auth_page.content()
        assert "Die code klopt niet" in inhoud
        # De pagina wijst de weg terug: een reset door een beheerder.
        assert "resetten" in inhoud
        assert auth_page.locator(".otp-qr svg").count() == 0

    def test_een_reset_door_een_beheerder_is_de_weg_terug(self, app_server: str, auth_page: Page) -> None:
        """Zonder de code van je huidige app kom je er alleen via een reset.

        Die reset loopt over /admin/users (``tests/test_otp_reset_door_beheerder.py``);
        hier telt wat de gebruiker daarna ziet: een lege stand waarin hij opnieuw
        registreert zonder een oude code.
        """
        _bevestig_registratie(auth_page, app_server)

        _reset_door_een_beheerder()

        auth_page.goto(f"{app_server}{OTP_URL}")
        assert "Nog niet ingericht" in auth_page.content()

        _bevestig_registratie(auth_page, app_server)
        assert "Ingericht" in auth_page.content()
