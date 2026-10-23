"""Een lijstmodal die maar een deel van zijn lijst toont.

Een modal op een lijst (de componenten van een project, de componenten binnen een
deployment) rendert normaal elk item. Met een selectie rendert hij alleen de genoemde
items, en bij het opslaan worden die teruggezet op hun plek in de opgeslagen lijst.

Wat niet geselecteerd is, staat NIET in het formulier: niet zichtbaar en ook niet als
verborgen veld. Het komt bij het opslaan uit het projectbestand zoals dat op dat moment
in git staat, dus een niet-getoond item kan niet overschreven of weggehaald worden.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.service_path import smart_get_value, smart_set_value

if TYPE_CHECKING:
    from opi.forms.visualizers.flows import ListSelectable


def parse_selection(raw: str | None) -> list[str]:
    """De namen uit ``?components=a,b``, zonder lege namen en zonder dubbelen."""
    if not raw:
        return []
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return names


def selectable_names(project_data: dict[str, Any], selectable: ListSelectable) -> list[str]:
    """De namen die de lijst van *selectable* nu bevat."""
    stored = smart_get_value(project_data, selectable.list_path)
    if not isinstance(stored, list):
        return []
    return [
        item.get(selectable.key_field) for item in stored if isinstance(item, dict) and item.get(selectable.key_field)
    ]


def narrow_to_selection(
    project_data: dict[str, Any],
    selectable: ListSelectable,
    names: list[str],
) -> dict[str, Any]:
    """Een kopie van *project_data* waarin de lijst alleen de geselecteerde items heeft.

    De volgorde is die van het projectbestand, niet die van de URL.
    """
    data = copy.deepcopy(project_data)
    stored = smart_get_value(data, selectable.list_path)
    if not isinstance(stored, list):
        return data
    chosen = set(names)
    subset = [item for item in stored if isinstance(item, dict) and item.get(selectable.key_field) in chosen]
    smart_set_value(data, selectable.list_path, subset)
    return data


def splice_selection(
    existing_data: dict[str, Any],
    merged_data: dict[str, Any],
    selectable: ListSelectable,
    names: list[str],
) -> None:
    """Zet de getoonde items terug op hun eigen plek in de opgeslagen lijst.

    De niet-getoonde items komen uit *existing_data* (vers uit git) en blijven staan zoals
    ze zijn, op hun eigen plek. Een getoond item keert terug op zijn EIGEN plek, gepaard op
    ``key_field``: wat de gebruiker weghaalde laat zijn plek leeg, wat hij toevoegde komt
    achteraan. Op positie paren schoof de niet-getoonde items op zodra het eerste item van
    een selectie van twee wegviel.

    Weigert de opslag als een geselecteerde naam intussen uit het projectbestand is
    verdwenen: dan is de lijst onder de modal veranderd en zegt het formulier iets over
    een item dat er niet meer is (dezelfde reden als
    ``guard_target_still_points_at_the_same_item``).
    """
    submitted = smart_get_value(merged_data, selectable.list_path)
    stored = smart_get_value(existing_data, selectable.list_path)
    if not isinstance(submitted, list) or not isinstance(stored, list):
        return

    chosen = set(names)
    found = {
        item.get(selectable.key_field)
        for item in stored
        if isinstance(item, dict) and item.get(selectable.key_field) in chosen
    }
    missing = [name for name in names if name not in found]
    if missing:
        raise ProjectIntegrityError(
            f"'{', '.join(missing)}' staat niet meer in dit project: er is iets gewijzigd terwijl je aan het "
            "bewerken was. Sluit dit scherm en open het opnieuw, dan werk je weer op de huidige lijst."
        )

    getoond: dict[Any, Any] = {}
    toegevoegd: list[Any] = []
    for item in submitted:
        naam = item.get(selectable.key_field) if isinstance(item, dict) else None
        if naam in chosen and naam not in getoond:
            getoond[naam] = item
        else:
            toegevoegd.append(item)

    resultaat: list[Any] = []
    for item in stored:
        naam = item.get(selectable.key_field) if isinstance(item, dict) else None
        if naam in chosen:
            if naam in getoond:
                resultaat.append(getoond[naam])
        else:
            resultaat.append(item)
    resultaat.extend(toegevoegd)
    smart_set_value(merged_data, selectable.list_path, resultaat)
