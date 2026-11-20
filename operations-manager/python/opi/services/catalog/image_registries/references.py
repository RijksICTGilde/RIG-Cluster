"""The way back: a registry does not vanish from under a component.

See features/image-registries.md, "De weg terug is geen stille weg". Next to
``values_must_exist``, because that one deliberately skips an EMPTY option list, and empty is
exactly the state that arises here.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.image_registries.resolution import component_registry_name, project_registries


def validate_registry_references(project_data: dict[str, Any]) -> list[str]:
    """Refuse a project file in which a component, a deployment or the dedicated database
    points at a registry that is no longer there, naming the places that mention it."""
    # Lazy: ``postgres_scope`` reads the service registry, which imports this module.
    from opi.services.postgres_scope import dedicated_postgres_registry_reference

    available = {registry["name"] for registry in project_registries(project_data)}
    used: dict[str, list[str]] = {}

    for component in project_data.get("components", []) or []:
        name = component_registry_name(component) if isinstance(component, dict) else None
        if name and name not in available:
            used.setdefault(name, []).append(f"component '{component.get('name', 'onbekend')}'")

    for deployment in project_data.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        for component in deployment.get("components", []) or []:
            name = component_registry_name(component) if isinstance(component, dict) else None
            if name and name not in available:
                place = f"deployment '{deployment.get('name', 'onbekend')}', component '{component.get('reference', 'onbekend')}'"
                used.setdefault(name, []).append(place)

    reference = dedicated_postgres_registry_reference(project_data)
    if reference is not None and reference[1] not in available:
        used.setdefault(reference[1], []).append(f"database ({reference[0]})")

    return [
        f"Registry '{name}' bestaat niet (meer) in dit project, maar wordt nog gebruikt door: "
        f"{', '.join(places)}. Zet daar eerst een andere registry of Automatisch, "
        f"en haal deze registry daarna pas weg."
        for name, places in sorted(used.items())
    ]
