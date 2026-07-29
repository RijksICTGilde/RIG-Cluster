#!/usr/bin/env python3
"""
Migrate a sandbox project file to the odcn-production cluster.

The mirror of ``migrate_project_to_sandbox.py``, plus a **structure downgrade**: the
production OPI predates the RC-5 service refactor, so service entries are written back
in the legacy name-as-key form and the file is stamped as schema-version 2.2.

What it does:
  1. schema-version -> 2.2 and clusters -> odcn-production
  2. Re-encrypts ``config.age-private-key`` from the sandbox cluster key to the
     production cluster key. Every other secret stays as-is: those are encrypted with
     the *project's* own key pair, and that pair travels with the file.
  3. Generates a fresh api-key (encrypted with the project's public key)
  4. Drops the Keycloak realm block: cluster-specific, OPI recreates it on the target
  5. Converts service entries back to the legacy form ({name: {config: ...}})
  6. Folds deployment-level plain ``env-vars`` into the AGE-encrypted ``user-env-vars``
     of the same component, so the values survive on a portal that only shows the latter
  7. Rewrites users, repositories and per-deployment cluster/domain/issuer
  8. Validates the result against the project schema before writing

Usage:
    cd operations-manager/python
    uv run python scripts/migrate_project_to_production.py <source.yaml> [options]

Options:
    --sandbox-key PATH   Sandbox AGE key file (default: ../../security/sandbox-key.txt)
    --prod-key PATH      Production AGE key file (default: ../../security/key.txt)
    --output PATH        Output file (default: alongside the source, .prod.yaml)
    --base-domain NAME   Production base domain (default: rijks.app)
    --admin EMAIL        Admin user for the target (repeatable)
    --set KEY=VALUE      Extra env-var to add to the first component (repeatable)
"""

import argparse
import json
import logging
import secrets
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import jsonschema
from opi.utils.age import decrypt_age_content_sync, encrypt_age_content_sync
from opi.utils.yaml_util import load_yaml_from_path, save_yaml_to_path
from ruamel.yaml.scalarstring import LiteralScalarString

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TARGET_CLUSTER = "odcn-production"
TARGET_SCHEMA_VERSION = 2.2
DEFAULT_BASE_DOMAIN = "rijksapp.dev"
DEFAULT_ISSUER = "letsencrypt"
SOURCE_DEFAULT_DOMAIN = "sandbox.rijksapp.dev"
PRODUCTION_REPOSITORIES = [
    {
        "name": "main-repo",
        "url": "https://github.com/RijksICTGilde/rig-cluster-application-test.git",
        "username": "git",
        # Alle productie-repos delen dezelfde credential en de waarde is niet met de
        # projectsleutel versleuteld, dus hij is letterlijk overneembaar.
        "password": "base64+age:LS0tLS1CRUdJTiBBR0UgRU5DUllQVEVEIEZJTEUtLS0tLQpZV2RsTFdWdVkzSjVjSFJwYjI0dWIzSm5MM1l4Q2kwK0lGZ3lOVFV4T1NCd1lVWk1ZVFZ1Ukd4dmJrUjZNelZRCksxbFVVVE5qWm5wNllYRjBUVXBsWjJWc2VuSjRRa00xZW1wdkNpOUdNakpQVldSTWNrUnRSakUyWTNObVlXcFUKZEZOMGMzZHlVbG8wY0ZkTFQwWnhhWFZ4U21wVmNVMEtMUzB0SUdReFJtUldWSGRhTVdVd1dqaHRSVW92WnlzeQpkVTlNWmpSMFZWSjFTWFZIVDFZd2NIZFZVekJwY1RnSzNvYVR4b3YwRW1RcVkrRjlTWkgzVjBONHFXd25ESEllCjI4U05ud2ZxaWthQWE1dGNWcmIvOW4xM3BLN3NEQVQ2bXpZS3NKeFhxdDV0UnpJeWxUWHk5dkk0REticmRiSmkKLS0tLS1FTkQgQUdFIEVOQ1JZUFRFRCBGSUxFLS0tLS0=",
        "branch": "main",
        "path": ".",
    }
]


def read_age_key_file(path: str) -> tuple[str, str]:
    """Return (public_key, private_key) from an age key file.

    Format is age-keygen's: a ``# public key: ...`` comment line and the
    ``AGE-SECRET-KEY-...`` line.
    """
    public_key = ""
    private_key = ""
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line.startswith("# public key:"):
            public_key = line.split(":", 1)[1].strip()
        elif line.startswith("AGE-SECRET-KEY-"):
            private_key = line
    if not private_key:
        raise ValueError(f"No AGE-SECRET-KEY found in {path}")
    return public_key, private_key


# ---------------------------------------------------------------------------
# Service entry format: record -> legacy
# ---------------------------------------------------------------------------


