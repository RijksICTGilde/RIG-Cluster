#!/usr/bin/env python3
"""Generate the probe spec embedded in the e2e-allservices test image.

The e2e-allservices workload (``images/e2e-allservices/``) round-trips every
platform service a project binds it to. *What* it tests must stay in sync with
the platform automatically, so it is derived from a scan of OPI's own service
registry and ``*Variables`` enums rather than a hardcoded list.

This script imports :class:`opi.services.services.ServiceAdapter` and, for every
service that injects environment variables, emits the full set of variable names
the platform would inject (canonical names, ``APP_`` aliases, and the *computed*
keys the secret classes add - ``DATABASE_SERVER_FULL``, ``OBJECT_STORE_URL``,
``REDIS_URL``, ...). Services that exercise the same resource with the same
variables (e.g. ``postgresql-database`` and ``namespace-postgresql-database``,
which are mutually exclusive at runtime) collapse into one probe *target*.

The result is written to ``images/e2e-allservices/probe_spec.json`` and embedded
in the Go binary at build time. ``tests/test_probe_spec_drift.py`` regenerates it
and fails if the committed file drifts, so a new service or a new injected
variable that isn't reflected breaks the build instead of silently going
untested.

Usage::

    uv run python ../../scripts/generate_probe_spec.py            # write the file
    uv run python ../../scripts/generate_probe_spec.py --check     # exit 1 on drift
    uv run python ../../scripts/generate_probe_spec.py --stdout    # print, don't write

Run it from ``operations-manager/python`` (so ``opi`` is importable) or from
anywhere with that directory on ``PYTHONPATH``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

# Make ``opi`` importable no matter the working directory: the OPI package lives
# in operations-manager/python, a sibling of this scripts/ directory.
_OPI_ROOT = Path(__file__).resolve().parents[1] / "operations-manager" / "python"
if str(_OPI_ROOT) not in sys.path:
    sys.path.insert(0, str(_OPI_ROOT))

from opi.services.services import ServiceAdapter  # noqa: E402  (after sys.path bootstrap above)
from opi.services.services_enums import ServiceType  # noqa: E402
from opi.utils import secrets as secrets_module  # noqa: E402

# Spec format version. Bump only on a structural change to the JSON shape (new
# fields, renamed keys) - the Go loader pins to this. Adding services/variables
# does NOT bump it; that is data, picked up automatically by the scan.
SPEC_VERSION = 1

# The one deliberately hand-maintained mapping: which probe *kind* exercises each
# service, and which stable target id it reports under. A probe kind is a reusable
# round-trip handler in the Go binary (sql, redis, s3, oidc, path, metadata, vlam); the
# runtime dispatches on it. Adding a variable to an existing service, or a second
# service of an existing kind, needs NO change here - only a genuinely new *kind*
# of resource does (one new handler + one line below). See the plan in
# features/e2e-allservices-image.md.
#
# tuple = (probe_kind, target_id, primary connection var names). "primary" are the
# canonical var names whose presence marks the service bound; their aliases are
# added automatically. For the "metadata" kind (nothing to connect to, only echo)
# leave primary empty - boundness is "any of its vars present".
KIND_MAP: dict[ServiceType, tuple[str, str, tuple[str, ...]]] = {
    ServiceType.POSTGRESQL_DATABASE: ("sql", "postgres", ("DATABASE_SERVER_HOST",)),
    ServiceType.NAMESPACE_POSTGRESQL_DATABASE: ("sql", "postgres", ("DATABASE_SERVER_HOST",)),
    ServiceType.REDIS: ("redis", "redis", ("REDIS_HOST",)),
    ServiceType.NAMESPACE_REDIS: ("redis", "redis", ("REDIS_HOST",)),
    ServiceType.MINIO_STORAGE: ("s3", "minio", ("OBJECT_STORE_HOST",)),
    ServiceType.KEYCLOAK: ("oidc", "oidc", ("OIDC_DISCOVERY_URL", "OIDC_URL")),
    ServiceType.PERSISTENT_STORAGE: ("path", "storage-data", ("DATA_PATH",)),
    ServiceType.TEMP_STORAGE: ("path", "storage-temp", ("TEMP_PATH",)),
    ServiceType.PLATFORM: ("metadata", "platform", ()),
    ServiceType.PUBLISH_ON_WEB: ("metadata", "web", ()),
    ServiceType.METRICS_SCRAPER: ("metadata", "metrics", ()),
    # send-email is metadata en geen eigen probe-soort, en dat is een bewuste keuze.
    # De dienst injecteert wel degelijk verbindingsgegevens (SMTP_HOST, SMTP_USERNAME,
    # SMTP_PASSWORD, SMTP_FROM), dus SKIP zou onwaar zijn: die lijst is voor diensten
    # die niets te verbinden hebben. Een echte round-trip zou een nieuwe probe-soort
    # vragen die EHLO en AUTH doet tegen de relay, en dat is een wijziging in het
    # e2e-allservices-image zelf. Als metadata-doel wordt nu wel bewaakt dat de
    # variabelen daadwerkelijk geinjecteerd worden; wat niet bewaakt wordt is of ze
    # ook werken. Dat verschil is de moeite van een vervolgstap waard.
    ServiceType.SEND_EMAIL: ("metadata", "mail", ()),
    # vlam heeft sinds RC-144 een EIGEN probe-soort: hij haalt {VLAM_API_URL}/v1/models op.
    # Als metadata-doel bewaakte deze dienst alleen dat het adres in de pod aankwam, en dat
    # is precies de helft die nooit stukgaat -- de keten erachter (uitgaande netwerkregel,
    # inkomende regel op de proxy, de proxy zelf) werd met de hand gecurled. De reden om
    # dat toen niet te doen, "in de sandbox staat er geen VLAM achter het adres", is
    # weggenomen: de sandbox draagt nu een stub op de plaatshoudercoordinaten.
    ServiceType.VLAM: ("vlam", "vlam", ("VLAM_API_URL",)),
}

# Services that legitimately inject no connection variables and so have no probe.
# The scan asserts every OTHER service with variables is mapped above, so a new
# service can't be silently dropped from coverage.
SKIP: set[ServiceType] = {
    ServiceType.AUTHORIZATION_WALL,
    ServiceType.ATTACHMENTS,
    ServiceType.HEALTH_CHECK,
    ServiceType.SLEEP_MODE,
    ServiceType.INVITE,
    ServiceType.RESOURCE_TUNING,
    ServiceType.DEPLOYMENT_HEALTH,
    ServiceType.CROSS_DOMAIN_ACCESS,
    # System services that own a plain component property; they inject no connection
    # variables of their own -- they ARE the user's own variables (RC-25).
    ServiceType.USER_ENV_VARS,
    ServiceType.ALIASES,
}


def _dummy_secret_keys(secret_class_name: str) -> set[str]:
    """All env-var keys a secret class injects, including computed/alias keys.

    Instantiates the dataclass with placeholder values and calls
    ``to_k8s_secret_data()`` - the exact method the platform uses to build the
    injected secret - so computed keys (connection strings, endpoint URLs) are
    discovered, not guessed.
    """
    cls = getattr(secrets_module, secret_class_name)
    kwargs: dict[str, object] = {}
    for f in dataclasses.fields(cls):
        # Fields with a default are optional; omit them (placeholders are enough
        # to render every key, and some computed props read only required fields).
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        kwargs[f.name] = 1 if f.type in ("int", int) else "x"
    instance = cls(**kwargs)
    return set(instance.to_k8s_secret_data().keys())


def _service_env_vars(service: ServiceType) -> set[str]:
    """Every env-var name (canonical + aliases + computed) the service injects."""
    definition = ServiceAdapter.get_service_definition(service)
    names: set[str] = set()
    for var in definition.variables:
        names.update(var.get_all_names())
    if definition.secret_class:
        names.update(_dummy_secret_keys(definition.secret_class))
    return names


def _primary_bound_vars(service: ServiceType, primary: tuple[str, ...]) -> list[str]:
    """Expand the primary connection var names with their aliases."""
    definition = ServiceAdapter.get_service_definition(service)
    by_name = {var.name: var for var in definition.variables}
    names: list[str] = []
    for canonical in primary:
        names.append(canonical)
        var = by_name.get(canonical)
        if var:
            names.extend(var.aliases)
    return names


def build_spec() -> dict:
    """Scan the service registry and build the probe spec (deterministic)."""
    # Guard: any service that injects variables MUST be mapped or explicitly
    # skipped, so coverage tracks the platform instead of silently rotting.
    for service in ServiceType:
        definition = ServiceAdapter.SERVICE_DEFINITIONS.get(service)
        if definition is None:
            continue
        has_vars = bool(definition.variables)
        if service in KIND_MAP:
            continue
        if has_vars:
            raise SystemExit(
                f"Service '{service.value}' injects variables but is neither in KIND_MAP nor SKIP "
                f"in {Path(__file__).name}. Map it to a probe kind (or add it to SKIP if it truly "
                f"has nothing to round-trip) so the e2e-allservices image tests it."
            )
        if service not in SKIP:
            # Vars-less and unmapped: harmless, but flag unknown ones so a future
            # service that gains variables later isn't overlooked.
            raise SystemExit(
                f"Service '{service.value}' is not in KIND_MAP or SKIP. Add it to SKIP (no "
                f"connection variables) or KIND_MAP (has a resource to probe)."
            )

    targets: dict[str, dict] = {}
    for service, (kind, target_id, primary) in KIND_MAP.items():
        env_vars = _service_env_vars(service)
        entry = targets.setdefault(
            target_id,
            {"id": target_id, "kind": kind, "services": set(), "env_vars": set(), "bound_when_any": set()},
        )
        if entry["kind"] != kind:
            raise SystemExit(f"Target '{target_id}' mapped to conflicting kinds '{entry['kind']}' and '{kind}'.")
        entry["services"].add(service.value)
        entry["env_vars"].update(env_vars)
        if primary:
            entry["bound_when_any"].update(_primary_bound_vars(service, primary))

    # For metadata targets (no primary connection var), any injected var present
    # means bound.
    for entry in targets.values():
        if not entry["bound_when_any"]:
            entry["bound_when_any"] = set(entry["env_vars"])

    # Freeze to sorted lists so the JSON is byte-stable across runs.
    target_list = [
        {
            "id": entry["id"],
            "kind": entry["kind"],
            "services": sorted(entry["services"]),
            "bound_when_any": sorted(entry["bound_when_any"]),
            "env_vars": sorted(entry["env_vars"]),
        }
        for entry in targets.values()
    ]
    target_list.sort(key=lambda t: t["id"])
    return {"version": SPEC_VERSION, "targets": target_list}


def render(spec: dict) -> str:
    """Canonical JSON rendering (sorted keys, trailing newline)."""
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def _spec_path() -> Path:
    """Committed spec location: images/e2e-allservices/probe_spec.json (repo-relative)."""
    return Path(__file__).resolve().parents[1] / "images" / "e2e-allservices" / "probe_spec.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed spec is stale")
    parser.add_argument("--stdout", action="store_true", help="print the spec instead of writing it")
    args = parser.parse_args()

    spec = build_spec()
    rendered = render(spec)
    path = _spec_path()

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    if args.check:
        current = path.read_text() if path.exists() else ""
        if current != rendered:
            sys.stderr.write(
                f"probe spec is stale: {path} does not match the service registry.\n"
                f"Regenerate it: uv run python scripts/generate_probe_spec.py\n"
            )
            return 1
        sys.stdout.write("probe spec up to date\n")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered)
    sys.stdout.write(f"wrote {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
