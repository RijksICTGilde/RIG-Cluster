#!/usr/bin/env python3
"""
Add domains configuration blocks to project files.

Scans deployments for explicit domain/subdomain usage and adds the
corresponding ``domains.allowed-domains`` and ``domains.allowed-subdomains``
entries with ``status: approved`` (since these are already deployed).

Only adds entries that don't already exist. Does not modify any other
part of the YAML file.

Usage:
    cd operations-manager/python
    uv run python scripts/add_domain_approvals.py <project-file> [--dry-run]
    uv run python scripts/add_domain_approvals.py /path/to/projects/ [--dry-run]  # all files in dir

Examples:
    uv run python scripts/add_domain_approvals.py /path/to/wies.yaml --dry-run
    uv run python scripts/add_domain_approvals.py /path/to/projects/
"""

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

from ruamel.yaml import YAML

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def collect_domain_usage(project_data: dict) -> dict[str, set[str]]:
    """Scan deployments and return {domain: set(subdomains)} for non-empty domains."""
    usage: dict[str, set[str]] = defaultdict(set)
    for dep in project_data.get("deployments", []):
        if not isinstance(dep, dict):
            continue
        base_domain = dep.get("base-domain")
        subdomain = dep.get("subdomain")
        if not base_domain:
            continue
        if subdomain:
            usage[base_domain].add(subdomain)
        else:
            usage[base_domain]  # ensure domain key exists
    return dict(usage)


def add_domain_approvals(project_data: dict, dry_run: bool = False) -> dict[str, list[str]]:
    """Add missing domain/subdomain approval entries.

    Returns dict of changes: {domain: [list of what was added]}
    """
    usage = collect_domain_usage(project_data)
    if not usage:
        return {}

    changes: dict[str, list[str]] = {}

    # Ensure domains section exists
    if "domains" not in project_data and not dry_run:
        project_data["domains"] = {}
    domains = project_data.get("domains", {})

    # --- allowed-domains ---
    existing_domains = set()
    for entry in domains.get("allowed-domains", []):
        if isinstance(entry, dict):
            existing_domains.add(entry.get("domain", ""))

    for domain in sorted(usage.keys()):
        if domain not in existing_domains:
            changes.setdefault(domain, []).append("domain approved")
            if not dry_run:
                if "allowed-domains" not in domains:
                    domains["allowed-domains"] = []
                domains["allowed-domains"].append(
                    {
                        "domain": domain,
                        "status": "approved",
                    }
                )

    # --- allowed-subdomains ---
    existing_subs: dict[str, set[str]] = {}
    for entry in domains.get("allowed-subdomains", []):
        if isinstance(entry, dict):
            d = entry.get("domain", "")
            existing_subs[d] = set()
            for sub in entry.get("subdomains", []):
                if isinstance(sub, dict):
                    existing_subs[d].add(sub.get("name", ""))
                elif isinstance(sub, str):
                    existing_subs[d].add(sub)

    for domain, subdomains in sorted(usage.items()):
        subs = sorted(s for s in subdomains if s)
        if not subs:
            continue

        existing = existing_subs.get(domain, set())
        new_subs = [s for s in subs if s not in existing]
        if not new_subs:
            continue

        for s in new_subs:
            changes.setdefault(domain, []).append(f"subdomain '{s}' approved")

        if not dry_run:
            if "allowed-subdomains" not in domains:
                domains["allowed-subdomains"] = []

            # Find or create domain entry
            domain_entry = None
            for entry in domains["allowed-subdomains"]:
                if isinstance(entry, dict) and entry.get("domain") == domain:
                    domain_entry = entry
                    break
            if domain_entry is None:
                domain_entry = {"domain": domain, "subdomains": []}
                domains["allowed-subdomains"].append(domain_entry)

            for s in new_subs:
                domain_entry["subdomains"].append({"name": s, "status": "approved"})

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


def process_file(file_path: Path, dry_run: bool) -> bool:
    """Process a single file. Returns True if changes were made."""
    project_data = load_project(str(file_path))
    project_name = project_data.get("name", file_path.stem)

    changes = add_domain_approvals(project_data, dry_run=dry_run)
    if not changes:
        return False

    prefix = "[DRY RUN] " if dry_run else ""
    logger.info(f"{prefix}{project_name} ({file_path.name}):")
    for domain, items in sorted(changes.items()):
        for item in items:
            logger.info(f"  {domain}: {item}")

    if not dry_run:
        save_project(str(file_path), project_data)

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Add domain approval entries to project files")
    parser.add_argument("path", help="Path to a project YAML file or directory of project files")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    args = parser.parse_args()

    target = Path(args.path)
    if target.is_dir():
        files = sorted(target.glob("*.yaml"))
    elif target.is_file():
        files = [target]
    else:
        logger.error(f"Not found: {target}")
        sys.exit(1)

    total_changed = 0
    for f in files:
        try:
            if process_file(f, args.dry_run):
                total_changed += 1
        except Exception as e:
            logger.warning(f"Skipped {f.name}: {e}")

    action = "would change" if args.dry_run else "updated"
    logger.info(f"\n{total_changed}/{len(files)} files {action}")


if __name__ == "__main__":
    main()
