"""De telling van de database `keycloak` geeft aantallen en nooit persoonsgegevens (RC-411).

De uitvoer van `task keycloak-telling` wordt vergeleken rond een versiesprong en belandt
daarmee in een PR of een ticket. Een kolom die per ongeluk een gebruikersnaam of een hash
meeneemt zou daar dus in meekomen, en dat valt bij het lezen niet op tussen de getallen.
"""

import re
from pathlib import Path

import pytest

TELLING_SQL = Path(__file__).resolve().parents[3] / "keycloak-migration/telling.sql"

#: Kolommen uit het Keycloak-schema die een persoon of een geheim dragen. Ze mogen nergens
#: in het bestand staan, ook niet in een WHERE of een JOIN.
VERBODEN_KOLOMMEN = (
    "email",
    "email_constraint",
    "username",
    "first_name",
    "last_name",
    "secret_data",
    "credential_data",
    "salt",
    "user_label",
    "federated_username",
    "token",
    "value",
)

#: Wat een selectie-item mag zijn zonder in de GROUP BY te staan.
AGGREGATEN = ("count", "sum", "min", "max", "avg")


def _zonder_commentaar() -> str:
    regels = [re.sub(r"--.*$", "", regel) for regel in TELLING_SQL.read_text().splitlines()]
    return "\n".join(regels)


def _selecties() -> list[tuple[str, str]]:
    """Per SELECT in het bestand: de selectielijst en de GROUP BY die erbij hoort."""
    sql = _zonder_commentaar()
    treffers = list(re.finditer(r"\bSELECT\b(.*?)\bFROM\b", sql, re.DOTALL | re.IGNORECASE))
    assert treffers, "geen enkele SELECT gevonden in telling.sql"
    selecties = []
    for nummer, treffer in enumerate(treffers):
        tot = treffers[nummer + 1].start() if nummer + 1 < len(treffers) else len(sql)
        romp = sql[treffer.end() : tot]
        group_by = re.search(r"\bGROUP BY\b(.*?)(?:\bUNION\b|\bORDER BY\b|\)|$)", romp, re.DOTALL | re.IGNORECASE)
        selecties.append((treffer.group(1), group_by.group(1) if group_by else ""))
    return selecties


def _items(selectielijst: str) -> list[str]:
    """De items van een selectielijst, gesplitst op de komma's buiten haakjes."""
    items: list[str] = []
    diepte = 0
    huidig = ""
    for teken in selectielijst:
        if teken == "(":
            diepte += 1
        elif teken == ")":
            diepte -= 1
        if teken == "," and diepte == 0:
            items.append(huidig)
            huidig = ""
            continue
        huidig += teken
    items.append(huidig)
    return [item.strip() for item in items if item.strip()]


@pytest.mark.parametrize("kolom", VERBODEN_KOLOMMEN)
def test_geen_persoonsgegeven_in_het_bestand(kolom: str) -> None:
    assert not re.search(rf"\b{kolom}\b", _zonder_commentaar(), re.IGNORECASE), kolom


def test_elk_selectie_item_is_een_aggregaat_een_literal_of_een_groeperingssleutel() -> None:
    """Zo kan er geen kolom in de uitvoer komen die niet tot een getal is samengevat."""
    for selectielijst, group_by in _selecties():
        sleutels = {sleutel.strip().lower() for sleutel in group_by.split(",") if sleutel.strip()}
        for item in _items(selectielijst):
            uitdrukking = re.sub(r"\s+AS\s+\w+$", "", item, flags=re.IGNORECASE).strip()
            if uitdrukking == "*" or uitdrukking.startswith("'"):
                continue
            if uitdrukking.lower().startswith(AGGREGATEN):
                continue
            assert uitdrukking.lower() in sleutels, f"{uitdrukking!r} staat niet in GROUP BY {sorted(sleutels)}"
