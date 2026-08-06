"""Typed config model for the ``aliases`` system service (RC-25).

An alias maps a name the application expects onto a variable the platform exposes:
``POSTGRES_HOST: $DATABASE_SERVER_HOST``. It is stored as a mapping on the component,
with each value AGE-encrypted independently (a value may hold a secret), so the model
accepts both the encrypted and the plain form of a value.

What it does check is the key: an alias becomes an environment variable, so its name
must be a valid one. The "an alias value should reference something" rule deliberately
lives in the form validator (``AliasReferenceValidator``) and not here -- a stored alias
without a reference is harmless at deploy time (``substitute_variables`` passes it
through untouched), so turning it into a hard file-level error would reject existing,
working projects. The form is where the author still has the value in front of them.
"""

from __future__ import annotations

import re

from pydantic import Field, RootModel, field_validator

#: An environment-variable name: the shape an alias key must have to become one.
ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AliasesConfig(RootModel[dict[str, str]]):
    """A component's alias map: env-var name -> template referencing platform variables."""

    root: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Aliases for this component: the environment variable name the application expects, "
            "mapped to a template referencing a platform variable, e.g. "
            "POSTGRES_HOST: $DATABASE_SERVER_HOST."
        ),
    )

    @field_validator("root")
    @classmethod
    def _valid_keys(cls, value: dict[str, str]) -> dict[str, str]:
        invalid = [key for key in value if not ENV_VAR_NAME.match(key)]
        if invalid:
            raise ValueError(
                f"Ongeldige aliasnaam/-namen: {', '.join(sorted(invalid))}. "
                "Een alias wordt een omgevingsvariabele en moet beginnen met A-Z, a-z of _ "
                "en verder alleen A-Z, a-z, 0-9 of _ bevatten."
            )
        return value
