"""What an alias value points at, and whether it points at anything (RC-66).

An alias is a reference: ``POSTGRES_HOST: $DATABASE_SERVER_HOST`` says "give my
component the platform's database host under the name my application expects". Two
things follow from that, and both were missing.

**A reference the platform does not provide is a typo.** The service says so itself
("Een onbekende verwijzing is hier een harde fout, anders dan bij een eigen
omgevingsvariabele") and ``project_manager._categorize_alias`` enforces it -- but only
at deploy time, so ``{"KAPOT": "$BESTAAT_ECHT_NIET"}`` was accepted by the API and only
surfaced much later, when the container came up. :func:`validate_alias_value` is that
same rule at the moment of writing.

**A reference is not a secret.** The value IS the coupling, so masking it hides exactly
what a reader asks about. :func:`is_reference` is the distinction that lets the read
side show a pointer while it keeps masking a literal -- see
``AliasesService.owned_value_is_secret``. It is deliberately conservative: a value that
is not (only) references may carry a literal secret and stays masked.

The known names come from the service definitions, the same source
``_categorize_alias`` uses, so "known here" and "resolvable at deploy time" cannot
drift apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opi.services.component_values import ComponentValuesError
from opi.services.services import ServiceAdapter
from opi.utils.env_vars import extract_variable_references

if TYPE_CHECKING:
    from opi.services.services import VariableDefinition
    from opi.services.services_enums import ServiceType


def variable_to_service() -> dict[str, tuple[ServiceType, VariableDefinition]]:
    """Every platform variable name an alias may reference, and where it comes from.

    Includes each variable's alternative names: they resolve at deploy time, so they
    are as valid in an alias as the primary name.
    """
    mapping: dict[str, tuple[ServiceType, VariableDefinition]] = {}
    for service_type in ServiceAdapter.SERVICE_DEFINITIONS:
        for var_def in ServiceAdapter.get_service_definition(service_type).variables:
            for name in var_def.get_all_names():
                mapping[name] = (service_type, var_def)
    return mapping


def known_variable_names() -> set[str]:
    """The names an alias may reference."""
    return set(variable_to_service())


def is_reference(value: str) -> bool:
    """Whether *value* is a pointer at platform variables rather than a literal.

    True when it references at least one platform variable and every reference it makes
    is one the platform provides. Such a value carries no secret of its own: it names
    where the value comes from, which is the whole point of asking for it.
    """
    references = extract_variable_references(value)
    if not references:
        return False
    known = known_variable_names()
    return all(reference in known for reference in references)


def validate_alias_value(key: str, value: str) -> None:
    """Raise unless *value* references platform variables that exist.

    The strict counterpart of an own environment variable, where a dollar sign in a
    password is not a typo and is passed through untouched. Here it is: an alias exists
    to point somewhere, and a reference that resolves to nothing fails the deploy.
    """
    references = extract_variable_references(value)
    if not references:
        raise ComponentValuesError(
            f"De alias '{key}' verwijst niet naar een platformvariabele. Een alias koppelt een "
            "naam aan een variabele van het platform, bijvoorbeeld $DATABASE_SERVER_HOST. "
            "Gebruik een eigen omgevingsvariabele (user-env-vars) voor een vaste waarde."
        )
    known = known_variable_names()
    unknown = sorted({reference for reference in references if reference not in known})
    if unknown:
        raise ComponentValuesError(
            f"De alias '{key}' verwijst naar onbekende variabele(n): {', '.join(unknown)}. "
            f"Beschikbare variabelen: {', '.join(sorted(known))}."
        )
