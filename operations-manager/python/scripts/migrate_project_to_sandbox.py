#!/usr/bin/env python3
"""
Migrate production project files to sandbox cluster.

Reads a production project YAML, transforms it for the sandboxed-local cluster,
and writes the result to an output directory. The output can then be pushed to
the sandbox Forgejo zad-projects repo.

Usage:
    cd operations-manager/python
    uv run python scripts/migrate_project_to_sandbox.py <project-name> [options]

    # Multiple files:
    uv run python scripts/migrate_project_to_sandbox.py amt-dev wies regel-k4c

Options:
    --prod-key PATH       Production AGE key file (default: ../../security/key.txt)
    --sandbox-key PATH    Sandbox AGE key file (default: ../../security/sandbox-key.txt)
    --sandbox-public-key AGE1...  Target recipient directly; the private half stays on that
                          cluster. Use this when the target cluster minted its own key.
    --output-dir PATH     Output directory (default: /tmp/sandbox-projects)
    --source-dir PATH     Source directory for project files
    --probe-image [IMG]   Replace every component workload with the e2e-allservices
                          probe and move each component's inbound port to the probe
                          port. Without a value uses the default probe image. Omit to
                          keep the original images and ports.
    --probe-port PORT     Probe inbound port (default 8080; only used with --probe-image)
    --keep-domains        Laat base-domain staan zoals productie hem heeft (upgrade-veiligheidstest)

    # Upgrade-safety test (RC-19): swap in the probe so /status verifies each binding:
    uv run python scripts/migrate_project_to_sandbox.py wies regelrecht moza amt --probe-image
"""

import argparse
import logging
import os
import sys
from copy import deepcopy
from pathlib import Path

# Make ``opi`` importable no matter the working directory: the OPI package lives in
# operations-manager/python, the parent of this scripts/ directory. Without this the
# documented ``uv run python scripts/migrate_project_to_sandbox.py`` fails with
# ModuleNotFoundError, because Python puts scripts/ (not the package root) on sys.path.
_OPI_ROOT = Path(__file__).resolve().parents[1]
if str(_OPI_ROOT) not in sys.path:
    sys.path.insert(0, str(_OPI_ROOT))

from opi.core.cluster_config import get_domain_supports_dots  # noqa: E402
from opi.utils.age import decrypt_age_content_sync, encrypt_age_content_sync  # noqa: E402  (after sys.path bootstrap)
from opi.utils.api_keys import generate_api_key  # noqa: E402
from opi.utils.env_vars import _detect_env_var_format, validate_and_parse_env_vars  # noqa: E402
from opi.utils.yaml_util import (  # noqa: E402
    load_yaml_from_path,
    save_yaml_to_path,
)
from ruamel.yaml.scalarstring import LiteralScalarString  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TARGET_CLUSTER = "sandboxed-local"
#: Where the files come from; used to read the domain capabilities they rely on.
SOURCE_CLUSTER = "odcn-production"
SANDBOX_DOMAIN = "sandbox.rijksapp.dev"

# The e2e-allservices probe: a static workload that binds every service it is given
# and reports the round-trip on /status. Public, so no pull secret is needed.
# It listens on 8080, while production components declare their own ports -- so when
# we swap the workload in we must also move each component's inbound port to 8080,
# otherwise the health/ingress path points at a port nothing is listening on.
DEFAULT_PROBE_IMAGE = "ghcr.io/minbzk/base-images/e2e-allservices:latest"
DEFAULT_PROBE_PORT = 8080

#: Value written over every user-env-var. Recognisable on sight in a manifest or a pod,
#: so nobody mistakes a sandbox copy for real configuration.
SANDBOX_ENV_PLACEHOLDER = "SANDBOX-PLACEHOLDER"
SANDBOX_ADMIN = {"email": f"admin@{SANDBOX_DOMAIN}", "role": "admin"}
SANDBOX_REPOSITORIES = [
    {
        "name": "main-repo",
        "url": f"https://forgejo.{SANDBOX_DOMAIN}/rig-admin/zad-deployments.git",
        "username": "rig-admin",
        "password": "plain:admin1234",
        "branch": "main",
        "path": ".",
    }
]

