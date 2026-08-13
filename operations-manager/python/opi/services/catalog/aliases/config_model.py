"""Typed config model for the ``aliases`` system service (RC-25).

An alias maps a name the application expects onto a variable the platform exposes:
``POSTGRES_HOST: $DATABASE_SERVER_HOST``. Since RC-106 it is stored exactly like
``user-env-vars``: ONE AGE block whose plaintext is ``KEY=value`` lines. Three shapes are
therefore legal, and the model accepts all three:

* an AGE-encrypted **block** -- the normal stored shape, opaque and accepted as is;
* a plain ``KEY=value`` / YAML text block -- what the form posts before encryption;
* a mapping -- the unencrypted shape, which stays valid in the project schema.

What it checks is the key: an alias becomes an environment variable, so its name
must be a valid one. The "an alias value should reference something" rule deliberately
lives in the form validator (``AliasReferenceValidator``) and not here -- a stored alias
without a reference is harmless at deploy time (``substitute_variables`` passes it
through untouched), so turning it into a hard file-level error would reject existing,
working projects. The form is where the author still has the value in front of them.
"""

from __future__ import annotations

import re

from pydantic import Field, RootModel, field_validator

from opi.utils.age import is_age_encrypted
from opi.utils.env_vars import validate_and_parse_env_vars

#: An environment-variable name: the shape an alias key must have to become one.
ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AliasesConfig(RootModel[str | dict[str, str]]):
    """A component's aliases: env-var name -> template referencing platform variables."""

    root: str | dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Aliases for this component: the environment variable name the application expects, "
            "mapped to a template referencing a platform variable, e.g. "
            "POSTGRES_HOST: $DATABASE_SERVER_HOST. Stored as one AGE-encrypted block of "
            "KEY=value lines; a plain block or an unencrypted mapping is also read."
        ),
    )

    @field_validator("root")
    @classmethod
    def _valid_keys(cls, value: str | dict[str, str]) -> str | dict[str, str]:
        if isinstance(value, str):
            if is_age_encrypted(value):
                return value
            # The same parser the deploy path uses, so what validates here also deploys.
            names = list(validate_and_parse_env_vars(value))
        else:
            names = list(value)
        invalid = [key for key in names if not ENV_VAR_NAME.match(key)]
        if invalid:
            raise ValueError(
                f"Ongeldige aliasnaam/-namen: {', '.join(sorted(invalid))}. "
                "Een alias wordt een omgevingsvariabele en moet beginnen met A-Z, a-z of _ "
                "en verder alleen A-Z, a-z, 0-9 of _ bevatten."
            )
        return value