def _to_legacy_entry(entry: Any) -> Any:
    """Convert a ``{name|reference, config}`` record back to ``{name: {config}}``.

    The production OPI predates the uniform record form, so it reads the legacy shape.
    Bare strings stay bare, legacy entries are returned untouched.
    """
    if not isinstance(entry, dict):
        return entry
    name = entry.get("name") or entry.get("reference")
    if name is None:
        return entry  # already legacy
    body = {k: v for k, v in entry.items() if k not in ("name", "reference")}
    body.pop("schema-version", None)
    return {name: body} if body else name


def downgrade_service_entries(project: dict[str, Any]) -> int:
    """Rewrite every services list to the legacy form. Returns the number changed."""
    changed = 0

    def convert(container: dict[str, Any]) -> None:
        nonlocal changed
        entries = container.get("services")
        if not isinstance(entries, list):
            return
        for i, entry in enumerate(entries):
            legacy = _to_legacy_entry(entry)
            if legacy is not entry:
                entries[i] = legacy
                changed += 1

    convert(project)
    for component in project.get("components", []) or []:
        if isinstance(component, dict):
            convert(component)
    for deployment in project.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        convert(deployment)
        for component in deployment.get("components", []) or []:
            if isinstance(component, dict):
                convert(component)
    return changed


# ---------------------------------------------------------------------------
# env-vars -> user-env-vars
# ---------------------------------------------------------------------------


def fold_env_vars_into_user_env_vars(
    project: dict[str, Any],
    project_private_key: str,
    project_public_key: str,
    extra: dict[str, str],
    host_rewrites: dict[str, str],
) -> None:
    """Merge deployment-level plain ``env-vars`` into the encrypted ``user-env-vars``.

    ``env-vars`` is a file-only field: the portal never shows it, so anything living
    there is invisible and uneditable to a user on the target. Folding the values into
    the component's ``user-env-vars`` keeps them editable, at the cost of putting
    non-secret values behind encryption.
    """
    components_by_name = {
        c.get("name"): c for c in project.get("components", []) or [] if isinstance(c, dict) and c.get("name")
    }

    for deployment in project.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        for dep_component in deployment.get("components", []) or []:
            if not isinstance(dep_component, dict):
                continue
            plain = dep_component.pop("env-vars", None) or {}
            reference = dep_component.get("reference")
            target = components_by_name.get(reference)
            if target is None:
                if plain:
                    logger.warning("  Component '%s' not found; %d env-vars dropped", reference, len(plain))
                continue

            merged: dict[str, str] = {}
            existing = target.get("user-env-vars")
            if isinstance(existing, str) and existing.strip():
                decrypted = decrypt_age_content_sync(existing.strip(), project_private_key)
                if decrypted is None:
                    raise RuntimeError(f"Could not decrypt user-env-vars of component '{reference}'")
                merged.update(json.loads(decrypted) if decrypted.lstrip().startswith("{") else _parse_kv(decrypted))
            elif isinstance(existing, dict):
                merged.update(existing)

            merged.update({str(k): str(v) for k, v in plain.items()})
            if reference == _first_component_name(project):
                merged.update(extra)

            # A value may embed the project's own public hostname (headscale's
            # --login-server is the obvious one). That host is derived from the base
            # domain, so it moves with the cluster; left alone the target would keep
            # pointing at the source.
            for old_host, new_host in host_rewrites.items():
                for key, value in merged.items():
                    if old_host in value:
                        merged[key] = value.replace(old_host, new_host)
                        logger.info("  [%s] %s: %s -> %s", reference, key, old_host, new_host)

            if not merged:
                continue
            body = "\n".join(f"{k}={v}" for k, v in merged.items())
            target["user-env-vars"] = LiteralScalarString(encrypt_age_content_sync(body, project_public_key))
            logger.info("  [%s] %d env-vars folded into user-env-vars", reference, len(merged))


