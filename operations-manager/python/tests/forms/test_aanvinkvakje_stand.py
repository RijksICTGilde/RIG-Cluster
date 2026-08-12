"""De STAND van een aanvinkvakje: wat er getekend wordt, en wat er opgeslagen wordt.

WAT ER MISGING

In de wizard stond "Markeer voor verwijdering" bij een databaseschema AAN, terwijl het
projectbestand ``marked-for-deletion: false`` zei (en het model die waarde als standaard
heeft). Een vinkje dat aanstaat terwijl het uit hoort is geen schoonheidsfoutje: markeren
stopt het beheer van dat schema en verbergt zijn variabele, dus het scherm beweerde iets
over de toestand van de database dat niet waar was.

DE OORZAAK

``editable_to_form_field`` koos per widget tussen ``converter.read()`` (de waarde zoals
een formulier hem draagt) en ``converter.view()`` (de waarde zoals een MENS hem leest).
Een aanvinkvakje stond in geen van beide lijstjes en viel in de view-tak. Een
BooleanConverter levert daar "Ja" of "Nee" op, en het sjabloon toetst
``:checked="field.value"``. "Nee" is een niet-lege tekst, dus waar. Elk aanvinkvakje met
een converter stond aan: het schemavinkje, en "Versiebeheer op de bucket" bij minio.

Niet de htmx-kant deze keer (dat was RC-71, zie features/aanvinkvakje.md) maar de
tekenkant. De twee zijn los van elkaar te toetsen en dat gebeurt hier ook: eerst wat er
in het formulier komt te staan, dan wat een inzending in het projectbestand achterlaat.

HOORT DAT VINKJE ER UBERHAUPT WEL?

Ja, en de reden staat in features/postgresql-scope-and-schemas.md. Kort: in de wizard is
een rij uit de lijst halen een ECHTE verwijdering (de reeksverwerker schrijft de
ingediende lijst weg), terwijl "weghalen = markeren" de afspraak van de API is. Het
vinkje is dus de enige manier waarop een wizardgebruiker een schema kan markeren, en
daarmee een keuze die hij maakt - geen gevolg van iets anders.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest
from opi.core.templates_lotc import templates_lotc
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.bridge import editable_to_form_field
from opi.services.catalog.minio.visualizers import MINIO_ENABLE_VERSIONING
from opi.services.catalog.postgresql_database.visualizers import POSTGRESQL_SCHEMAS, SCHEMA_MARKED


def _project(schemas: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": "proj",
        "services": [{"name": "postgresql-database", "config": {"schemas": schemas}}],
        "deployments": [{"name": "dep"}],
    }


def _vakje_html(veld: Any) -> str:
    sjabloon = templates_lotc.env.get_template("widgets/checkbox.html.j2")
    return sjabloon.render(field=veld, field_attrs=lambda _f: {})


# --- wat er getekend wordt --------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "verwacht"),
    [
        ({"postfix": "a", "marked-for-deletion": False}, False),
        ({"postfix": "a"}, False),
        ({"postfix": "a", "marked-for-deletion": True}, True),
    ],
    ids=["expliciet-false", "helemaal-afwezig", "gemarkeerd"],
)
def test_het_vinkje_volgt_de_opgeslagen_waarde(entry: dict[str, Any], verwacht: bool) -> None:
    veld = editable_to_form_field(SCHEMA_MARKED, _project([entry]), index=0)

    assert veld.value is verwacht
    assert (" checked" in _vakje_html(veld)) is verwacht


def test_een_vakje_draagt_een_boolean_en_geen_weergave() -> None:
    """De kern van de fout: "Nee" is waar.

    Deze test valt om zodra iemand de weergavewaarde terugzet, ook als het sjabloon
    intussen anders toetst.
    """
    veld = editable_to_form_field(SCHEMA_MARKED, _project([{"postfix": "a", "marked-for-deletion": False}]), index=0)

    assert veld.value is False
    assert veld.value not in ("Nee", "false", "")


def test_hetzelfde_geldt_voor_het_versiebeheervakje_van_minio() -> None:
    """Dezelfde converter, hetzelfde vakje, dezelfde fout - buiten de schemalijst."""
    data = {"services": [{"name": "minio-storage", "config": {"enable-versioning": False}}]}
    uit = editable_to_form_field(MINIO_ENABLE_VERSIONING, data)
    aan = editable_to_form_field(
        MINIO_ENABLE_VERSIONING, {"services": [{"name": "minio-storage", "config": {"enable-versioning": True}}]}
    )

    assert uit.value is False
    assert aan.value is True


# --- wat een inzending achterlaat --------------------------------------------------


async def _opslaan(schemas: list[dict[str, Any]], ingediend: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dien de schemalijst in zoals het formulier hem stuurt en geef het resultaat terug."""
    resultaat, fouten = await EditableFormProcessor().process_json_submission(
        {"_services-config": {"postgresql-database": {"config": {"schemas": ingediend}}}},
        [POSTGRESQL_SCHEMAS],
        copy.deepcopy(_project(schemas)),
        edit_mode=True,
    )
    assert not fouten, f"onverwachte validatiefouten: {fouten}"
    return resultaat["services"][0]["config"]["schemas"]


