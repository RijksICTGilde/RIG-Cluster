"""Het hele rondje van een aanvinkvakje: aan, opslaan, heropenen, uit, opslaan, heropenen.

Wat hier misging en wat geen enkele test dekte: alles toetste AANzetten, niets toetste
UITzetten. Gemeld op "Toegang beperken" in de keycloak-configstap - aanvinken lukte,
uitvinken niet, en het vakje sprong terug.

De oorzaak zat in de serialisatie, in de browser gemeten (zie features/aanvinkvakje.md):
htmx verzamelt zijn parameters zelf uit ``form.elements`` en leest daar ``.value``, met
een uitzondering voor ``type="checkbox"``. Het aanvinkvakje van het thema is een
form-associated custom element zonder ``.type``, dus die uitzondering sloeg niet aan en
htmx stuurde ``"true"`` mee ongeacht de stand.

Daarom toetst dit bestand op twee niveaus:

- wat er over de lijn gaat (``test_*_stuurt_*``): de directe toets op de oorzaak, en de
  enige plek waar zichtbaar is dat het aan de VERZENDKANT zat;
- het hele rondje tot in het projectbestand (``test_*_hele_rondje``): dat is wat de
  gebruiker meldde, en het bewijst dat de rest van de keten het uitvinken ook overbrengt.

De eerste twee op BEIDE vormen - een enkel vakje en een groep - want die horen zich sinds
RC-71 hetzelfde te gedragen: allebei ``<c-checkbox-field>``.

Run: uv run pytest tests/e2e/test_aanvinkvakje.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from opi.services.project_service import get_project_service
from tests.e2e.helpers.edit_modal import EditModalHelper
from tests.e2e.helpers.htmx import wait_for_htmx_quiet
from tests.e2e.helpers.service_config import modal_advance_to_field
from tests.e2e.helpers.wizard import WizardHelper, aanvinkvakje, aanvinkvakjes

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

#: Het gemelde vakje: een enkel aanvinkvakje in de keycloak-config. Het pad is het
#: VIRTUELE pad waaronder de dienstconfig wordt verstuurd.
ENKEL = "_services-config/keycloak/config/restrict-access/enabled"
#: Een groep aanvinkvakjes: de diensten die een component gebruikt.
GROEP = "components[0]/services"

PROJECT = "test-project-detail"


# ---------------------------------------------------------------------------
# Wat er over de lijn gaat
# ---------------------------------------------------------------------------


class _Verzonden:
    """Verzamelt de JSON die de wizard naar de server stuurt."""

    def __init__(self, page: Page) -> None:
        self.bodies: list[dict[str, Any]] = []
        page.on("request", self._noteer)

    def _noteer(self, request: Any) -> None:
        if request.method != "POST" or "/step/" not in request.url:
            return
        data = request.post_data
        if not data:
            return
        try:
            self.bodies.append(json.loads(data))
        except ValueError:
            return

    def laatste(self) -> dict[str, Any]:
        assert self.bodies, "er is geen stap-POST verstuurd"
        return self.bodies[-1]

    def wis(self) -> None:
        self.bodies.clear()


def _plat(data: Any, prefix: str = "") -> dict[str, Any]:
    """De geneste JSON als platte pad -> waarde, zodat een sleutel opzoekbaar is."""
    plat: dict[str, Any] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            plat.update(_plat(value, f"{prefix}/{key}" if prefix else str(key)))
    elif isinstance(data, list) and any(isinstance(item, (dict, list)) for item in data):
        for i, item in enumerate(data):
            plat.update(_plat(item, f"{prefix}[{i}]"))
    else:
        plat[prefix] = data
    return plat


def _sleutels_met(body: dict[str, Any], fragment: str) -> dict[str, Any]:
    return {pad: waarde for pad, waarde in _plat(body).items() if fragment in pad}


def test_enkel_vakje_stuurt_niets_mee_als_het_uit_staat(app_server: str, auth_page: Page) -> None:
    """Aanvinken stuurt de waarde mee, uitvinken stuurt de sleutel NIET mee.

    Dit is de toets op de oorzaak. Voor de oplossing stond hier bij uitvinken
    ``enabled: "true"`` in de body - dezelfde waarde als bij aanvinken.
    """
    page = auth_page
    wizard = WizardHelper(page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(description="aanvinkvakje")
    wizard.click_next()
    wizard.fill_services(["keycloak"])
    wizard.click_next()
    page.wait_for_load_state("networkidle")
    wait_for_htmx_quiet(page)

    assert aanvinkvakje(page, ENKEL).count() == 1, f"verwacht precies een vakje voor {ENKEL}"

    verzonden = _Verzonden(page)
    aanvinkvakje(page, ENKEL).click()
    wait_for_htmx_quiet(page)
    aan = _sleutels_met(verzonden.laatste(), "restrict-access/enabled")
    assert aan, f"aangevinkt hoort de sleutel mee te gaan, body droeg: {verzonden.laatste()}"
    assert list(aan.values()) == ["true"], f"verwacht 'true', kreeg {aan}"

    verzonden.wis()
    aanvinkvakje(page, ENKEL).click()
    wait_for_htmx_quiet(page)
    uit = _sleutels_met(verzonden.laatste(), "restrict-access/enabled")
    assert not uit, f"uitgevinkt hoort de sleutel NIET mee te gaan, maar body droeg: {uit}"


def test_groep_stuurt_alleen_de_aangevinkte_keuzes_mee(app_server: str, auth_page: Page) -> None:
    """Dezelfde regel voor een groep: alleen wat aanstaat gaat mee, leeg is leeg.

    Een groep is sinds RC-71 dezelfde componentvorm als een enkel vakje, dus als de
    serialisatie voor de een stukgaat gaat hij voor de ander ook stuk.
    """
    page = auth_page
    wizard = WizardHelper(page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(description="aanvinkvakje groep")
    wizard.click_next()
    wizard.fill_services(["keycloak"])
    wizard.click_next()
    for _ in range(8):
        if aanvinkvakjes(page, GROEP).count():
            break
        wizard.click_next()
        wait_for_htmx_quiet(page)
    vakjes = aanvinkvakjes(page, GROEP)
    assert vakjes.count() > 0, "de groep 'Gebruikte services' is niet in beeld gekomen"

    aangevinkt = [vakjes.nth(i) for i in range(vakjes.count()) if vakjes.nth(i).evaluate("el => !!el.checked")]
    assert aangevinkt, "de groep begint met niets aangevinkt; dan valt er niets uit te vinken"

    verzonden = _Verzonden(page)
    for vakje in aangevinkt:
        vakje.click()
        wait_for_htmx_quiet(page)

    verstuurd = _sleutels_met(verzonden.laatste(), "services")
    keuzes = {pad: waarde for pad, waarde in verstuurd.items() if "components" in pad}
    assert not keuzes, f"alles uitgevinkt hoort geen keuze mee te sturen, maar body droeg: {keuzes}"


# ---------------------------------------------------------------------------
# Het hele rondje, tot in het projectbestand
# ---------------------------------------------------------------------------


def _projectgegevens(naam: str = PROJECT) -> dict[str, Any]:
    """De opgeslagen projectgegevens, uit de store die de app zelf gebruikt.

    De testserver draait in dit proces, dus dit is het projectbestand zoals het is
    opgeslagen - en niet het antwoord van de pagina die we net indienden.
    """
    summary = get_project_service().get_project(naam)
    assert summary is not None, f"project {naam} niet gevonden"
    assert summary.data is not None, f"project {naam} heeft geen gegevens"
    return summary.data


def _keycloak_config(data: dict[str, Any]) -> dict[str, Any]:
    """De keycloak-config uit het projectbestand, in beide opslagvormen.

    Een dienst met config staat er als record (``{"name": "keycloak", "config": {...}}``)
    of in de oudere enkelsleutelvorm (``{"keycloak": {"config": {...}}}``). Op een van de
    twee toetsen leest als "er staat niets" terwijl er wel degelijk iets staat.
    """
    for entry in data.get("services", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("name") == "keycloak":
            return entry.get("config", {}) or {}
        if "keycloak" in entry:
            return (entry["keycloak"] or {}).get("config", {}) or {}
    return {}


def _open_keycloak_config(page: Page, base_url: str) -> EditModalHelper:
    """Open de keycloak-configdialoog zoals een gebruiker dat doet: via het tabblad.

    De knop "Configureer" staat op de dienstenkaart, en die kaarten staan op het tabblad
    Services - niet op de landingspagina van het project.
    """
    modal = EditModalHelper(page, base_url, PROJECT)
    page.goto(f"/projects/services/{modal.project_name}", wait_until="networkidle")
    modal.open_edit_modal("modal-edit-keycloak-config", "Keycloak Authentication configuratie")
    assert modal_advance_to_field(page, "restrict-access/enabled"), "de stap met 'Toegang beperken' is niet bereikt"
    return modal


def _sla_op(modal: EditModalHelper, max_stappen: int = 6) -> None:
    """Dien de dialoog in tot hij dicht is: de config kan meer dan een stap tellen."""
    for _ in range(max_stappen):
        if not modal.page.locator("#edit-section-modal.is-open").count():
            return
        if not modal.page.locator("#modal-wizard-form button[type='submit']").count():
            return
        modal.submit_step()
        wait_for_htmx_quiet(modal.page)


def _staat_aan(page: Page) -> bool:
    vakje = aanvinkvakje(page, ENKEL)
    assert vakje.count() == 1, f"verwacht precies een vakje voor {ENKEL}, kreeg {vakje.count()}"
    return bool(vakje.evaluate("el => !!el.checked"))


@pytest.fixture
def herstel_keycloak() -> Iterator[None]:
    """Zet restrict-access terug zoals het was: het project is met andere tests gedeeld."""
    voor = json.loads(json.dumps(_keycloak_config(_projectgegevens()).get("restrict-access")))
    yield
    config = _keycloak_config(_projectgegevens())
    if voor is None:
        config.pop("restrict-access", None)
    else:
        config["restrict-access"] = voor


def test_enkel_vakje_hele_rondje(app_server: str, auth_page: Page, herstel_keycloak: None) -> None:
    """Aan, opslaan, heropenen, uit, opslaan, heropenen - en dan is de waarde weg.

    De tweede helft is wat er misging: het vakje stond na opslaan weer aan, en
    ``restrict-access/enabled`` bleef in het projectbestand staan.
    """
    page = auth_page

    # 1. aanvinken en opslaan
    modal = _open_keycloak_config(page, app_server)
    if not _staat_aan(page):
        aanvinkvakje(page, ENKEL).click()
        wait_for_htmx_quiet(page)
    _sla_op(modal)

    # 2. heropenen: het vakje staat aan, en de waarde staat in het projectbestand
    modal = _open_keycloak_config(page, app_server)
    assert _staat_aan(page), "na opslaan hoort het vakje aan te staan"
    restrict = _keycloak_config(_projectgegevens()).get("restrict-access", {})
    assert restrict.get("enabled") is True, f"verwacht enabled: true in het projectbestand, kreeg {restrict}"

    # 3. uitvinken en opslaan
    aanvinkvakje(page, ENKEL).click()
    wait_for_htmx_quiet(page)
    assert not _staat_aan(page), "de klik heeft het vakje niet uitgezet"
    _sla_op(modal)

    # 4. heropenen: het vakje staat uit, en de waarde is uit het projectbestand weg
    _open_keycloak_config(page, app_server)
    assert not _staat_aan(page), "na uitvinken en opslaan hoort het vakje UIT te staan"
    restrict = _keycloak_config(_projectgegevens()).get("restrict-access", {})
    assert not restrict.get("enabled"), f"'enabled' hoort uit het projectbestand te zijn, maar er staat: {restrict}"
