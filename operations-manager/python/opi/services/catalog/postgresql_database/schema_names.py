"""Whether this project's composed schema names fit.

An extra schema is called ``{project}_{deployment}_{postfix}`` and has to stay within
PostgreSQL's 63 characters. How much room the postfix has therefore depends on the project and
deployment name, and those are not in the service's config block: hence a rule about the whole
project and not a field rule.
"""

from __future__ import annotations

from typing import Any

from opi.services.postgres_scope import get_postgres_schemas
from opi.utils.naming import generate_extra_database_schema


def validate_database_schema_names(project_data: dict[str, Any]) -> list[str]:
    """Every extra schema against EVERY deployment, including a deployment that did not exist
    when the schema list was stored.

    ``UniqueSchemaEnforcer`` does not cover that: a postfix that fits today no longer fits
    once a deployment with a longer name is added. A marked schema does not count, because it
    must not block a save (``get_postgres_schemas``).
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
