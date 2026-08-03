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
    --output-dir PATH     Output directory (default: /tmp/sandbox-projects)
    --source-dir PATH     Source directory for project files
    --probe-image [IMG]   Replace every component workload with the e2e-allservices
                          probe and move each component's inbound port to the probe
                          port. Without a value uses the default probe image. Omit to
                          keep the original images and ports.
    --probe-port PORT     Probe inbound port (default 8080; only used with --probe-image)

    # Upgrade-safety test (RC-19): swap in the probe so /status verifies each binding:
    uv run python scripts/migrate_project_to_sandbox.py wies regelrecht moza amt --probe-image
"""

import argparse
import logging
import os
import secrets
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

from opi.utils.age import decrypt_age_content_sync, encrypt_age_content_sync  # noqa: E402  (after sys.path bootstrap)
from opi.utils.yaml_util import load_yaml_from_path, save_yaml_to_path  # noqa: E402
from ruamel.yaml.scalarstring import LiteralScalarString  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TARGET_CLUSTER = "sandboxed-local"
SANDBOX_DOMAIN = "sandbox.rijksapp.dev"

# The e2e-allservices probe: a static workload that binds every service it is given
# and reports the round-trip on /status. Public, so no pull secret is needed.
# It listens on 8080, while production components declare their own ports -- so when
# we swap the workload in we must also move each component's inbound port to 8080,
# otherwise the health/ingress path points at a port nothing is listening on.
DEFAULT_PROBE_IMAGE = "ghcr.io/minbzk/base-images/e2e-allservices:latest"
DEFAULT_PROBE_PORT = 8080
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
) -> dict:
    """Apply all transformations to convert a production project to sandbox.

    When ``probe_image`` is given, every component workload is additionally replaced
    with the e2e-allservices probe (see ``apply_probe_workload``); otherwise the
    original images and ports are kept so the script stays usable for a plain migration.
    """
    project = deepcopy(data)
    name = project.get("name", "unknown")

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
        re_encrypted = encrypt_age_content_sync(decrypted, sandbox_public_key)
        config["age-private-key"] = LiteralScalarString(re_encrypted)
    else:
        logger.warning(f"  [{name}] No age-private-key found in config")

    # 3. Generate new API key (encrypted with project's own public key)
    if project_public_key:
        new_api_key = secrets.token_urlsafe(32)
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

        # Change base-domain
        if "base-domain" in dep:
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

    if probe_image:
        apply_probe_workload(project, probe_image, probe_port)

    return project


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
    logger.info(f"Sandbox key: {args.sandbox_key}")
    _, prod_private_key = read_age_key_file(args.prod_key)
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
