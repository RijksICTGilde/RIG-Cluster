"""Typed config model for the ``user-env-vars`` system service (RC-25).

The value is what a component (or a deployment's component) carries under the
``user-env-vars`` key. Three shapes are legal:

* an AGE-encrypted **block** (``-----BEGIN AGE ENCRYPTED FILE-----``) -- the normal
  stored shape, written by the encrypt generator. Note this is a block, not the
  single-line ``base64+age:`` prefix form used for passwords elsewhere in a project file;
* a plain ``KEY=value`` / YAML text block -- what the form posts before encryption and
  what a hand-written project file may contain;
* a mapping -- the legacy shape from before the value became a single string.

Validation therefore checks what it can: an encrypted block is opaque and accepted as
is, a plaintext block must parse as one of the two supported formats with keys that are
valid environment-variable names. That is the same parser the deploy path uses
(``validate_and_parse_env_vars``), so a file that validates here also deploys.
"""

from __future__ import annotations

from pydantic import RootModel, field_validator

from opi.utils.age import is_age_encrypted
from opi.utils.env_vars import validate_and_parse_env_vars


class UserEnvVarsConfig(RootModel[str | dict[str, str]]):
    """A component's own environment variables, encrypted or plain."""

    @field_validator("root")
    @classmethod
    def _parseable(cls, value: str | dict[str, str]) -> str | dict[str, str]:
        if isinstance(value, str):
            if is_age_encrypted(value):
                return value
            validate_and_parse_env_vars(value)
            return value
        # A mapping is already parsed; only the key shape is still checkable, and the
        # parser owns that rule, so run the same text through it.
        validate_and_parse_env_vars("\n".join(f"{key}={val}" for key, val in value.items()))
        return value