DEFAULT_SOURCE_DIR = (
    "/Users/robbertuittenbroek/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github/projects"
)


def read_age_key_file(path: str) -> tuple[str, str]:
    """Read an AGE key file and return (public_key, private_key).

    AGE key files have the format:
        # created: ...
        # public key: age1...
        AGE-SECRET-KEY-...
    """
    with open(path) as f:
        lines = f.readlines()

    public_key = ""
    private_key = ""
    for line in lines:
        line = line.strip()
        if line.startswith("# public key:"):
            public_key = line.split("# public key:")[1].strip()
        elif line.startswith("AGE-SECRET-KEY-"):
            private_key = line

    if not public_key or not private_key:
        raise ValueError(f"Could not parse AGE key file: {path}")

    return public_key, private_key


def apply_probe_workload(project: dict, image: str, port: int) -> None:
    """Replace every component workload with the e2e-allservices probe, in place.

    Two rewrites, both required for the probe to come up healthy:
    - Each deployment component's ``image`` becomes the probe image, and its
      ``registry`` / ``imagePullPolicy`` are dropped: the probe is public, so it
      needs no pull secret, and a stale registry reference would break the pull.
    - Each top-level component's inbound port becomes the probe port (8080). The
      probe binds only 8080; the ingress and readiness probe target the component's
      declared inbound port, so without this the health checks would fail.

    Outbound ports, path routing and service bindings are left untouched -- the point
    of the probe is to exercise exactly the services the project file declares.
    """
    name = project.get("name", "unknown")

    for component in project.get("components", []):
        if not isinstance(component, dict):
            continue
        ports = component.setdefault("ports", {})
        if isinstance(ports, dict):
            ports["inbound"] = [port]
            logger.info(f"  [{name}] component '{component.get('name', '?')}': inbound port -> {port}")

    for dep in project.get("deployments", []):
        if not isinstance(dep, dict):
            continue
        for comp in dep.get("components", []):
            if not isinstance(comp, dict):
                continue
            comp["image"] = image
            comp.pop("registry", None)
            comp.pop("imagePullPolicy", None)
            logger.info(
                f"  [{name}] deployment '{dep.get('name', '?')}' component '{comp.get('reference', '?')}': "
                f"image -> {image}"
            )


