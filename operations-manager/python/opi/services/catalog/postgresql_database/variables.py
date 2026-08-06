"""Environment variables the postgresql-database service provides -- single source of truth.

Lives in the service's own package (RC-36): what a service hands to a deployment
is part of that service, not of a shared module.
"""

from enum import Enum

from opi.services.services import VariableDefinition


class DatabaseVariables(Enum):
    """Database service variable definitions - single source of truth."""

    HOST = VariableDefinition(
        name="DATABASE_SERVER_HOST",
        description="PostgreSQL server hostnaam",
        source="secret",
        secret_key="host",
        aliases=["APP_DATABASE_SERVER_HOST", "APP_DATABASE_SERVER"],
    )
    PORT = VariableDefinition(
        name="DATABASE_SERVER_PORT",
        description="PostgreSQL server poort",
        source="secret",
        secret_key="port",
        aliases=["APP_DATABASE_PORT", "APP_DATABASE_SERVER_PORT"],
    )
    USER = VariableDefinition(
        name="DATABASE_SERVER_USER",
        description="Database gebruikersnaam",
        source="secret",
        secret_key="username",
        aliases=["APP_DATABASE_USER"],
    )
    PASSWORD = VariableDefinition(
        name="DATABASE_PASSWORD",
        description="Database gebruiker wachtwoord",
        source="secret",
        secret_key="password",
        aliases=["APP_DATABASE_PASSWORD"],
    )
    USER_RO = VariableDefinition(
        name="DATABASE_SERVER_USER_RO",
        description="Read-only database gebruikersnaam",
        source="secret",
        secret_key="ro_username",
        aliases=["APP_DATABASE_USER_RO"],
    )
    PASSWORD_RO = VariableDefinition(
        name="DATABASE_PASSWORD_RO",
        description="Read-only database gebruiker wachtwoord",
        source="secret",
        secret_key="ro_password",
        aliases=["APP_DATABASE_PASSWORD_RO"],
    )
    DATABASE = VariableDefinition(
        name="DATABASE_DB",
        description="Database naam",
        source="secret",
        secret_key="database",
        aliases=["APP_DATABASE_DB"],
    )
    SCHEMA = VariableDefinition(
        name="DATABASE_SCHEMA",
        description="Database schema naam",
        source="secret",
        secret_key="schema",
        aliases=["APP_DATABASE_SCHEMA"],
    )
    CONNECTION_STRING = VariableDefinition(
        name="DATABASE_SERVER_FULL",
        description="Volledige PostgreSQL connectiestring",
        source="secret",
        secret_key="connection_string",
        aliases=["APP_DATABASE_SERVER_FULL"],
    )


def reserved_database_variable_names() -> set[str]:
    """Every env-variable name the database service already exposes (names + aliases).

    An extra schema's ``DATABASE_SCHEMA_{POSTFIX}`` variable must not collide with one
    of these (a postfix ``db`` would produce ``DATABASE_SCHEMA_DB``, uncomfortably close
    to ``DATABASE_DB``). The save-time enforcer checks a candidate against this set.
    """
    names: set[str] = set()
    for var in DatabaseVariables:
        names.add(var.value.name)
        names.update(var.value.aliases)
    return names
