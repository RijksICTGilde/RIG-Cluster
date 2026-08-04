"""Typed config model for the ``user-env-vars`` system service (RC-25).

The value is what a component (or a deployment's component) carries under the
``user-env-vars`` key. It has three shapes in the wild, and all three are legal:

* an AGE-encrypted block -- the normal stored shape, written by the encrypt generator;
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

from opi.utils.age import is_age_encrypted, parse_password_with_prefix
from opi.utils.env_vars import validate_and_parse_env_vars


def _is_opaque(value: str) -> bool:
    """Whether the value is encrypted, and therefore not ours to parse.

    Two forms are in the wild and both must be accepted: the multiline AGE block, and
    the single-line prefixed form (``base64+age:``, ``age:``) that fits in a .env-style
    value. Reading only the block form is how a real production file
    (``base64+age:...`` on ``algor-odc``) would have been rejected.
    """
    return is_age_encrypted(value) or parse_password_with_prefix(value)[0] != "plain"


class UserEnvVarsConfig(RootModel[str | dict[str, str]]):
    """A component's own environment variables, encrypted or plain."""

    @field_validator("root")
    @classmethod
    def _parseable(cls, value: str | dict[str, str]) -> str | dict[str, str]:
        if isinstance(value, str):
            if _is_opaque(value):
                return value
            validate_and_parse_env_vars(value)
            return value
        # A mapping is already parsed; only the key shape is still checkable, and the
        # parser owns that rule, so run the same text through it.
        validate_and_parse_env_vars("\n".join(f"{key}={val}" for key, val in value.items()))
        return value