def migrate_project(
    data: dict,
    prod_private_key: str,
    sandbox_public_key: str,
    probe_image: str | None = None,
    probe_port: int = DEFAULT_PROBE_PORT,
    keep_domains: bool = False,
) -> dict:
    """Apply all transformations to convert a production project to sandbox.

    When ``probe_image`` is given, every component workload is additionally replaced
    with the e2e-allservices probe (see ``apply_probe_workload``); otherwise the
    original images and ports are kept so the script stays usable for a plain migration.

    ``keep_domains`` leaves ``base-domain`` alone. Rewriting it to the sandbox domain is
    right for the script's original purpose (get a production project running in the
    sandbox, where a resolvable address matters), but wrong for the upgrade-safety test:
    the hostname is exactly what that test compares, and rewriting it changes the ingress,
    the TLS secret, the external-dns annotation and the generated PUBLIC_HOST. It also
    creates a combination that exists nowhere, since ``domain-format`` is left as-is: a
    dot format like ``component.subdomain`` on a domain whose cluster config says
    ``supports_dots: false`` is rejected by DomainConfigEnforcer. Nothing needs the
    address to resolve either: the probe's /status is read over a port-forward, and the
    sandbox runs no cert-manager.
    """
    project = deepcopy(data)
    name = project.get("name", "unknown")
    # Kept from step 2 so later steps can read blocks encrypted with the project's own
    # key (user-env-vars); after step 2 the stored copy is sandbox-encrypted.
    project_private_key: str | None = None

    # 1. Clusters -> sandboxed-local
    project["clusters"] = [TARGET_CLUSTER]
    logger.info(f"  [{name}] clusters -> [{TARGET_CLUSTER}]")

    # 2. Re-encrypt age-private-key: prod cluster key -> sandbox cluster key
    config = project.get("config", {})
    age_private_key_encrypted = config.get("age-private-key")
    project_public_key = config.get("age-public-key")

    if age_private_key_encrypted:
        raw_content = str(age_private_key_encrypted).strip()
        logger.info(f"  [{name}] Re-encrypting age-private-key with sandbox cluster key")
        decrypted = decrypt_age_content_sync(raw_content, prod_private_key)
        if decrypted is None:
            raise RuntimeError(f"Failed to decrypt age-private-key for {name}")
        project_private_key = decrypted
        re_encrypted = encrypt_age_content_sync(decrypted, sandbox_public_key)
        config["age-private-key"] = LiteralScalarString(re_encrypted)
    else:
        logger.warning(f"  [{name}] No age-private-key found in config")

    # 3. Generate new API key (encrypted with project's own public key)
    if project_public_key:
        # OPI's own generator, not token_urlsafe: that produces 43 chars with '-' and '_',
        # while every real key is 32 alphanumerics. sandbox_project_tool.py scrapes the key
        # off the details page by matching exactly 32 chars, so a urlsafe token silently
        # yielded nothing there.
        new_api_key = generate_api_key()
        encrypted_api_key = encrypt_age_content_sync(new_api_key, project_public_key)
        config["api-key"] = LiteralScalarString(encrypted_api_key)
        logger.info(f"  [{name}] Generated new API key")
    else:
        logger.warning(f"  [{name}] No age-public-key found, cannot generate API key")

    # 4. Drop config.keycloak (generated cluster-specific config)
    if "keycloak" in config:
        del config["keycloak"]
        logger.info(f"  [{name}] Dropped config.keycloak (will be regenerated by OPI)")

    # 5. Users -> sandbox admin
    project["users"] = [SANDBOX_ADMIN]
    logger.info(f"  [{name}] users -> [{SANDBOX_ADMIN['email']}]")

    # 6. Drop backup config
    if "backup" in project:
        del project["backup"]
        logger.info(f"  [{name}] Dropped backup config")

    # 7. Replace repositories with sandbox Forgejo config
    project["repositories"] = deepcopy(SANDBOX_REPOSITORIES)
    logger.info(f"  [{name}] Replaced repositories with sandbox Forgejo config")

    # 8. Transform deployments
    deployments = project.get("deployments", [])
    for dep in deployments:
        dep_name = dep.get("name", "?")

        # Change cluster
        if "cluster" in dep:
            dep["cluster"] = TARGET_CLUSTER
            logger.info(f"  [{name}] deployment '{dep_name}': cluster -> {TARGET_CLUSTER}")

        # Change base-domain, unless the caller wants the production hostnames kept.
        if "base-domain" in dep and not keep_domains:
            dep["base-domain"] = SANDBOX_DOMAIN
            logger.info(f"  [{name}] deployment '{dep_name}': base-domain -> {SANDBOX_DOMAIN}")

        # Drop issuer (sandbox uses its own cert)
        if "issuer" in dep:
            del dep["issuer"]
            logger.info(f"  [{name}] deployment '{dep_name}': dropped issuer")

        # Drop configuration (production-encrypted deployment config)
        if "configuration" in dep:
            del dep["configuration"]
            logger.info(f"  [{name}] deployment '{dep_name}': dropped configuration")

        # Replace repository reference with sandbox default
        dep["repository"] = "main-repo"
        logger.info(f"  [{name}] deployment '{dep_name}': repository -> main-repo")

        # Strip clone-from status (keep structure, but it hasn't been cloned yet)
        clone_from = dep.get("clone-from")
        if isinstance(clone_from, dict) and "status" in clone_from:
            del clone_from["status"]
            logger.info(f"  [{name}] deployment '{dep_name}': stripped clone-from status")

        # Strip service revision state (keep references, drop config with revisions)
        for svc in dep.get("services", []):
            if isinstance(svc, dict) and "config" in svc:
                del svc["config"]
                logger.info(f"  [{name}] deployment '{dep_name}': stripped service '{svc.get('name', '?')}' config")

        # Strip component-level service revision state
        for comp in dep.get("components", []):
            if isinstance(comp, dict) and "services" in comp:
                for svc_name, svc_entries in list(comp["services"].items()):
                    if isinstance(svc_entries, list):
                        for entry in svc_entries:
                            if isinstance(entry, dict) and "config" in entry:
                                del entry["config"]
                                logger.info(
                                    f"  [{name}] deployment '{dep_name}' component '{comp.get('name', '?')}': stripped {svc_name} config"
                                )

    replace_user_env_var_values(project, project_private_key, project_public_key, name)
    replace_alias_values(project, name)

    if keep_domains:
        carry_domain_capabilities(project, name)

    if probe_image:
        apply_probe_workload(project, probe_image, probe_port)

    return project


