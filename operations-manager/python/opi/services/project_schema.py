"""Single validation chokepoint for ZAD project files.

Every project definition that enters the Operations Manager - whether through
the API/wizard or through a direct git commit picked up by the git monitor -
must pass JSON Schema validation before any processing happens. This is the
security gate that stops hostile namespaces, ownership injection and field
injection from reaching the connectors.

Fails closed: any schema violation raises ProjectSchemaError and the caller
must reject the project.

One schema per schema-version (RC-32)
-------------------------------------
``project_v2.json`` is the schema of the LATEST version and nothing else. A file
that declares an older ``schema-version`` is validated against the schema of
*that* version, composed here from the latest schema plus a chain of legacy
patches in ``schemas/project_legacy/``. ``v<version>.json`` there is an RFC 7386
JSON Merge Patch that turns the schema of the next-newer version back into the
schema of that version - so a patch is the schema-level record of what one
migration changed, and ``null`` removes what that migration introduced.

That is what lets a migration be finished: once the old form lives in the patch
of the version that still carried it, it can leave the latest schema, instead of
having to stay forever because some file on disk has not been reprocessed yet.

Every version except the latest MUST have a patch file (an empty ``{}`` is a
valid patch, for a migration that did not change the accepted shape). A missing
one is a hard error, not a silent fallback - see ``check_schema_versions``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

if TYPE_CHECKING:
    from jsonschema.exceptions import ValidationError

logger = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
SCHEMA_PATH = SCHEMA_DIR / "project_v2.json"
LEGACY_PATCH_DIR = SCHEMA_DIR / "project_legacy"

#: Annotation in the latest schema naming the version it describes. Kept in the
#: schema itself so the schema file, not a Python constant, says what it is.
SCHEMA_VERSION_KEY = "x-zad-schema-version"


class ProjectSchemaError(Exception):
    """Raised when a project file does not conform to the project schema.

    The message is user-facing and in Dutch (government project convention).

    ``field_path`` names the offending field in schema notation
    (``components/0/command``) when the violation could be located, so a caller
    that has a form in front of it can point at the field the user filled in
    instead of only echoing the message. It is None for the rejections that are
    about the file as a whole (an unknown or missing schema version).

    ``reason`` says WHY the field was rejected: the schema keyword that refused it,
    in words. The message is built from the path and that reason, so neither carries
    the rejected value -- a caller that logs this or shows it in a browser would
    otherwise echo what may be a stored secret.
    """

    def __init__(self, message: str, *, field_path: str | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.field_path = field_path
        self.reason = reason


class ProjectIntegrityError(Exception):
    """Raised when a project file is schema-valid but structurally inconsistent.

    Covers the cross-field checks the JSON schema cannot express: duplicate
    component/deployment names, dangling component references, colliding ingress
    paths, invalid root components and hard domain-config violations. Like
    ProjectSchemaError the message is user-facing and in Dutch, and the caller
    must reject the project (fails closed).
    """


#: A schema version as text: MAJOR, or MAJOR.MINOR. The number of digits is bounded because
#: int() raises ValueError above 4300 digits, and this path has to raise ProjectSchemaError
#: always: the declared-version gate catches only that one, and the value comes from a project
#: file.
_VERSION_TEXT = re.compile(r"^(\d{1,9})(?:\.(\d{1,9}))?$")


@dataclass(frozen=True, order=True)
class SchemaVersion:
    """A schema version as MAJOR.MINOR, compared part by part: 2.10 is newer than 2.9.

    A float is read through its text form, so the float ``2.1`` is always (2, 1) and only the
    string ``"2.10"`` is (2, 10). That is why OPI writes a new version out as a string: as a
    number 2.10 would equal 2.1.
    """

    major: int
    minor: int = 0

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}" if self.minor else str(self.major)


def _float_text(value: float) -> str:
    """The text of a float, with the trailing zeroes the YAML source still had.

    An unquoted ``schema-version: 2.10`` is the number 2.1 to YAML, and that is a different,
    older version. ruamel remembers the width of the number as it was written (``_width``,
    with ``_prec`` as the position of the dot), so that turns it back into 2.10 and not 2.1.
    """
    width, prec = getattr(value, "_width", None), getattr(value, "_prec", None)
    if isinstance(width, int) and isinstance(prec, int) and width > prec + 1:
        return f"{value:.{width - prec - 1}f}"
    return f"{value:g}"


def parse_schema_version(version: SchemaVersion | int | float | str) -> SchemaVersion:
    """Read a schema version out of an int, a float or text.

    Raises ``ProjectSchemaError`` for a value that is neither MAJOR nor MAJOR.MINOR. The
    value itself does not end up in the message: it comes from a project file.
    """
    if isinstance(version, SchemaVersion):
        return version
    text = _float_text(version) if isinstance(version, float) else str(version)
    match = None if isinstance(version, bool) else _VERSION_TEXT.match(text)
    if match is None:
        raise ProjectSchemaError(
            "Een schemaversie moet de vorm MAJOR of MAJOR.MINOR hebben, met hoogstens negen cijfers per deel."
        )
    return SchemaVersion(int(match.group(1)), int(match.group(2) or 0))


def version_key(version: SchemaVersion | int | float | str) -> str:
    """Canonical file-name key for a schema version: 2.0 -> "2", 2.6 -> "2.6"."""
    return str(parse_schema_version(version))


@lru_cache(maxsize=1)
def _load_latest_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as schema_file:
        schema: dict[str, Any] = json.load(schema_file)
    return schema


def latest_schema_version() -> SchemaVersion:
    """The version the committed latest schema describes."""
    version = _load_latest_schema().get(SCHEMA_VERSION_KEY)
    if not isinstance(version, int | float | str) or isinstance(version, bool):
        raise ProjectSchemaError(
            f"Het projectschema mist de annotatie '{SCHEMA_VERSION_KEY}'; de schemaversie is niet vast te stellen."
        )
    return parse_schema_version(version)


@lru_cache(maxsize=1)
def _legacy_patch_versions() -> tuple[SchemaVersion, ...]:
    """Versions that have a legacy patch on disk, oldest first."""
    return tuple(sorted(parse_schema_version(path.stem[1:]) for path in LEGACY_PATCH_DIR.glob("v*.json")))


def known_schema_versions() -> tuple[SchemaVersion, ...]:
    """Every schema version this build can validate against, oldest first."""
    return (*_legacy_patch_versions(), latest_schema_version())


def _merge_patch(target: Any, patch: Any) -> Any:
    """Apply an RFC 7386 JSON Merge Patch. A ``null`` value removes the key."""
    if not isinstance(patch, dict):
        return patch
    if not isinstance(target, dict):
        target = {}
    result = dict(target)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


@cache
def _get_validator(version: SchemaVersion | None = None) -> Draft202012Validator:
    """Validator for one schema version. ``None`` means the latest.

    An older version is composed by applying every legacy patch from the latest
    down to the requested version, in order.
    """
    schema = _load_latest_schema()
    if version is not None and version != latest_schema_version():
        if version not in _legacy_patch_versions():
            raise ProjectSchemaError(
                f"Onbekende schemaversie '{version_key(version)}': er is geen schema voor deze versie. "
                f"Bekende versies: {', '.join(version_key(v) for v in known_schema_versions())}."
            )
        for patch_version in sorted(_legacy_patch_versions(), reverse=True):
            if patch_version < version:
                break
            with (LEGACY_PATCH_DIR / f"v{version_key(patch_version)}.json").open(encoding="utf-8") as patch_file:
                schema = _merge_patch(schema, json.load(patch_file))
    Draft202012Validator.check_schema(schema)
    # With a format_checker "format" does something; without one it is decoration. The real
    # work is done by the "pattern" next to it: jsonschema checks "email" for an at sign only,
    # and skips "date-time" as long as rfc3339-validator is not installed.
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def check_schema_versions(migration_versions: tuple[SchemaVersion, ...]) -> None:
    """Fail loudly when a migration exists without a schema for its version.

    ``migration_versions`` is the full chain a project file can be stamped with
    (``schema_migration.SCHEMA_VERSIONS``). Every one of them must be validatable:
    the newest by the latest schema, each older one by a legacy patch. Adding a
    migration without adding its patch is a programming error and stops startup
    here rather than surfacing as a rejected project file months later.

    Raises ``ProjectSchemaError`` when the chain and the schemas on disk disagree.
    """
    latest = latest_schema_version()
    if migration_versions[-1] != latest:
        raise ProjectSchemaError(
            f"Het projectschema beschrijft versie {version_key(latest)} maar de migratieketen eindigt op "
            f"{version_key(migration_versions[-1])}; werk project_v2.json bij of voeg de ontbrekende migratie toe."
        )
    known = set(known_schema_versions())
    missing = [version_key(v) for v in migration_versions if v not in known]
    if missing:
        raise ProjectSchemaError(
            f"Geen schema voor schemaversie(s) {', '.join(missing)}: voeg "
            f"opi/schemas/project_legacy/v<versie>.json toe voor elke migratie."
        )
    extra = [version_key(v) for v in sorted(known) if v not in set(migration_versions)]
    if extra:
        raise ProjectSchemaError(
            f"Schema aanwezig voor onbekende versie(s) {', '.join(extra)}: die staan niet in de migratieketen."
        )


def _describe_violation(error: ValidationError) -> str:
    """Say why a value was rejected WITHOUT repeating the value.

    jsonschema's own message quotes the instance ("'geheim' does not match ..."),
    so it can carry a stored secret (``config/api-key``, ``user-env-vars``) or
    attacker-chosen text into a browser or a log line. The keyword and the limit it was
    given come from the schema, so a description built from those two is safe to show.

    A field NAME may be named: the three keywords that are about which fields are there
    (``additionalProperties``, ``required``, ``not``) say which ones, because without that
    the rejection cannot be acted on. A name is not a value.
    """
    keyword = str(error.validator)
    if keyword == "additionalProperties":
        return _unknown_fields_reason(error)
    limit = error.validator_value
    if keyword == "required" and isinstance(limit, list):
        missing = [str(field) for field in limit if not isinstance(error.instance, dict) or field not in error.instance]
        if missing:
            return f"een verplicht veld ontbreekt: {', '.join(missing)}"
    if keyword == "not" and isinstance(limit, dict) and isinstance(limit.get("required"), list):
        # ``not: {required: [...]}`` is how the schema forbids a field in this shape.
        return f"dit veld hoort hier niet te staan: {', '.join(str(field) for field in limit['required'])}"
    reasons: dict[str, str] = {
        "required": "een verplicht veld ontbreekt",
        "minItems": f"er is minimaal {limit} waarde nodig",
        "maxItems": f"er zijn maximaal {limit} waarden toegestaan",
        "minLength": f"de waarde moet minimaal {limit} tekens lang zijn",
        "maxLength": f"de waarde mag maximaal {limit} tekens lang zijn",
        "minimum": f"de waarde moet minimaal {limit} zijn",
        "maximum": f"de waarde mag maximaal {limit} zijn",
        "pattern": "de waarde heeft niet de vorm die het schema voorschrijft",
        "format": f"de waarde is geen geldige {limit}",
        "type": f"hier hoort een waarde van het type {limit}",
        "enum": "de waarde staat niet in de lijst met toegestane waarden",
        "uniqueItems": "de lijst bevat een dubbele waarde",
    }
    return reasons.get(keyword, f"de waarde is afgekeurd door schemaregel '{keyword}'")


def _unknown_fields_reason(error: ValidationError) -> str:
    """Name the fields the schema does not know here; a field NAME is not a value.

    Derived from the schema rather than from jsonschema's own message, which quotes the
    whole instance alongside the names.
    """
    base = "hier staat een veld dat het schema op deze plek niet kent"
    schema = error.schema if isinstance(error.schema, dict) else {}
    if not isinstance(error.instance, dict):
        return base
    allowed = set(schema.get("properties") or ())
    patterns = [re.compile(pattern) for pattern in (schema.get("patternProperties") or ())]
    unknown = sorted(
        key
        for key in error.instance
        if key not in allowed and not any(pattern.search(str(key)) for pattern in patterns)
    )
    return f"{base}: {', '.join(unknown)}" if unknown else base


def validate_project_schema(project_data: dict[str, Any], *, schema_version: SchemaVersion | None = None) -> None:
    """Validate a parsed project dict against the project schema.

    ``schema_version`` validates against the schema of that version; it defaults to the
    latest schema, which is what every write path wants: it validates the FINAL state, and
    that state is migrated.

    Raises ``ProjectSchemaError`` when the project does not conform. Its message is a Dutch
    user-facing error naming the first offending field path and why it was refused, never
    the refused value.
    """
    validator = _get_validator(schema_version)
    errors = sorted(validator.iter_errors(project_data), key=lambda e: list(e.absolute_path))
    if not errors:
        return

    first = errors[0]
    location = "/".join(str(part) for part in first.absolute_path) or "(root)"
    project_name = project_data.get("name", "(onbekend)") if isinstance(project_data, dict) else "(onbekend)"
    described = version_key(schema_version) if schema_version is not None else version_key(latest_schema_version())
    reason = _describe_violation(first)
    # Not jsonschema's own message: that one quotes the rejected value, and callers log this
    # message and show it in a browser (``web/router_env_vars.py``), where a stored secret or
    # attacker-chosen text must not end up. Field path plus reason says the same thing.
    message = (
        f"Projectbestand '{project_name}' is afgekeurd: het voldoet niet aan het projectschema "
        f"(versie {described}). Veld '{location}': {reason}"
    )
    # A pattern says nothing to the user; the description of the field does.
    if first.validator == "pattern" and isinstance(first.schema, dict) and first.schema.get("description"):
        message = f"{message}. {first.schema['description']}"
    # Not at ERROR: the message is carried on the exception and the caller logs it once
    # with context. Logging it here too turns one rejection into several ERR alerts.
    logger.debug(message)
    raise ProjectSchemaError(
        message,
        field_path=location if first.absolute_path else None,
        reason=reason,
    )


def validate_declared_project_schema(project_data: dict[str, Any]) -> None:
    """Validate RAW, pre-migration content against the schema of its declared version.

    This is the git-monitor gate. It runs before any migration on purpose: a
    hostile file that happens to migrate clean must not be written back under our
    own identity. Validating against the version the file DECLARES is what lets the
    latest schema drop a form whose migration is written.

    The declared version comes from the same untrusted file, so it is treated as
    input, not as a hint: a missing, malformed or unknown ``schema-version``
    is a rejection. There is deliberately no fallback to the newest schema (that
    would let an old file through against rules it never had to meet) and none to
    the loosest (that would let anything through by declaring version 1).

    Raises ``ProjectSchemaError`` on a rejected version declaration or a schema violation.
    """
    project_name = project_data.get("name", "(onbekend)") if isinstance(project_data, dict) else "(onbekend)"
    declared = project_data.get("schema-version") if isinstance(project_data, dict) else None
    invalid = ProjectSchemaError(
        f"Projectbestand '{project_name}' is afgekeurd: het declareert geen geldige 'schema-version'. "
        f"Een projectbestand moet zijn schemaversie noemen, zodat het tegen het schema van die versie "
        f"gecontroleerd kan worden."
    )
    if not isinstance(declared, int | float | str) or isinstance(declared, bool):
        raise invalid
    try:
        version = parse_schema_version(declared)
    except ProjectSchemaError:
        raise invalid from None
    if version not in known_schema_versions():
        raise ProjectSchemaError(
            f"Projectbestand '{project_name}' is afgekeurd: schemaversie {version_key(version)} is onbekend. "
            f"Bekende versies: {', '.join(version_key(v) for v in known_schema_versions())}."
        )
    validate_project_schema(project_data, schema_version=version)


# The marker of the schema's age-encrypted $defs pattern. Detection is derived from
# the schema rather than from a hand-written field list, so it cannot drift when a
# new secret field is added.
_AGE_PATTERN_MARKER = "BEGIN AGE ENCRYPTED FILE"


def _walk_errors(errors: Any) -> Any:
    """Yield validation errors depth-first, including anyOf/oneOf sub-errors."""
    for error in errors:
        yield error
        yield from _walk_errors(error.context or [])


def age_pattern_violations(validator: Draft202012Validator, data: Any, *, prefix: str = "") -> list[str]:
    """Paths in ``data`` that a schema wants AGE-encrypted but that hold plain text.

    Derived from the schema (a ``pattern`` carrying the AGE marker) and not from a field
    list, so it cannot drift. ``prefix`` names where ``data`` sits in a larger document.
    """
    violations: list[str] = []
    for error in _walk_errors(validator.iter_errors(data)):
        schema = error.schema if isinstance(error.schema, dict) else {}
        if _AGE_PATTERN_MARKER in str(schema.get("pattern", "")):
            path = "/".join(str(part) for part in error.absolute_path)
            violations.append("/".join(part for part in (prefix, path) if part) or "(root)")
    return sorted(set(violations))


def find_plaintext_secret_violations(project_data: dict[str, Any]) -> list[str]:
    """Field paths that must hold an AGE-encrypted value but do not.

    Separate from ``validate_project_schema`` because this specific class of
    violation must fail closed on EVERY write path. ``enforce_validation=False``
    exists so a recovery write is not blocked by pre-existing structural drift; it
    is not a licence to commit a decrypted secret, which is what writing back a
    ``get_decrypted()`` view would do.

    Covers only what ``project_v2.json`` describes. A secret that lives in a SERVICE
    config is described by that service's own model, so the same check on that half lives
    in ``project_validation.find_plaintext_service_config_violations``, and
    ``ProjectStore._validate`` runs both.

    Returns the offending field paths, empty when there are none.
    """
    return age_pattern_violations(_get_validator(), project_data)
