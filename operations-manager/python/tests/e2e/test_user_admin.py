"""
E2E tests for the user admin CRUD pages.

Verifies that templates render correctly, ROOS components are fully
converted to HTML, forms display and validate, and the CRUD flow works
end-to-end through the real routing/template pipeline (with only the
database-backed service mocked via an in-memory stub).
"""

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import pytest
from opi.services.user_otp_service import OtpStatus, OtpVerification
from tests.e2e.conftest import TEST_USER, _sign_session
from tests.e2e.testserver import get_in_memory_user_service

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from playwright.sync_api import Browser, Page

pytestmark = pytest.mark.e2e

USERS_URL = "/admin/users"
CREATE_URL = "/admin/users/create"


@pytest.fixture
def beheer_page(app_server: str, browser: Browser) -> Generator[Page]:
    """Een pagina waarvan de sessie een VERSE step-up draagt.

    Sinds RC-409 vraagt elke wijziging op /admin/users/* er een, net als de API. Een toets
    die meet wat NA de code komt zet de stempel er zelf bij; de dialoog zelf staat in
    ``TestStepUpInDeBrowser`` hieronder.
    """
    context = browser.new_context()
    cookie = _sign_session({"user": TEST_USER, "step_up_at": datetime.now(UTC).isoformat()})
    host = urlparse(app_server).hostname or "127.0.0.1"
    context.add_cookies([{"name": "session", "value": cookie, "domain": host, "path": "/"}])
    page = context.new_page()
    yield page
    context.close()


@pytest.fixture
def otp_factor() -> Generator[None]:
    """Deze beheerder heeft een factor, en elke code is goed.

    De dienst zelf staat in ``tests/test_user_otp_service.py``; hier gaat het om de
    dialoog en de herhaling erna.
    """
    dienst = MagicMock(
        status=AsyncMock(return_value=OtpStatus(registered=True)),
        verify=AsyncMock(return_value=OtpVerification.OK),
    )
    with (
        patch("opi.web.step_up_dialog.UserOtpService", return_value=dienst),
        patch("opi.web.router_step_up.UserOtpService", return_value=dienst),
    ):
        yield


RAW_ROOS_PATTERN = re.compile(r"<c-[a-z][a-z-]+")


def _assert_no_raw_roos_tags(page: Page, url_label: str) -> None:
    """Assert that no unconverted <c-*> tags remain in the rendered HTML."""
    html = page.content()
    raw_tags = RAW_ROOS_PATTERN.findall(html)
    if raw_tags:
        contexts = []
        for tag in sorted(set(raw_tags)):
            idx = html.find(tag)
            snippet = html[max(0, idx - 60) : idx + 80]
            contexts.append(f"  {tag}: ...{snippet}...")
        pytest.fail(
            f"Raw ROOS component tags found on {url_label}: {sorted(set(raw_tags))}\nContext:\n" + "\n".join(contexts)
        )


#: Het element waarin een veldfout belandt, zie components/_forms.j2 in de LOTC-adapter.
FOUTREGEL = "nldd-validation-item"


def _zichtbare_veldfouten(page: Page) -> str:
    """De tekst van de veldfouten die een beheerder ECHT ziet, in kleine letters.

    Op page.content() asserteren zou ook verborgen DOM meenemen: een fout die in een
    dichte dialoog belandt leest dan als geslaagd. Daarom wachten op zichtbaar.
    """
    page.locator(FOUTREGEL).first.wait_for(state="visible", timeout=5000)
    return " ".join(page.locator(FOUTREGEL).all_inner_texts()).lower()


# ---------------------------------------------------------------------------
# Rendering tests
# ---------------------------------------------------------------------------