def carry_domain_capabilities(project: dict, name: str) -> None:
    """Write the source cluster's domain capabilities into the project's own domain list.

    Whether a dot format is allowed is looked up per domain in the TARGET cluster's config,
    with the project's own ``allowed-domains`` entry as the fallback for domains that
    cluster does not know. Production domains are in the odcn-production config, so a
    production file never needs that fallback and therefore does not carry ``supports-dots``.
    Move such a file to any other cluster and the fallback answers "no", so
    ``regel-k4c`` (component.subdomain on rijks.app) is rejected there while being perfectly
    valid in production.

    Copying the source cluster's answer into the file makes it self-describing, which is
    exactly what that fallback is for, and keeps the generated hostname identical to
    production instead of degrading to a dash format.
    """
    # Two locations, because this runs on the RAW file: before v2.5 the block sits at the
    # project root, after it under the publish-on-web service. Most production files are
    # still pre-v2.5 (regel-k4c is on 2.2), so looking only under the service finds nothing.
    entries = []
    for domains in (project.get("domains"), _find_publish_on_web_domains(project)):
        if isinstance(domains, dict):
            entries.extend(domains.get("allowed-domains") or [])
    carried = 0
    for entry in entries:
        if not isinstance(entry, dict) or "supports-dots" in entry:
            continue
        domain = entry.get("domain")
        if domain and get_domain_supports_dots(SOURCE_CLUSTER, domain):
            entry["supports-dots"] = True
            carried += 1
    if carried:
        logger.info(f"  [{name}] Carried supports-dots from {SOURCE_CLUSTER} for {carried} domain(s)")


def _find_publish_on_web_domains(project: dict) -> dict | None:
    for entry in project.get("services") or []:
        if isinstance(entry, dict):
            name = entry.get("name") or next(iter(entry), None)
            config = entry.get("config") or (entry.get(name) or {}).get("config") or {}
            if name == "publish-on-web" and isinstance(config.get("domains"), dict):
                return config["domains"]
    return None


def _blank_env_values(content: str) -> str:
    """Replace every value in a user-env-vars block, keeping the keys.

    Both the ``KEY=VALUE`` and the YAML ``KEY: value`` form occur in real project files,
    and OPI already detects and parses both (``validate_and_parse_env_vars``). Reusing
    that instead of splitting lines here means this cannot drift from what OPI accepts,
    and the format detection stays in one place.

    Rewriting hand-rolled parsing here got it wrong twice: the first version read the
    block as YAML and produced one placeholder per character, the second understood only
    ``KEY=`` and passed every ``KEY: value`` line through untouched, which left a
    production ``SECRET_KEY_BASE`` in the converted output. ``validate_and_parse_env_vars``
    raises on a line it cannot parse, which is what we want: silence here would mean a
    real value ships verbatim. An empty result means the block held only comments, so
    there is nothing to blank.
    """
    parsed = validate_and_parse_env_vars(content)
    if not parsed:
        return content

    separator = ": " if _detect_env_var_format(content) == "yaml" else "="
    return "\n".join(f"{key}{separator}{SANDBOX_ENV_PLACEHOLDER}" for key in parsed) + "\n"


