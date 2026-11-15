"""Editables owned by the ``user-env-vars`` system service.

Two layers, one field each, and both yaml_paths are plain component properties rather
than ``config_path`` blocks inside the ``services`` list: this service owns
``components[*]/user-env-vars`` and ``deployments[*]/components[*]/user-env-vars``. Being a
service gives them the config model, the schema fragment, the validator and the declared
form sections; the data stays where it is.

The deployment-component value wins over the component value on a collision (merged in
``ProjectManager``), which is why the same service carries both.

The validator and the converter below are the field's own, not the generic key/value pair:
the form is one of the writers of this block, and since RC-481 every writer of it judges
and stores a value the same way (``opi.services.component_values``).
"""

from __future__ import annotations

from typing import Any

from opi.forms.editables.converters import KeyValueConverter
from opi.forms.editables.editable import Editable
from opi.forms.wizard.secrets import REDACTED
from opi.services.component_values import (
    ComponentValuesError,
    encode_with_key,
    validate_value_for_storage,
)
from opi.utils.age import carries_encrypted_value, get_project_public_key
from opi.utils.env_vars import validate_and_parse_env_vars


def _is_stored_shape(value: str) -> bool:
    """Whether this text is what storage holds rather than what an author typed.

    Neither form is judged or re-encoded: the wizard validates its STORED state at submit,
    and that state carries an AGE block or the redaction placeholder the session hands back.
    """
    return carries_encrypted_value(value) or value.strip() == REDACTED


class UserEnvVarsValidator:
    """The text parses, and every value survives the storage shape.

    Parsing alone is not enough. A value with edge whitespace or surrounding quotes does
    not come back out of the encrypted ``KEY=value`` block as it went in, which is why the
    values API refuses it with a 422 (``validate_value_for_storage``). Judging it here as
    well means the create wizard and the add-component route cannot store what every other
    writer of this field rejects.
    """

    def validate(self, value: object) -> list[str]:
        if not isinstance(value, str) or not value.strip() or _is_stored_shape(value):
            return []
        try:
            values = validate_and_parse_env_vars(value)
        except (ValueError, TypeError) as error:
            return [str(error)]
        problems: list[str] = []
        for key, stored in values.items():
            try:
                validate_value_for_storage(key, str(stored))
            except ComponentValuesError as refusal:
                problems.append(str(refusal))
        return problems


class UserEnvVarsConverter(KeyValueConverter):
    """Stores the typed text as the canonical block, the shape every reader expects.

    Reading stays the generic key/value behaviour (decrypt, show ``KEY=value`` lines); only
    the write goes through ``component_values``, so what the create wizard stores is what
    the values API reads back. Without a public key it raises, like every other writer.

    A converter must not raise over the value itself: the processor writes a field whose
    validation already failed, and the field error is what stops the save.
    """

    def __init__(self) -> None:
        super().__init__(fmt="env", write_as="string")

    def write(self, value: object, context_data: dict[str, Any] | None = None) -> str | None:
        if isinstance(value, dict):
            values = {str(key): str(stored) for key, stored in value.items()}
        else:
            text = str(value or "").strip()
            if not text:
                return None
            if _is_stored_shape(text):
                return text
            values = self._parse_env_text(text)
        return encode_with_key(values, get_project_public_key(context_data or {}))


COMPONENT_USER_ENV_VARS_EDITABLE = Editable(
    yaml_path="components[*]/user-env-vars",
    converter=UserEnvVarsConverter(),
    validator=UserEnvVarsValidator(),
    remove_when_none=True,
)

DEPLOYMENT_COMP_USER_ENV_VARS_EDITABLE = Editable(
    yaml_path="deployments[*]/components[*]/user-env-vars",
    converter=UserEnvVarsConverter(),
    validator=UserEnvVarsValidator(),
    remove_when_none=True,
)