class TestUserListPage:
    """User list page at /admin/users."""

    def test_page_loads(self, app_server: str, auth_page: Page) -> None:
        """List page returns 200 and contains expected content."""
        response = auth_page.goto(f"{app_server}{USERS_URL}")
        assert response is not None
        assert response.ok
        assert "Gebruikersbeheer" in auth_page.content()

    def test_no_raw_roos_tags(self, app_server: str, auth_page: Page) -> None:
        """All ROOS components are converted to HTML."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        auth_page.wait_for_load_state("networkidle")
        _assert_no_raw_roos_tags(auth_page, USERS_URL)

    def test_shows_seeded_users(self, app_server: str, auth_page: Page) -> None:
        """Seeded users appear in the table."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        content = auth_page.content()
        assert "Jan de Vries" in content
        assert "Maria Jansen" in content
        assert "jan@example.nl" in content

    def test_has_add_button(self, app_server: str, auth_page: Page) -> None:
        """The 'Gebruiker toevoegen' button is present."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        assert auth_page.locator("text=Gebruiker toevoegen").is_visible()

    def test_has_edit_buttons(self, app_server: str, auth_page: Page) -> None:
        """Each user row has an edit button."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        edit_buttons = auth_page.locator("text=Bewerken")
        assert edit_buttons.count() >= 2  # At least the 2 seeded users

    def test_screenshot(self, app_server: str, auth_page: Page, screenshot_dir: Path) -> None:
        """Screenshot the user list page."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        auth_page.wait_for_load_state("networkidle")
        path = screenshot_dir / "user-admin-list.png"
        auth_page.screenshot(path=str(path))
        assert path.exists()


class TestUserCreatePage:
    """Create user form at /admin/users/create."""

    def test_page_loads(self, app_server: str, auth_page: Page) -> None:
        """Create form returns 200 and contains expected heading."""
        response = auth_page.goto(f"{app_server}{CREATE_URL}")
        assert response is not None
        assert response.ok
        assert "Gebruiker toevoegen" in auth_page.content()

    def test_no_raw_roos_tags(self, app_server: str, auth_page: Page) -> None:
        """All ROOS components are converted to HTML."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.wait_for_load_state("networkidle")
        _assert_no_raw_roos_tags(auth_page, CREATE_URL)

    def test_form_fields_present(self, app_server: str, auth_page: Page) -> None:
        """Email and full_name fields are rendered."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        assert auth_page.locator("input[name='email']").is_visible()
        assert auth_page.locator("input[name='full_name']").is_visible()

    def test_labels_in_dutch(self, app_server: str, auth_page: Page) -> None:
        """Form labels use Dutch text."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        content = auth_page.content()
        assert "E-mailadres" in content
        assert "Volledige naam" in content

    def test_screenshot(self, app_server: str, auth_page: Page, screenshot_dir: Path) -> None:
        """Screenshot the create form."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.wait_for_load_state("networkidle")
        path = screenshot_dir / "user-admin-create.png"
        auth_page.screenshot(path=str(path))
        assert path.exists()


class TestUserCreateValidation:
    """Form validation on the create page."""

    def test_empty_submit_shows_errors(self, app_server: str, beheer_page: Page) -> None:
        """Submitting empty form shows validation errors."""
        beheer_page.goto(f"{app_server}{CREATE_URL}")
        beheer_page.locator("button[type='submit'], input[type='submit']").first.click()

        fouten = _zichtbare_veldfouten(beheer_page)
        # Required fields should trigger errors (Dutch messages)
        assert "verplicht" in fouten or "geldig" in fouten

    def test_invalid_email_shows_error(self, app_server: str, beheer_page: Page) -> None:
        """Submitting an invalid email shows a validation error."""
        beheer_page.goto(f"{app_server}{CREATE_URL}")
        beheer_page.locator("input[name='email']").fill("not-an-email")
        beheer_page.locator("input[name='full_name']").fill("Test User")
        beheer_page.locator("button[type='submit'], input[type='submit']").first.click()

        fouten = _zichtbare_veldfouten(beheer_page)
        assert "geldig e-mailadres" in fouten or "e-mail" in fouten

    def test_short_name_shows_error(self, app_server: str, beheer_page: Page) -> None:
        """Submitting a name shorter than 2 chars shows a validation error."""
        beheer_page.goto(f"{app_server}{CREATE_URL}")
        beheer_page.locator("input[name='email']").fill("valid@example.nl")
        beheer_page.locator("input[name='full_name']").fill("A")
        beheer_page.locator("button[type='submit'], input[type='submit']").first.click()

        assert "minimaal" in _zichtbare_veldfouten(beheer_page)

    def test_validation_preserves_roos_rendering(self, app_server: str, beheer_page: Page) -> None:
        """After validation error, ROOS components still render correctly."""
        beheer_page.goto(f"{app_server}{CREATE_URL}")
        beheer_page.locator("input[name='email']").fill("bad")
        beheer_page.locator("button[type='submit'], input[type='submit']").first.click()
        _zichtbare_veldfouten(beheer_page)
        _assert_no_raw_roos_tags(beheer_page, f"{CREATE_URL} (after validation)")


class TestUserCreateFlow:
    """Full create-and-verify flow."""

    def test_create_user_appears_in_list(self, app_server: str, beheer_page: Page) -> None:
        """Create a user via the form and verify it appears in the list."""
        beheer_page.goto(f"{app_server}{CREATE_URL}")
        beheer_page.locator("input[name='email']").fill("nieuw@example.nl")
        beheer_page.locator("input[name='full_name']").fill("Nieuwe Gebruiker")
        beheer_page.locator("button[type='submit'], input[type='submit']").first.click()

        # De route antwoordt met HX-Redirect, dus de navigatie start NA het antwoord:
        # zonder het wachten op de laadstand leest content() een pagina in beweging.
        beheer_page.wait_for_url(f"**{USERS_URL}?success=*")
        beheer_page.wait_for_load_state("networkidle")
        content = beheer_page.content()
        assert "Nieuwe Gebruiker" in content
        assert "nieuw@example.nl" in content


class TestUserEditPage:
    """Edit user form."""

    def _get_first_user_id(self) -> str:
        service = get_in_memory_user_service()
        # Pick the first user synchronously from the dict
        return next(iter(service._users))

    def test_edit_form_loads_with_data(self, app_server: str, auth_page: Page) -> None:
        """Edit form pre-fills with existing user data."""
        user_id = self._get_first_user_id()
        response = auth_page.goto(f"{app_server}/admin/users/{user_id}/edit")
        assert response is not None
        assert response.ok

        email_value = auth_page.locator("input[name='email']").input_value()
        name_value = auth_page.locator("input[name='full_name']").input_value()
        assert "@" in email_value  # Should be pre-filled
        assert len(name_value) > 0

    def test_edit_form_no_raw_roos_tags(self, app_server: str, auth_page: Page) -> None:
        """Edit form: all ROOS components are converted to HTML."""
        user_id = self._get_first_user_id()
        auth_page.goto(f"{app_server}/admin/users/{user_id}/edit")
        auth_page.wait_for_load_state("networkidle")
        _assert_no_raw_roos_tags(auth_page, f"/admin/users/{user_id}/edit")

    def test_edit_heading(self, app_server: str, auth_page: Page) -> None:
        """Edit form shows correct heading."""
        user_id = self._get_first_user_id()
        auth_page.goto(f"{app_server}/admin/users/{user_id}/edit")
        assert "Gebruiker bewerken" in auth_page.content()

    def test_screenshot(self, app_server: str, auth_page: Page, screenshot_dir: Path) -> None:
        """Screenshot the edit form."""
        user_id = self._get_first_user_id()
        auth_page.goto(f"{app_server}/admin/users/{user_id}/edit")
        auth_page.wait_for_load_state("networkidle")
        path = screenshot_dir / "user-admin-edit.png"
        auth_page.screenshot(path=str(path))
        assert path.exists()


class TestMenuIntegration:
    """Verify the admin menu item appears for admin users."""

    def test_admin_menu_item_visible(self, app_server: str, auth_page: Page) -> None:
        """Gebruikersbeheer menu item is visible on the user admin page."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        assert "Gebruikersbeheer" in auth_page.content()