def replace_user_env_var_values(
    project: dict, project_private_key: str | None, project_public_key: str | None, name: str
) -> None:
    """Overwrite the *values* in every ``user-env-vars`` block, keeping the keys.

    These blocks are AGE-encrypted with the project's own public key, and step 2 above
    re-encrypts that project key with the sandbox key. Copying them across verbatim would
    therefore make every production value a team put there (API tokens, third-party
    credentials) readable by anyone holding the sandbox key. That is a real downgrade of
    protection, not a theoretical one: over the six sample projects it covers fifteen
    components.

    Dropping the block instead would be safe but would stop the upgrade test from proving
    anything about it, and whether user-env-vars survive a migration and still reach the
    manifest is exactly one of the things that must be proven. So the structure, the key
    names and the whole processing path stay intact and only the value changes.
    """
    if not project_public_key or not project_private_key:
        return

    entities = list(project.get("components", []) or [])
    for dep in project.get("deployments", []) or []:
        if isinstance(dep, dict):
            entities.extend(dep.get("components", []) or [])

    replaced = 0
    for entity in entities:
        if not isinstance(entity, dict) or not entity.get("user-env-vars"):
            continue
        raw = str(entity["user-env-vars"]).strip()
        decrypted = decrypt_age_content_sync(raw, project_private_key)
        if decrypted is None:
            # Cannot read it, so cannot preserve the key names. Removing beats shipping an
            # unreadable production blob to the sandbox.
            del entity["user-env-vars"]
            logger.warning(f"  [{name}] component '{entity.get('name', '?')}': user-env-vars unreadable, dropped")
            continue
        entity["user-env-vars"] = LiteralScalarString(
            encrypt_age_content_sync(_blank_env_values(decrypted), project_public_key)
        )
        replaced += 1

    if replaced:
        logger.info(f"  [{name}] Replaced the values of {replaced} user-env-vars block(s) with a placeholder")


def replace_alias_values(project: dict, name: str) -> None:
    """Overwrite every component alias value, keeping the alias names.

    Same reasoning as ``replace_user_env_var_values``: an alias template can hold a
    secret. Encrypting alias values at rest is new (see ComponentAliasesEncryptGenerator),
    so existing project files still carry them as plaintext, which means a sandbox copy
    would publish them verbatim.

    Kept plaintext here on purpose: OPI encrypts alias values lazily on the next save,
    so leaving them plain exercises exactly that upgrade path in the test.
    """
    entities = list(project.get("components", []) or [])
    for dep in project.get("deployments", []) or []:
        if isinstance(dep, dict):
            entities.extend(dep.get("components", []) or [])

    replaced = 0
    for entity in entities:
        aliases = entity.get("aliases") if isinstance(entity, dict) else None
        if not isinstance(aliases, dict) or not aliases:
            continue
        for key in aliases:
            aliases[key] = SANDBOX_ENV_PLACEHOLDER
        replaced += 1

    if replaced:
        logger.info(f"  [{name}] Replaced the values of {replaced} aliases block(s) with a placeholder")


