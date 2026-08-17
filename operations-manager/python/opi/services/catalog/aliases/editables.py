"""Editables owned by the ``aliases`` system service (RC-25).

The yaml_path is NOT built with ``config_path``: this service owns a plain property of
the component (``components[*]/aliases``), not a block inside the ``services`` list. The
service model is what gives it a config model, a schema fragment, a validator and a
declared form section; the storage location stays exactly where it has always been, so
no project file changes.
"""

from __future__ import annotations

from typing import Any

from opi.forms.editables.converters import KeyValueConverter
from opi.forms.editables.editable import Editable
from opi.forms.wizard.secrets import REDACTED
from opi.utils.age import is_age_encrypted
from opi.utils.env_vars import extract_variable_references, validate_and_parse_env_vars


def _is_untouched(template: str) -> bool:
    """Whether this alias value is one the author never saw: stored, not re-entered."""
    return template.strip() == REDACTED or is_age_encrypted(template)


class AliasMapValidator:
    """The alias map parses, and every alias references at least one variable.

    Two rules in one validator because they share the parse: the text must be readable
    as ``KEY=value`` or YAML (the same parser the deploy path uses, so what validates
    here also deploys), and each value must contain a ``$VAR`` / ``${VAR}`` reference.

    That second rule is what an alias IS: a second name for a variable the platform
    exposes. A constant belongs in the environment variables next to it, which are
    encrypted as a whole and carry no reference requirement. The rule lives here, on the
    form, and not in the config model: an already-stored constant deploys fine
    (``substitute_variables`` passes a reference-free template through untouched), so
    rejecting it at file level would break working projects. Here the author still has
    the value on screen.

    A value the author does NOT have on screen cannot be judged, and there are two of
    those. An AGE block is one -- and since RC-106 that is the whole set at once, so the
    guard on the text as a whole below is the one that fires. The other is the
    ``REDACTED`` placeholder, kept because the wizard session redaction may still hand it
    back: judging it as a reference-free constant made every following save of the
    components modal fail with "Alias(sen) zonder verwijzing" -- for any component that
    had ever stored an alias, and for a plain user editing something else entirely. The
    placeholder is put back from the stored project at save
    (``restore_redacted_secrets``), so what is written is the original value, which was
    validated when it was entered.
    """

    def validate(self, value: Any) -> list[str]:
        if not isinstance(value, str) or not value.strip() or is_age_encrypted(value):
            return []
        if value.strip() == REDACTED:
            # The whole set, redacted as one block: nothing on screen to judge.
            return []
        try:
            aliases = validate_and_parse_env_vars(value)
        except (ValueError, TypeError) as e:
            return [str(e)]
        without = [
            name
            for name, template in aliases.items()
            if not _is_untouched(str(template)) and not extract_variable_references(str(template))
        ]
        if not without:
            return []
        return [
            f"Alias(sen) zonder verwijzing: {', '.join(sorted(without))}. "
            "Een alias verwijst naar een platformvariabele, bijvoorbeeld "
            "POSTGRES_HOST=$DATABASE_SERVER_HOST. Zet een vaste waarde bij de "
            "omgevingsvariabelen."
        ]


COMPONENT_ALIASES_EDITABLE = Editable(
    yaml_path="components[*]/aliases",
    converter=KeyValueConverter(fmt="env", write_as="string"),
    validator=AliasMapValidator(),
    remove_when_none=True,
)