class TestBevestigingMetVijandigeNaam:
    """Een bevestiging is een dialoog van het thema, en de naam erin is gegeven (RC-287).

    Twee dingen in een meting, omdat ze aan elkaar vastzitten. Er mag geen systeemvenster
    meer komen: ``page.on("dialog")`` vangt elke ``confirm()`` en ``alert()``, en als die
    lijst niet leeg is, is het venster terug. En de naam komt uit Keycloak, dus hij hoort
    letterlijk op het scherm te staan en nergens code te worden. Hij stond eerst in een
    JS-tekenreeks binnen ``onsubmit``, en daar beschermt HTML-escaping niet: de browser
    draait die terug voordat de JS gelezen wordt, dus een apostrof sloot de tekenreeks af.
    Nu is het een gewone attribuutwaarde (``data-bevestig``), waar autoescape wel geldt.
    """

    VIJANDIGE_NAAM = "O'Brien\"><img src=x onerror=alert(1)>"

    def _maak_gebruiker(self, app_server: str, beheer_page: Page, email: str) -> str:
        """Een verse gebruiker met de vijandige naam. Elke toets zijn eigen adres: de
        gebruikersdienst van de testserver leeft zolang de server leeft."""
        beheer_page.goto(f"{app_server}{CREATE_URL}")
        beheer_page.locator("input[name='email']").fill(email)
        beheer_page.locator("input[name='full_name']").fill(self.VIJANDIGE_NAAM)
        beheer_page.locator("button[type='submit'], input[type='submit']").first.click()
        beheer_page.wait_for_url(f"**{USERS_URL}?success=*")

        service = get_in_memory_user_service()
        return next(uid for uid, u in service._users.items() if u["email"] == email)

    def test_elke_bevestiging_is_de_dialoog_van_het_thema(self, app_server: str, beheer_page: Page) -> None:
        """De drie handelingen op deze pagina vragen in <nldd-modal-dialog>, met de naam erin."""
        user_id = self._maak_gebruiker(app_server, beheer_page, "obrien@example.nl")
        assert beheer_page.locator("img[src='x']").count() == 0, "de naam leverde een element op"

        systeemvensters: list[str] = []
        beheer_page.on("dialog", lambda d: (systeemvensters.append(d.message), d.dismiss()))

        dialoog = beheer_page.locator("#bevestiging-dialoog")
        for actie in ("revoke-sessions", "delete", "otp-reset"):
            formulier = beheer_page.locator(f"form[action='/admin/users/{user_id}/{actie}']")
            formulier.locator("button[type='submit']").first.click()
            dialoog.wait_for(state="visible", timeout=5000)
            vraag = dialoog.get_attribute("text") or ""
            assert self.VIJANDIGE_NAAM in vraag, f"{actie}: de naam staat niet in de vraag: {vraag!r}"
            beheer_page.locator("#bevestiging-annuleren").click()
            dialoog.wait_for(state="hidden", timeout=5000)

        assert not systeemvensters, f"er kwam alsnog een systeemvenster: {systeemvensters}"

    def test_annuleren_doet_niets_en_bevestigen_verwijdert(self, app_server: str, beheer_page: Page) -> None:
        """De POST loopt pas na een klik op de bevestigknop."""
        user_id = self._maak_gebruiker(app_server, beheer_page, "obrien-weg@example.nl")
        verwijderen = beheer_page.locator(f"form[action='/admin/users/{user_id}/delete']")
        dialoog = beheer_page.locator("#bevestiging-dialoog")

        verwijderen.locator("button[type='submit']").first.click()
        dialoog.wait_for(state="visible", timeout=5000)
        beheer_page.locator("#bevestiging-annuleren").click()
        dialoog.wait_for(state="hidden", timeout=5000)
        assert get_in_memory_user_service()._users.get(user_id), "annuleren verwijderde de gebruiker alsnog"

        verwijderen.locator("button[type='submit']").first.click()
        dialoog.wait_for(state="visible", timeout=5000)
        beheer_page.locator("#bevestiging-doorgaan").click()
        beheer_page.wait_for_url(f"**{USERS_URL}?success=Gebruiker+verwijderd")
        assert not get_in_memory_user_service()._users.get(user_id), "bevestigen verwijderde de gebruiker niet"