def resolve_input_path(input_path: str, source_dir: str) -> str:
    """Resolve input to a full file path, checking source_dir if needed."""
    if os.path.isfile(input_path):
        return input_path

    # Try as a name (with or without .yaml)
    name = input_path
    if not name.endswith(".yaml"):
        name = f"{name}.yaml"

    full_path = os.path.join(source_dir, name)
    if os.path.isfile(full_path):
        return full_path

    raise FileNotFoundError(f"Project file not found: tried '{input_path}' and '{full_path}'")


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent.parent

    parser = argparse.ArgumentParser(description="Migrate production project files to sandbox cluster")
    parser.add_argument("projects", nargs="+", help="Project file paths or names (looked up in --source-dir)")
    parser.add_argument("--prod-key", default=str(repo_root / "security" / "key.txt"), help="Production AGE key file")
    parser.add_argument(
        "--sandbox-key", default=str(repo_root / "security" / "sandbox-key.txt"), help="Sandbox AGE key file"
    )
    parser.add_argument(
        "--keep-domains",
        action="store_true",
        help=(
            "Leave base-domain as production has it. For the upgrade-safety test, where the "
            "hostname is what gets compared; nothing needs it to resolve (the probe is read "
            "over a port-forward)."
        ),
    )
    parser.add_argument(
        "--sandbox-public-key",
        help=(
            "Target AGE recipient (age1...), instead of reading it from --sandbox-key. "
            "Encrypting needs only the public half, so a cluster whose private key must stay "
            "on that cluster can still be targeted from here."
        ),
    )
    parser.add_argument("--output-dir", default="/tmp/sandbox-projects", help="Output directory for transformed files")
    parser.add_argument(
        "--source-dir", default=DEFAULT_SOURCE_DIR, help="Source directory for looking up project names"
    )
    parser.add_argument(
        "--probe-image",
        nargs="?",
        const=DEFAULT_PROBE_IMAGE,
        default=None,
        help=(
            "Replace every component workload with the e2e-allservices probe image and move each "
            f"component's inbound port to the probe port. Without a value uses {DEFAULT_PROBE_IMAGE}. "
            "Omit the flag entirely to keep the original images and ports."
        ),
    )
    parser.add_argument(
        "--probe-port",
        type=int,
        default=DEFAULT_PROBE_PORT,
        help=f"Inbound port the probe listens on (default {DEFAULT_PROBE_PORT}); only used with --probe-image",
    )

    args = parser.parse_args()

    # Read AGE keys
    logger.info(f"Production key: {args.prod_key}")
    _, prod_private_key = read_age_key_file(args.prod_key)
    if args.sandbox_public_key:
        # Only the recipient is needed to encrypt. Every target cluster mints its own key
        # (the server sandbox did on 2026-08-02), so requiring the key FILE would mean
        # copying a private key off that cluster for no reason.
        sandbox_public_key = args.sandbox_public_key
        logger.info(f"Sandbox recipient (public key given directly): {sandbox_public_key}")
    else:
        logger.info(f"Sandbox key: {args.sandbox_key}")
        sandbox_public_key, _ = read_age_key_file(args.sandbox_key)
    logger.info(f"Sandbox public key: {sandbox_public_key}")

    # Ensure output directory exists
    os.makedirs(args.output_dir, exist_ok=True)

    success_count = 0
    for project_input in args.projects:
        try:
            input_path = resolve_input_path(project_input, args.source_dir)
            filename = os.path.basename(input_path)
            logger.info(f"\nMigrating: {input_path}")

            data = load_yaml_from_path(input_path)
            if data is None:
                logger.error(f"  Failed to load YAML from {input_path}")
                continue

            transformed = migrate_project(
                data,
                prod_private_key,
                sandbox_public_key,
                probe_image=args.probe_image,
                probe_port=args.probe_port,
                keep_domains=args.keep_domains,
            )

            output_path = os.path.join(args.output_dir, filename)
            if save_yaml_to_path(output_path, transformed):
                logger.info(f"  Written to: {output_path}")
                success_count += 1
            else:
                logger.error(f"  Failed to write: {output_path}")

        except FileNotFoundError as e:
            logger.error(str(e))
        except (RuntimeError, ValueError) as e:
            logger.error(f"  Migration failed for {project_input}: {e}")

    logger.info(f"\nDone: {success_count}/{len(args.projects)} projects migrated")
    logger.info(f"Output directory: {args.output_dir}")

    if success_count < len(args.projects):
        sys.exit(1)


if __name__ == "__main__":
    main()