def _parse_kv(text: str) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` per line blob, ignoring blanks and comments."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _first_component_name(project: dict[str, Any]) -> str | None:
    components = project.get("components") or []
    return components[0].get("name") if components and isinstance(components[0], dict) else None


# ---------------------------------------------------------------------------


def migrate(
    data: dict[str, Any],
    sandbox_private_key: str,
    prod_public_key: str,
    base_domain: str,
    admins: list[str],
    extra_env: dict[str, str],
) -> dict[str, Any]:
    project = deepcopy(data)
    name = project.get("name", "unknown")

    project["schema-version"] = TARGET_SCHEMA_VERSION
    project["clusters"] = [TARGET_CLUSTER]
    logger.info("  [%s] schema-version -> %s, clusters -> [%s]", name, TARGET_SCHEMA_VERSION, TARGET_CLUSTER)

    config = project.get("config", {})
    project_public_key = config.get("age-public-key")
    if not project_public_key:
        raise RuntimeError(f"No age-public-key in {name}; cannot re-encrypt anything")

    encrypted_private = config.get("age-private-key")
    if not encrypted_private:
        raise RuntimeError(f"No age-private-key in {name}")
    project_private_key = decrypt_age_content_sync(str(encrypted_private).strip(), sandbox_private_key)
    if project_private_key is None:
        raise RuntimeError(f"Could not decrypt age-private-key of {name} with the sandbox key")
    config["age-private-key"] = LiteralScalarString(encrypt_age_content_sync(project_private_key, prod_public_key))
    logger.info("  [%s] age-private-key re-encrypted for the production cluster key", name)

    config["api-key"] = LiteralScalarString(encrypt_age_content_sync(secrets.token_urlsafe(32), project_public_key))
    logger.info("  [%s] new api-key generated", name)

    # Public hostname per deployment moves with the base domain; collect the mapping
    # before the deployments are rewritten below.
    host_rewrites: dict[str, str] = {}
    for deployment in project.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        subdomain = deployment.get("subdomain")
        old_base = deployment.get("base-domain")
        if subdomain and old_base and old_base != base_domain:
            host_rewrites[f"{subdomain}.{old_base}"] = f"{subdomain}.{base_domain}"
    if not host_rewrites:
        # Sandbox deployments often omit base-domain (it is the cluster default).
        for deployment in project.get("deployments", []) or []:
            subdomain = deployment.get("subdomain") if isinstance(deployment, dict) else None
            if subdomain:
                host_rewrites[f"{subdomain}.{SOURCE_DEFAULT_DOMAIN}"] = f"{subdomain}.{base_domain}"

    fold_env_vars_into_user_env_vars(project, project_private_key, project_public_key, extra_env, host_rewrites)

    # Keycloak realm block is cluster-specific (host, realm name, admin password).
    # OPI recreates it on the target, so it must not travel.
    for entry in project.get("services", []) or []:
        if not isinstance(entry, dict):
            continue
        entry_name = entry.get("name") or next((k for k in entry if k != "config"), None)
        if entry_name != "keycloak":
            continue
        body = entry.get("config") if "name" in entry else entry.get("keycloak", {}).get("config", {})
        if isinstance(body, dict) and body.pop("realms", None) is not None:
            logger.info("  [%s] dropped keycloak realms (recreated on the target)", name)

    # Domain approvals are per base domain: an approved subdomain on the sandbox domain
    # says nothing about the target and would present itself as already granted.
    for entry in project.get("services", []) or []:
        if not isinstance(entry, dict):
            continue
        entry_name = entry.get("name") or next((k for k in entry if k != "config"), None)
        if entry_name != "publish-on-web":
            continue
        body = entry.get("config") if "name" in entry else (entry.get("publish-on-web") or {}).get("config", {})
        if isinstance(body, dict) and body.pop("domains", None) is not None:
            logger.info("  [%s] dropped domain approvals (re-request on the target)", name)

    changed = downgrade_service_entries(project)
    logger.info("  [%s] %d service entries downgraded to the legacy form", name, changed)

    if admins:
        project["users"] = [{"email": email, "role": "admin"} for email in admins]
        logger.info("  [%s] users -> %s", name, ", ".join(admins))

    project["repositories"] = deepcopy(PRODUCTION_REPOSITORIES)
    logger.info("  [%s] repositories -> production", name)

    for deployment in project.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        deployment["cluster"] = TARGET_CLUSTER
        deployment["base-domain"] = base_domain
        deployment["issuer"] = DEFAULT_ISSUER
        deployment["repository"] = "main-repo"
        logger.info("  [%s] deployment '%s': cluster/base-domain/issuer set", name, deployment.get("name", "?"))

    return project


def validate(project: dict[str, Any], schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text())
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(project), key=lambda e: list(e.path))
    if errors:
        for error in errors[:10]:
            logger.error("  schema: %s -> %s", "/".join(str(p) for p in error.path), error.message[:160])
        raise SystemExit("Result does not validate against the project schema; nothing written")
    logger.info("  schema validation passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="Source project YAML")
    parser.add_argument("--sandbox-key", default="../../security/sandbox-key.txt")
    parser.add_argument("--prod-key", default="../../security/key.txt")
    parser.add_argument("--output")
    parser.add_argument("--base-domain", default=DEFAULT_BASE_DOMAIN)
    parser.add_argument("--admin", action="append", default=[])
    parser.add_argument("--set", action="append", default=[], dest="extra", help="KEY=VALUE env-var")
    args = parser.parse_args()

    _, sandbox_private_key = read_age_key_file(args.sandbox_key)
    prod_public_key, _ = read_age_key_file(args.prod_key)
    if not prod_public_key:
        raise SystemExit(f"No '# public key:' line in {args.prod_key}")

    extra_env = dict(item.split("=", 1) for item in args.extra)

    source = Path(args.source)
    data = load_yaml_from_path(str(source))
    logger.info("Migrating %s", source)

    result = migrate(data, sandbox_private_key, prod_public_key, args.base_domain, args.admin, extra_env)

    schema_path = Path(__file__).parent.parent / "opi" / "schemas" / "project_v2.json"
    validate(result, schema_path)

    output = Path(args.output) if args.output else source.with_suffix(".prod.yaml")
    save_yaml_to_path(str(output), result)
    logger.info("Written to %s", output)


if __name__ == "__main__":
    sys.exit(main())
