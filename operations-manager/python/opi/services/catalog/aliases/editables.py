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
from opi.utils.age import is_age_encrypted
from opi.utils.env_vars import extract_variable_references, validate_and_parse_env_vars


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
    """

    def validate(self, value: Any) -> list[str]:
        if not isinstance(value, str) or not value.strip() or is_age_encrypted(value):
            return []
        try:
            aliases = validate_and_parse_env_vars(value)
        except (ValueError, TypeError) as e:
            return [str(e)]
        without = [name for name, template in aliases.items() if not extract_variable_references(str(template))]
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
    converter=KeyValueConverter(fmt="env"),
    validator=AliasMapValidator(),
    remove_when_none=True,
)
