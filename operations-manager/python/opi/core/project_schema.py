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

import json
import logging
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

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
    """

    def __init__(self, message: str, *, field_path: str | None = None) -> None:
        super().__init__(message)
        self.field_path = field_path


class ProjectIntegrityError(Exception):
    """Raised when a project file is schema-valid but structurally inconsistent.

    Covers the cross-field checks the JSON schema cannot express: duplicate
    component/deployment names, dangling component references, colliding ingress
    paths, invalid root components and hard domain-config violations. Like
    ProjectSchemaError the message is user-facing and in Dutch, and the caller
    must reject the project (fails closed).
    """


def version_key(version: int | float) -> str:
    """Canonical file-name key for a schema version: 2.0 -> "2", 2.6 -> "2.6"."""
    return f"{float(version):g}"


@lru_cache(maxsize=1)
def _load_latest_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as schema_file:
        schema: dict[str, Any] = json.load(schema_file)
    return schema


def latest_schema_version() -> float:
    """The version the committed latest schema describes."""
    version = _load_latest_schema().get(SCHEMA_VERSION_KEY)
    if not isinstance(version, int | float):
        raise ProjectSchemaError(
            f"Het projectschema mist de annotatie '{SCHEMA_VERSION_KEY}'; de schemaversie is niet vast te stellen."
        )
    return float(version)


@lru_cache(maxsize=1)
def _legacy_patch_versions() -> tuple[float, ...]:
    """Versions that have a legacy patch on disk, oldest first."""
    return tuple(sorted(float(path.stem[1:]) for path in LEGACY_PATCH_DIR.glob("v*.json")))


def known_schema_versions() -> tuple[float, ...]:
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
def _get_validator(version: float | None = None) -> Draft202012Validator:
    """Validator for one schema version. ``None`` means the latest.

    An older version is composed by applying every legacy patch from the latest
    down to the requested version, in order.
    """
    schema = _load_latest_schema()
    if version is not None and version != latest_schema_version():
        if version not in _legacy_patch_versions():
            raise ProjectSchemaError(
                f"Onbekende schemaversie '{version}': er is geen schema voor deze versie. "
                f"Bekende versies: {', '.join(version_key(v) for v in known_schema_versions())}."
            )
        for patch_version in sorted(_legacy_patch_versions(), reverse=True):
            if patch_version < version:
                break
            with (LEGACY_PATCH_DIR / f"v{version_key(patch_version)}.json").open(encoding="utf-8") as patch_file:
                schema = _merge_patch(schema, json.load(patch_file))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def check_schema_versions(migration_versions: tuple[int | float, ...]) -> None:
    """Fail loudly when a migration exists without a schema for its version.

    ``migration_versions`` is the full chain a project file can be stamped with
    (``schema_migration.SCHEMA_VERSIONS``). Every one of them must be validatable:
    the newest by the latest schema, each older one by a legacy patch. Adding a
    migration without adding its patch is a programming error and stops startup
    here rather than surfacing as a rejected project file months later.

    Raises:
        ProjectSchemaError: if the chain and the schemas on disk disagree.
    """
    latest = latest_schema_version()
    if float(migration_versions[-1]) != latest:
        raise ProjectSchemaError(
            f"Het projectschema beschrijft versie {version_key(latest)} maar de migratieketen eindigt op "
            f"{version_key(migration_versions[-1])}; werk project_v2.json bij of voeg de ontbrekende migratie toe."
        )
    known = set(known_schema_versions())
    missing = [version_key(v) for v in migration_versions if float(v) not in known]
    if missing:
        raise ProjectSchemaError(
            f"Geen schema voor schemaversie(s) {', '.join(missing)}: voeg "
            f"opi/schemas/project_legacy/v<versie>.json toe voor elke migratie."
        )
    extra = [version_key(v) for v in sorted(known) if v not in {float(m) for m in migration_versions}]
    if extra:
        raise ProjectSchemaError(
            f"Schema aanwezig voor onbekende versie(s) {', '.join(extra)}: die staan niet in de migratieketen."
        )


def validate_project_schema(project_data: dict[str, Any], *, schema_version: float | None = None) -> None:
    """Validate a parsed project dict against the project schema.

    Args:
        project_data: The parsed project file content.
        schema_version: Validate against the schema of this version. Defaults to
            the latest schema, which is what every write path wants: it validates
            the FINAL state, and that state is migrated.

    Raises:
        ProjectSchemaError: If the project does not conform to the schema.
            The exception message is a Dutch user-facing error that names the
            first offending field path and the validation message.
    """
    validator = _get_validator(schema_version)
    errors = sorted(validator.iter_errors(project_data), key=lambda e: list(e.absolute_path))
    if not errors:
        return

    first = errors[0]
    location = "/".join(str(part) for part in first.absolute_path) or "(root)"
    project_name = project_data.get("name", "(onbekend)") if isinstance(project_data, dict) else "(onbekend)"
    described = version_key(schema_version) if schema_version is not None else version_key(latest_schema_version())
    message = (
        f"Projectbestand '{project_name}' is afgekeurd: het voldoet niet aan het projectschema "
        f"(versie {described}). Veld '{location}': {first.message}"
    )
    # Do not log at ERROR here: the message is carried on the exception and the
    # caller logs it once with context. Self-logging made one rejection surface as
    # several ERR alerts (validator + orchestrator + task-progress). debug keeps a
    # breadcrumb without feeding the log-watch.
    logger.debug(message)
    raise ProjectSchemaError(message, field_path=location if first.absolute_path else None)


def validate_declared_project_schema(project_data: dict[str, Any]) -> None:
    """Validate RAW, pre-migration content against the schema of its declared version.

    This is the git-monitor gate. It runs before any migration on purpose: a
    hostile file that happens to migrate clean must not be written back under our
    own identity. Validating it against the latest schema was the reason the
    latest schema could never drop an old form; validating it against the version
    the file declares removes that pressure without opening the gate.

    The declared version comes from the same untrusted file, so it is treated as
    input, not as a hint: a missing, non-numeric or unknown ``schema-version``
    is a rejection. There is deliberately no fallback to the newest schema (that
    would let an old file through against rules it never had to meet) and none to
    the loosest (that would let anything through by declaring version 1).

    Raises:
        ProjectSchemaError: on a rejected version declaration or a schema violation.
    """
    project_name = project_data.get("name", "(onbekend)") if isinstance(project_data, dict) else "(onbekend)"
    declared = project_data.get("schema-version") if isinstance(project_data, dict) else None
    if not isinstance(declared, int | float) or isinstance(declared, bool):
        raise ProjectSchemaError(
            f"Projectbestand '{project_name}' is afgekeurd: het declareert geen geldige 'schema-version'. "
            f"Een projectbestand moet zijn schemaversie noemen, zodat het tegen het schema van die versie "
            f"gecontroleerd kan worden."
        )
    if float(declared) not in known_schema_versions():
        raise ProjectSchemaError(
            f"Projectbestand '{project_name}' is afgekeurd: schemaversie {version_key(declared)} is onbekend. "
            f"Bekende versies: {', '.join(version_key(v) for v in known_schema_versions())}."
        )
    validate_project_schema(project_data, schema_version=float(declared))


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