@pytest.mark.asyncio
async def test_een_gemarkeerd_schema_blijft_gemarkeerd_na_opslaan() -> None:
    """Het vakje staat aan, gaat als "true" mee, en het schema blijft gemarkeerd."""
    schemas = await _opslaan(
        [{"postfix": "rapportage", "description": "oud", "marked-for-deletion": True}],
        [{"postfix": "rapportage", "description": "oud", "marked-for-deletion": "true"}],
    )

    assert schemas[0]["marked-for-deletion"] is True


@pytest.mark.asyncio
async def test_een_ongemarkeerd_schema_raakt_niet_per_ongeluk_gemarkeerd() -> None:
    """Een vakje dat uitstaat stuurt zijn sleutel niet mee; dat mag geen mark opleveren.

    Dit is de kant die RC-71 in de browser rechtzette (htmx stuurde een uitgevinkt
    form-associated vakje toch als "true" mee). Hier staat de serverkant ervan vast: geen
    sleutel betekent uit, ook binnen een reeks.
    """
    schemas = await _opslaan(
        [{"postfix": "rapportage", "description": "in gebruik"}],
        [{"postfix": "rapportage", "description": "in gebruik"}],
    )

    assert schemas[0].get("marked-for-deletion") is not True


@pytest.mark.asyncio
async def test_een_mark_uitzetten_haalt_hem_weg() -> None:
    """De andere richting: het vakje uitvinken maakt het schema weer gewoon."""
    schemas = await _opslaan(
        [{"postfix": "rapportage", "description": "terug", "marked-for-deletion": True}],
        [{"postfix": "rapportage", "description": "terug"}],
    )

    assert schemas[0].get("marked-for-deletion") is not True


@pytest.mark.asyncio
async def test_de_rij_zelf_blijft_staan_met_zijn_omschrijving() -> None:
    """Markeren is geen verwijderen: de rij en zijn omschrijving blijven in het bestand.

    Dat is de hele reden dat het veld bestaat (RC-17): een gemarkeerd schema en zijn data
    blijven staan, alleen de variabele wordt niet meer aangeboden.
    """
    schemas = await _opslaan(
        [{"postfix": "rapportage", "description": "kwartaalcijfers"}],
        [{"postfix": "rapportage", "description": "kwartaalcijfers", "marked-for-deletion": "true"}],
    )

    assert len(schemas) == 1
    assert schemas[0]["postfix"] == "rapportage"
    assert schemas[0]["description"] == "kwartaalcijfers"
    assert schemas[0]["marked-for-deletion"] is True


# --- een id per element ------------------------------------------------------------


def _ids(html: str) -> list[str]:
    return re.findall(r'\bid="([^"]*)"', html)


def test_het_vakje_en_zijn_omhulling_dragen_niet_dezelfde_id() -> None:
    """Waarom ``[id='<pad>']`` er precies een oplevert, en niet twee.

    Sinds LOTC ``762e570`` zet het component de id zelf op het besturingselement in plaats
    van op de omhulling. Zetten wij hem daarnaast ook nog via ``:attrs``, dan stonden er
    twee elementen met dezelfde id in de pagina: ongeldige HTML, en de gedocumenteerde
    selector (features/aanvinkvakje.md) leverde er twee op. Precies daar liep de
    toetsenbordtest op vast.
    """
    veld = editable_to_form_field(SCHEMA_MARKED, _project([{"postfix": "a"}]), index=0)
    html = _vakje_html(veld)
    ids = _ids(html)

    assert ids.count(veld.path) == 1
    assert len(ids) == len(set(ids)), f"dubbele id in het vakje: {ids}"


def test_de_hulptekst_hangt_nog_aan_het_veld() -> None:
    """De hulptekst houdt zijn afgeleide id: het component stelt hem uit de prop samen."""
    veld = editable_to_form_field(SCHEMA_MARKED, _project([{"postfix": "a"}]), index=0)
    html = _vakje_html(veld)

    assert f'id="{veld.path}-help"' in html
