"""De weg terug: een registry verdwijnt niet onder een component vandaan.

Zie features/image-registries.md, "De weg terug is geen stille weg". Naast
``values_must_exist``, want die slaat een LEGE keuzelijst met opzet over, en leeg is precies
de toestand die hier ontstaat.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.image_registries.resolution import component_registry_name, project_registries


def validate_registry_references(project_data: dict[str, Any]) -> list[str]:
    """Weiger een projectbestand waarin een component naar een registry wijst die er niet
    (meer) is, met de plekken erbij die hem noemen."""
    beschikbaar = {registry["name"] for registry in project_registries(project_data)}
    gebruikt: dict[str, list[str]] = {}

    for component in project_data.get("components", []) or []:
        naam = component_registry_name(component) if isinstance(component, dict) else None
        if naam and naam not in beschikbaar:
            gebruikt.setdefault(naam, []).append(f"component '{component.get('name', 'onbekend')}'")

    for deployment in project_data.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        for component in deployment.get("components", []) or []:
            naam = component_registry_name(component) if isinstance(component, dict) else None
            if naam and naam not in beschikbaar:
                plek = f"deployment '{deployment.get('name', 'onbekend')}', component '{component.get('reference', 'onbekend')}'"
                gebruikt.setdefault(naam, []).append(plek)

    return [
        f"Registry '{naam}' bestaat niet (meer) in dit project, maar wordt nog gebruikt door: "
        f"{', '.join(plekken)}. Zet daar eerst een andere registry of de publieke registry, "
        f"en haal deze registry daarna pas weg."
        for naam, plekken in sorted(gebruikt.items())
    ]
