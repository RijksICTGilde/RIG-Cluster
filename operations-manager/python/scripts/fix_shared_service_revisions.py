#!/usr/bin/env python3
"""
Fix shared service revisions caused by YAML anchor/alias bug.

When deployments were cloned, ruamel.yaml created shared references (&id001/*id001)
for the services list. This caused all cloned deployments to share the same revisions
list, mixing revision history across deployments.

This script fixes a project file by:
1. Matching each revision to its owning deployment via the resource name
2. Keeping only revisions that belong to each deployment
3. Clearing services config for deployments with no own revisions
4. Writing the corrected file (without YAML anchors)

Usage:
    cd operations-manager/python
    uv run python scripts/fix_shared_service_revisions.py <project-file> [--dry-run]

Examples:
    uv run python scripts/fix_shared_service_revisions.py /path/to/regel-k4c.yaml --dry-run
    uv run python scripts/fix_shared_service_revisions.py /path/to/wies.yaml
"""

import argparse
import logging
import sys
from pathlib import Path

from ruamel.yaml import YAML

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def sanitize_for_identifier(value: str) -> str:
    """Match opi.utils.naming._sanitize_for_identifier."""
    return value.replace("-", "_").lower()


def expected_resource_prefix(project_name: str, deployment_name: str) -> str:
    """Build the resource name prefix for a deployment (without generation suffix)."""
    return f"{sanitize_for_identifier(project_name)}_{sanitize_for_identifier(deployment_name)}"


def revision_belongs_to_deployment(revision: dict, prefix: str) -> bool:
    """Check if a revision's resource matches a deployment's expected prefix.

    The resource name is {project}_{deployment} or {project}_{deployment}_v{N}.
    We match on the prefix and ensure the next character (if any) is '_v' for
    versioned resources, not another deployment name segment.
    """
    resource = revision.get("resource", "")
    if resource == prefix:
        return True
    return bool(resource.startswith(prefix + "_v"))


def fix_project_file(project_data: dict, dry_run: bool = False) -> dict:
    """Fix shared service revisions in project data.

    Returns:
        dict with changes summary: {deployment_name: {before, after, removed}}
    """
    project_name = project_data.get("name", "")
    deployments = project_data.get("deployments", [])
    changes: dict[str, dict] = {}

    # Build lookup of all known deployment prefixes
    deployment_prefixes = {}
    for dep in deployments:
        dep_name = dep.get("name", "")
        deployment_prefixes[dep_name] = expected_resource_prefix(project_name, dep_name)

    for dep in deployments:
        dep_name = dep.get("name", "")
        services = dep.get("services", [])
        if not services:
            continue

        prefix = deployment_prefixes[dep_name]

        for svc in services:
            if not isinstance(svc, dict):
                continue
            config = svc.get("config")
            if not isinstance(config, dict):
                continue
            revisions = config.get("revisions")
            if not isinstance(revisions, list) or not revisions:
                continue

            original_count = len(revisions)
            own_revisions = [r for r in revisions if revision_belongs_to_deployment(r, prefix)]
            removed_count = original_count - len(own_revisions)

            if removed_count > 0:
                changes[dep_name] = {
                    "service": svc.get("reference", "?"),
                    "before": original_count,
                    "after": len(own_revisions),
                    "removed": removed_count,
                }

                if not dry_run:
                    if own_revisions:
                        # Keep own revisions, update generation from the active one
                        config["revisions"] = own_revisions
                        active = [r for r in own_revisions if r.get("status") == "active"]
                        if active:
                            config["generation"] = active[0].get("generation", 0)
                    else:
                        # No own revisions: remove config entirely, keep service reference
                        del svc["config"]

    return changes


def load_project(file_path: str) -> dict:
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(file_path) as f:
        return yaml.load(f)


def save_project(file_path: str, project_data: dict) -> None:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.representer.ignore_aliases = lambda *_: True

    with open(file_path, "w") as f:
        yaml.dump(project_data, f)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fix shared service revisions in project files")
    parser.add_argument("file", help="Path to the project YAML file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying the file")
    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        logger.error(f"File not found: {file_path}")
        sys.exit(1)

    project_data = load_project(str(file_path))
    project_name = project_data.get("name", file_path.stem)

    logger.info(f"{'[DRY RUN] ' if args.dry_run else ''}Processing project: {project_name} ({file_path})")

    changes = fix_project_file(project_data, dry_run=args.dry_run)

    if not changes:
        logger.info("No shared revisions found - file is clean")
        return

    for dep_name, info in changes.items():
        logger.info(
            f"  {dep_name}: {info['service']} - "
            f"{info['before']} revisions -> {info['after']} own "
            f"({info['removed']} foreign removed)"
        )

    if args.dry_run:
        logger.info("Dry run complete - no changes written")
    else:
        save_project(str(file_path), project_data)
        logger.info(f"Saved fixed file: {file_path}")


if __name__ == "__main__":
    main()
