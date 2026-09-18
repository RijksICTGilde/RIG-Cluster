"""Of de samengestelde schemanamen van dit project passen (RC-59).

Een extra schema heet ``{project}_{deployment}_{postfix}`` en dat moet binnen de 63
tekens van PostgreSQL blijven. Hoeveel ruimte de postfix heeft hangt dus af van de
project- en deploymentnaam, en die staan niet in het configblok van de dienst: daarom
een regel over het hele project en geen veldregel.
"""

from __future__ import annotations

from typing import Any

from opi.services.postgres_scope import get_postgres_schemas
from opi.utils.naming import generate_extra_database_schema


def validate_database_schema_names(project_data: dict[str, Any]) -> list[str]:
    """Elk extra schema tegen ELKE deployment, ook een deployment die er nog niet was toen
    de schemalijst werd opgeslagen.

    Dat is het gat dat ``UniqueSchemaEnforcer`` openliet: een postfix die vandaag past,
    past niet meer zodra er een deployment met een langere naam bij komt, en op die weg
    keek niets naar schema's. Een gemarkeerd schema telt niet mee, want dat mag een
    opslag niet blokkeren (``get_postgres_schemas``).
    """
    errors: list[str] = []
    project_name = project_data.get("name") or ""
    deployment_names = [
        name for d in (project_data.get("deployments") or []) if isinstance(d, dict) and (name := d.get("name"))
    ]
    if not deployment_names:
        return errors

    for entry in get_postgres_schemas(project_data):
        postfix = entry.get("postfix")
        if not postfix:
            continue
        for deployment_name in deployment_names:
            try:
                generate_extra_database_schema(project_name, deployment_name, postfix)
            except ValueError:
                errors.append(
                    f"schema '{postfix}' levert voor deployment '{deployment_name}' een naam op die langer is "
                    f"dan de 63 tekens die PostgreSQL toestaat. Kies een kortere postfix of een kortere "
                    f"deploymentnaam."
                )
    return errors