class TestStepUpInDeBrowser:
    """De step-up op deze pagina's, in de weg die een beheerder echt neemt (RC-409).

    Waarom in een browser: de weigering rendert de dialoog ALLEEN op een HX-Request en
    werpt anders een 403. Of de beheerder bij een invulbaar codeveld komt hangt dus aan
    drie dingen die niet uit de HTML blijken: dat de knop van het thema via htmx post, dat
    het antwoord met HX-Retarget in #edit-section-inner landt, en dat edit_modal.js de
    dialoog dan opent. Een van de drie stuk is een kale 403 en een geblokkeerde handeling.
    """

    @staticmethod
    def _codeveld(page: Page) -> object:
        page.wait_for_selector(".edit-section-modal.is-open", timeout=10000)
        dialoog = page.locator("#edit-section-inner")
        dialoog.locator("[data-step-up]").wait_for(state="visible", timeout=10000)
        return dialoog.locator("#edit-section-inner input[name='otp'], input[name='otp']").first

    def test_het_formulier_komt_bij_een_invulbaar_codeveld(
        self, app_server: str, auth_page: Page, otp_factor: None
    ) -> None:
        """Zonder stempel: de dialoog komt in beeld en de code is er in te vullen."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.locator("input[name='email']").fill("via-dialoog@example.nl")
        auth_page.locator("input[name='full_name']").fill("Via Dialoog")
        auth_page.locator("button[type='submit'], input[type='submit']").first.click()

        code = self._codeveld(auth_page)
        assert code.is_visible(), "geen invulbaar codeveld, dus de handeling is geblokkeerd"
        assert "een gebruiker aan het portaal toe te voegen" in auth_page.locator("#edit-section-inner").inner_text()

    def test_na_de_code_maakt_het_formulier_de_gebruiker_alsnog_aan(
        self, app_server: str, auth_page: Page, otp_factor: None
    ) -> None:
        """De herhaling stuurt de ingevulde velden mee, dus niemand typt zijn formulier opnieuw."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.locator("input[name='email']").fill("na-de-code@example.nl")
        auth_page.locator("input[name='full_name']").fill("Na De Code")
        auth_page.locator("button[type='submit'], input[type='submit']").first.click()

        self._codeveld(auth_page).fill("000000")
        auth_page.locator("#edit-section-inner button:has-text('Bevestigen')").first.click()

        auth_page.wait_for_url(f"**{USERS_URL}?success=*", timeout=15000)
        auth_page.wait_for_load_state("networkidle")
        assert "Na De Code" in auth_page.content()

    def test_een_veldfout_na_de_code_staat_zichtbaar_in_de_dialoog(
        self, app_server: str, auth_page: Page, otp_factor: None
    ) -> None:
        """De andere helft van de weg: de herhaling keurt een veld af en dat moet te zien zijn."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.locator("input[name='email']").fill("not-an-email")
        auth_page.locator("input[name='full_name']").fill("Foute Invoer")
        auth_page.locator("button[type='submit'], input[type='submit']").first.click()

        self._codeveld(auth_page).fill("000000")
        auth_page.locator("#edit-section-inner button:has-text('Bevestigen')").first.click()

        fout = auth_page.locator(f"#edit-section-inner {FOUTREGEL}").first
        fout.wait_for(state="visible", timeout=10000)
        assert "geldig" in fout.inner_text().lower()

    def test_een_knoproute_komt_ook_bij_de_dialoog(self, app_server: str, auth_page: Page, otp_factor: None) -> None:
        """De drie knopformulieren op de lijst hebben geen body, en dezelfde weg."""
        user_id = next(iter(get_in_memory_user_service()._users))
        auth_page.goto(f"{app_server}{USERS_URL}")

        formulier = auth_page.locator(f"form[action='/admin/users/{user_id}/revoke-sessions']")
        formulier.locator("button[type='submit']").first.click()
        auth_page.locator("#bevestiging-doorgaan").click()

        assert self._codeveld(auth_page).is_visible()
