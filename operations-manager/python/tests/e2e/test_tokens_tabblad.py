"""Het tabblad Team toont de leden, hun tokens en de formulieren eromheen.

Waarom een browsertest en niet alleen een rendertest: de velden zijn NLDD-componenten
(``nldd-text-input-field``, ``nldd-select-field``), en of zo'n custom element zijn waarde
meestuurt in een gewoon formulier is niet uit de HTML af te lezen. Dat is precies de fout
die een groene rendertest zou missen: het formulier lijkt te werken en de server antwoordt
"geef het token een korte naam".
"""

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.tabs import open_tab
from tests.e2e.helpers.tekst import toon_tekst

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
DETAIL_URL = f"/projects/{PROJECT}/details"


def _open_team(app_server: str, page: Page) -> None:
    page.goto(f"{app_server}{DETAIL_URL}")
    page.wait_for_load_state("networkidle")
    open_tab(page, "team")


def test_het_tabblad_team_toont_de_leden_en_hun_tokens(app_server: str, auth_page: Page) -> None:
    _open_team(app_server, auth_page)

    toon_tekst(auth_page, "Leden en tokens")


def test_het_formulier_voor_een_eigen_token_staat_er(app_server: str, auth_page: Page) -> None:
    formulieren = _tokenformulieren(app_server, auth_page)
    assert formulieren.count() >= 1
    assert formulieren.first.locator('input[name="csrf_token"]').count() == 1


def test_de_naam_en_de_dagen_reizen_mee_in_de_verzending(app_server: str, auth_page: Page) -> None:
    """De eigenschap waar de keuze op staat of valt: komen de velden mee in de POST?

    Een custom element dat niet form-associated is stuurt niets mee, en dan weigert de
    server een formulier dat op het scherm ingevuld lijkt.
    """
    formulieren = _tokenformulieren(app_server, auth_page)
    formulier = formulieren.first
    # Het uitgifteformulier staat ingeklapt: een lid is anders drie formulieren hoog.
    auth_page.locator("summary", has_text="Een token uitgeven").first.click()
    formulier.locator('[name="tokennaam"] input').first.fill("pijplijn")

    verzonden = auth_page.evaluate(
        """
        () => {
            const f = document.querySelector('form[action$="/tokens"]');
            return Object.fromEntries(new FormData(f).entries());
        }
        """
    )

    assert verzonden.get("tokennaam") == "pijplijn"
    assert verzonden.get("dagen"), "de vervaldatum is verplicht en hoort dus mee te gaan"


def test_de_formuliervelden_hebben_unieke_ids(app_server: str, auth_page: Page) -> None:
    """Twee leden met tokens mogen niet dezelfde veld-id krijgen.

    De roteerformulieren staan in de tokenlus, dus een nummer uit die lus is per lid
    hetzelfde: dan wijst `aria-describedby` van het ene lid naar de hulptekst van het
    andere.
    """
    _open_team(app_server, auth_page)

    ids = auth_page.evaluate(
        """
        () => Array.from(document.querySelectorAll('[id]'))
            .map(e => e.id)
            .filter(i => i.startsWith('dagen-') || i.startsWith('tokennaam-'))
        """
    )
    dubbel = sorted({i for i in ids if ids.count(i) > 1})
    assert not dubbel, f"deze veld-ids komen meer dan een keer voor: {dubbel}"


def _tokenformulieren(app_server: str, page: Page):
    _open_team(app_server, page)
    return page.locator(f'form[action^="/projects/{PROJECT}/leden/"][action$="/tokens"]')
