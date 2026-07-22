"""Single validation chokepoint for ZAD project files.

Every project definition that enters the Operations Manager - whether through
the API/wizard or through a direct git commit picked up by the git monitor -
must pass JSON Schema validation before any processing happens. This is the
security gate that stops hostile namespaces, ownership injection and field
injection from reaching the connectors.

Fails closed: any schema violation raises ProjectSchemaError and the caller
must reject the project.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "project_v2.json"


class ProjectSchemaError(Exception):
    """Raised when a project file does not conform to the project schema.

    The message is user-facing and in Dutch (government project convention).
    """


class ProjectIntegrityError(Exception):
    """Raised when a project file is schema-valid but structurally inconsistent.

    Covers the cross-field checks the JSON schema cannot express: duplicate
    component/deployment names, dangling component references, colliding ingress
    paths, invalid root components and hard domain-config violations. Like
    ProjectSchemaError the message is user-facing and in Dutch, and the caller
    must reject the project (fails closed).
    """


@lru_cache(maxsize=1)
def _get_validator() -> Draft202012Validator:
    with SCHEMA_PATH.open(encoding="utf-8") as schema_file:
        schema = json.load(schema_file)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_project_schema(project_data: dict[str, Any]) -> None:
    """Validate a parsed project dict against the project schema.

    Args:
        project_data: The parsed project file content.

    Raises:
        ProjectSchemaError: If the project does not conform to the schema.
            The exception message is a Dutch user-facing error that names the
            first offending field path and the validation message.
    """
    validator = _get_validator()
    errors = sorted(validator.iter_errors(project_data), key=lambda e: list(e.absolute_path))
    if not errors:
        return

    first = errors[0]
    location = "/".join(str(part) for part in first.absolute_path) or "(root)"
    project_name = project_data.get("name", "(onbekend)") if isinstance(project_data, dict) else "(onbekend)"
    message = (
        f"Projectbestand '{project_name}' is afgekeurd: het voldoet niet aan het projectschema. "
        f"Veld '{location}': {first.message}"
    )
    # Do not log at ERROR here: the message is carried on the exception and the
    # caller logs it once with context. Self-logging made one rejection surface as
    # several ERR alerts (validator + orchestrator + task-progress). debug keeps a
    # breadcrumb without feeding the log-watch.
    logger.debug(message)
    raise ProjectSchemaError(message)


# The marker of the schema's age-encrypted $defs pattern. Detection is derived from
# the schema rather than from a hand-written field list, so it cannot drift when a
# new secret field is added.
_AGE_PATTERN_MARKER = "BEGIN AGE ENCRYPTED FILE"


def _walk_errors(errors: Any) -> Any:
    """Yield validation errors depth-first, including anyOf/oneOf sub-errors."""
    for error in errors:
        yield error
        yield from _walk_errors(error.context or [])


def find_plaintext_secret_violations(project_data: dict[str, Any]) -> list[str]:
    """Field paths that must hold an AGE-encrypted value but do not.

    Separate from ``validate_project_schema`` because this specific class of
    violation must fail closed on EVERY write path. ``enforce_validation=False``
    exists so a recovery write is not blocked by pre-existing structural drift --
    it is not a licence to commit a decrypted secret, which is what writing back a
    ``get_decrypted()`` view would do.

    Returns the offending field paths, empty when there are none.
    """
    validator = _get_validator()
    violations: list[str] = []
    for error in _walk_errors(validator.iter_errors(project_data)):
        schema = error.schema if isinstance(error.schema, dict) else {}
        if _AGE_PATTERN_MARKER in str(schema.get("pattern", "")):
            violations.append("/".join(str(part) for part in error.absolute_path) or "(root)")
    return sorted(set(violations))
