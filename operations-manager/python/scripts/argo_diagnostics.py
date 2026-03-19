#!/usr/bin/env python3
"""
ArgoCD Diagnostics Script

Uses the OPI ArgoConnector to query and diagnose ArgoCD application status.
Designed for troubleshooting stuck, degraded, or out-of-sync applications.

Usage:
    uv run python scripts/argo_diagnostics.py --env scripts/.env.odcn-production status
    uv run python scripts/argo_diagnostics.py --env scripts/.env.odcn-production app <name>
    uv run python scripts/argo_diagnostics.py --env scripts/.env.odcn-production refresh --all-stuck
    uv run python scripts/argo_diagnostics.py --env scripts/.env.odcn-production sync <name>

Env files live in scripts/.env.<environment> and are gitignored.
"""

import argparse
import asyncio
import json

# Suppress all logging and warnings before importing opi (config module logs at import time)
import logging
import os
import sys
import warnings
from pathlib import Path
from typing import Any

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")

# Add the parent directory to the path so we can import from opi
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opi.connectors.argo import ArgoConnector  # noqa: E402


def load_env_file(env_path: str) -> None:
    """Load a .env file into os.environ. Existing vars are NOT overwritten."""
    path = Path(env_path)
    if not path.exists():
        print(f"Error: env file not found: {env_path}")
        sys.exit(1)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def create_connector(args: argparse.Namespace) -> ArgoConnector:
    """Create an ArgoConnector from CLI args / env vars."""
    host = args.host or os.environ.get("ARGOCD_HOST", "")
    port = int(args.port or os.environ.get("ARGOCD_PORT", "443"))
    username = args.username or os.environ.get("ARGOCD_USERNAME", "admin")
    password = args.password or os.environ.get("ARGOCD_PASSWORD", "")

    if not host:
        print("Error: ArgoCD host is required (--host or ARGOCD_HOST)")
        sys.exit(1)
    if not password:
        print("Error: ArgoCD password is required (--password or ARGOCD_PASSWORD)")
        sys.exit(1)

    use_tls = port == 443 or args.tls
    return ArgoConnector(
        server_host=host,
        server_port=port,
        username=username,
        password=password,
        use_tls=use_tls,
        verify_ssl=args.verify_ssl,
    )


def extract_app_summary(app: dict[str, Any]) -> dict[str, Any]:
    """Extract key status fields from an ArgoCD application object."""
    metadata = app.get("metadata", {})
    status = app.get("status", {})
    spec = app.get("spec", {})
    sync_status = status.get("sync", {})
    health_status = status.get("health", {})
    operation_state = status.get("operationState", {})

    return {
        "name": metadata.get("name", "unknown"),
        "namespace": metadata.get("namespace", ""),
        "project": spec.get("project", ""),
        "sync_status": sync_status.get("status", "Unknown"),
        "health_status": health_status.get("status", "Unknown"),
        "health_message": health_status.get("message", ""),
        "revision": sync_status.get("revision", "")[:8],
        "last_sync_phase": operation_state.get("phase", ""),
        "last_sync_message": operation_state.get("message", ""),
        "source_repo": spec.get("source", {}).get("repoURL", ""),
        "source_path": spec.get("source", {}).get("path", ""),
        "destination": spec.get("destination", {}).get("namespace", ""),
    }


def is_stuck(summary: dict[str, Any]) -> bool:
    """Determine if an application is stuck (not synced+healthy)."""
    return not (summary["sync_status"] == "Synced" and summary["health_status"] == "Healthy")


def print_status_table(apps: list[dict[str, Any]]) -> None:
    """Print a formatted status table of all applications."""
    if not apps:
        print("No applications found.")
        return

    summaries = [extract_app_summary(app) for app in apps]
    summaries.sort(key=lambda s: (s["health_status"] != "Healthy", s["sync_status"] != "Synced", s["name"]))

    # Column widths
    name_w = max(len(s["name"]) for s in summaries)
    sync_w = max(len(s["sync_status"]) for s in summaries)
    health_w = max(len(s["health_status"]) for s in summaries)

    header = f"{'NAME':<{name_w}}  {'SYNC':<{sync_w}}  {'HEALTH':<{health_w}}  PROJECT       MESSAGE"
    print(header)
    print("-" * len(header))

    stuck_count = 0
    for s in summaries:
        stuck = is_stuck(s)
        if stuck:
            stuck_count += 1
        marker = ">> " if stuck else "   "
        message = s["health_message"] or s["last_sync_message"] or ""
        if len(message) > 60:
            message = message[:57] + "..."
        print(
            f"{marker}{s['name']:<{name_w}}  {s['sync_status']:<{sync_w}}  {s['health_status']:<{health_w}}  {s['project']:<12}  {message}"
        )

    print()
    print(f"Total: {len(summaries)} apps, {stuck_count} stuck/unhealthy (marked with >>)")


def print_app_detail(app_data: dict[str, Any], resource_tree: list[dict[str, Any]]) -> None:
    """Print detailed information about a single application."""
    summary = extract_app_summary(app_data)
    status = app_data.get("status", {})
    conditions = status.get("conditions", [])

    print(f"Application: {summary['name']}")
    print(f"  Project:     {summary['project']}")
    print(f"  Namespace:   {summary['destination']}")
    print(f"  Sync:        {summary['sync_status']}")
    print(f"  Health:      {summary['health_status']}")
    print(f"  Revision:    {summary['revision']}")
    print(f"  Source:      {summary['source_repo']}")
    print(f"  Path:        {summary['source_path']}")
    print(f"  Last Op:     {summary['last_sync_phase']}")

    if summary["health_message"]:
        print(f"  Message:     {summary['health_message']}")
    if summary["last_sync_message"]:
        print(f"  Sync Msg:    {summary['last_sync_message']}")

    # Conditions (often contain the actual error)
    if conditions:
        print("\n  Conditions:")
        for cond in conditions:
            print(f"    - [{cond.get('type', '?')}] {cond.get('message', '')}")

    # Resource sync status from status.resources
    resources = status.get("resources", [])
    if resources:
        out_of_sync = [r for r in resources if r.get("status") != "Synced"]
        unhealthy = [r for r in resources if r.get("health", {}).get("status") not in ("Healthy", None)]

        if out_of_sync:
            print(f"\n  Out-of-Sync Resources ({len(out_of_sync)}):")
            for r in out_of_sync:
                print(f"    - {r.get('kind', '?')}/{r.get('name', '?')} [{r.get('status', '?')}]")

        if unhealthy:
            print(f"\n  Unhealthy Resources ({len(unhealthy)}):")
            for r in unhealthy:
                health = r.get("health", {})
                print(
                    f"    - {r.get('kind', '?')}/{r.get('name', '?')} [{health.get('status', '?')}] {health.get('message', '')}"
                )

    # Resource tree (deeper details like pod errors)
    if resource_tree:
        unhealthy_nodes = [n for n in resource_tree if n.get("health", {}).get("status") not in ("Healthy", None, "")]
        if unhealthy_nodes:
            print(f"\n  Unhealthy Resource Tree Nodes ({len(unhealthy_nodes)}):")
            for node in unhealthy_nodes:
                health = node.get("health", {})
                kind = node.get("kind", "?")
                name = node.get("name", "?")
                ns = node.get("namespace", "")
                print(f"    - {kind}/{name} (ns: {ns}) [{health.get('status', '?')}]")
                if health.get("message"):
                    print(f"      {health['message']}")


async def cmd_status(connector: ArgoConnector, args: argparse.Namespace) -> None:
    """List all applications with status summary."""
    _ = args
    apps = await connector.list_applications()
    print_status_table(apps)


async def cmd_app(connector: ArgoConnector, args: argparse.Namespace) -> None:
    """Show detailed status for a single application."""
    app_data = await connector.get_application_status(args.name)
    if not app_data:
        print(f"Application '{args.name}' not found.")
        sys.exit(1)

    resource_tree = await connector.get_application_resource_tree(args.name)
    print_app_detail(app_data, resource_tree)

    if args.json:
        print("\n--- Raw JSON ---")
        print(json.dumps(app_data, indent=2))


async def cmd_refresh(connector: ArgoConnector, args: argparse.Namespace) -> None:
    """Hard-refresh one or all stuck applications."""
    if args.all_stuck:
        apps = await connector.list_applications()
        stuck_apps = [extract_app_summary(a) for a in apps if is_stuck(extract_app_summary(a))]
        if not stuck_apps:
            print("No stuck applications found.")
            return
        print(f"Hard-refreshing {len(stuck_apps)} stuck applications...")
        for s in stuck_apps:
            ok = await connector.hard_refresh_application(s["name"])
            status = "ok" if ok else "FAILED"
            print(f"  {s['name']}: {status}")
    else:
        if not args.name:
            print("Error: provide an app name or --all-stuck")
            sys.exit(1)
        ok = await connector.hard_refresh_application(args.name)
        print(f"Hard-refresh {'succeeded' if ok else 'FAILED'} for {args.name}")


async def cmd_sync(connector: ArgoConnector, args: argparse.Namespace) -> None:
    """Trigger sync for an application."""
    ok = await connector.sync_application(args.name)
    print(f"Sync {'triggered' if ok else 'FAILED'} for {args.name}")


def is_pending_deletion(app: dict[str, Any]) -> bool:
    """Check if an application has a deletionTimestamp (stuck in pending deletion)."""
    return "deletionTimestamp" in app.get("metadata", {})


async def delete_app_resource(connector: ArgoConnector, app_name: str, resource: dict[str, Any]) -> bool:
    """Delete a single managed resource via the ArgoCD API."""
    namespace = resource.get("namespace", "")
    name = resource.get("name", "")
    kind = resource.get("kind", "")
    group = resource.get("group", "")
    version = resource.get("version", "v1")

    params = f"namespace={namespace}&resourceName={name}&kind={kind}&version={version}"
    if group:
        params += f"&group={group}"

    url = f"{connector._actual_base_url}/api/v1/applications/{app_name}/resource?{params}"
    status_code, response_text = await connector._make_authenticated_request("DELETE", url)

    if status_code in (200, 204):
        return True
    elif status_code == 404:
        # Already gone
        return True
    else:
        print(f"    FAILED ({status_code}): {response_text[:100]}")
        return False


async def remove_app_finalizers(connector: ArgoConnector, app_name: str) -> bool:
    """Remove all finalizers from an ArgoCD Application via the API.

    ArgoCD validates the full app spec on PATCH, so if the source path is gone
    (the usual reason apps get stuck), we first fix the source path to point
    to a valid location, then remove the finalizers.
    """
    app_data = await connector.get_application_status(app_name)
    if not app_data:
        return True  # Already gone

    url = f"{connector._actual_base_url}/api/v1/applications/{app_name}"

    # Check if the source path is the problem (ComparisonError with "app path does not exist")
    conditions = app_data.get("status", {}).get("conditions", [])
    path_missing = any("app path does not exist" in c.get("message", "") for c in conditions)

    if path_missing:
        # Step 1: Fix the source path to "." (repo root always exists) and disable auto-sync
        print("  Source path missing — fixing spec first...")
        fix_patch = {
            "spec": {
                "source": {"path": "."},
                "syncPolicy": None,
            },
            "metadata": {"finalizers": []},
        }
        patch_body = {
            "name": app_name,
            "patch": json.dumps(fix_patch),
            "patchType": "merge",
        }
        status_code, response_text = await connector._make_authenticated_request("PATCH", url, json_data=patch_body)
        if status_code == 200:
            return True
        else:
            print(f"  Failed to fix spec + remove finalizers ({status_code}): {response_text[:200]}")
            return False
    else:
        # Source path is valid, just remove finalizers
        patch_body = {
            "name": app_name,
            "patch": json.dumps({"metadata": {"finalizers": []}}),
            "patchType": "merge",
        }
        status_code, response_text = await connector._make_authenticated_request("PATCH", url, json_data=patch_body)
        if status_code == 200:
            return True
        else:
            print(f"  Failed to remove finalizers ({status_code}): {response_text[:200]}")
            return False


async def delete_application(connector: ArgoConnector, app_name: str) -> bool:
    """Delete an ArgoCD Application (cascade=true)."""
    url = f"{connector._actual_base_url}/api/v1/applications/{app_name}?cascade=true"
    status_code, response_text = await connector._make_authenticated_request("DELETE", url)
    if status_code in (200, 204) or status_code == 404:
        return True
    else:
        print(f"  Failed to delete application ({status_code}): {response_text[:100]}")
        return False


async def force_delete_app(connector: ArgoConnector, app_name: str) -> bool:
    """Force-delete a stuck app: delete its resources, remove finalizers, delete the app."""
    app_data = await connector.get_application_status(app_name)
    if not app_data:
        print(f"  Application '{app_name}' not found (already deleted?).")
        return True

    resources = app_data.get("status", {}).get("resources", [])
    pending = is_pending_deletion(app_data)
    finalizers = app_data.get("metadata", {}).get("finalizers", [])

    print(f"  Pending deletion: {pending}")
    print(f"  Finalizers: {finalizers}")
    print(f"  Managed resources: {len(resources)}")

    # Step 1: Delete managed resources
    if resources:
        print(f"  Deleting {len(resources)} managed resources...")
        for r in resources:
            kind = r.get("kind", "?")
            name = r.get("name", "?")
            ns = r.get("namespace", "?")
            ok = await delete_app_resource(connector, app_name, r)
            status = "deleted" if ok else "FAILED"
            print(f"    {kind}/{name} (ns: {ns}): {status}")

    # Step 2: Remove finalizers
    if finalizers:
        print("  Removing finalizers...")
        ok = await remove_app_finalizers(connector, app_name)
        if ok:
            print("  Finalizers removed.")
        else:
            print("  FAILED to remove finalizers.")
            return False

    # Step 3: Delete the application itself (may already be gone if deletionTimestamp was set)
    print("  Deleting application...")
    ok = await delete_application(connector, app_name)
    if ok:
        print(f"  Application '{app_name}' deleted.")
    else:
        # 403 after finalizer removal usually means the app was already garbage-collected
        print(f"  Application '{app_name}' likely already garbage-collected.")
    return True


async def cmd_force_delete(connector: ArgoConnector, args: argparse.Namespace) -> None:
    """Force-delete stuck application(s): clean up resources, remove finalizers, delete."""
    if args.all_pending:
        apps = await connector.list_applications()
        pending_apps = [a for a in apps if is_pending_deletion(a)]
        if not pending_apps:
            print("No applications pending deletion found.")
            return
        print(f"Found {len(pending_apps)} applications stuck in pending deletion:\n")
        for app in pending_apps:
            name = app["metadata"]["name"]
            print(f"  - {name}")
        print()

        if not args.yes:
            answer = input("Proceed with force-delete? [y/N] ")
            if answer.lower() != "y":
                print("Aborted.")
                return

        for app in pending_apps:
            name = app["metadata"]["name"]
            print(f"\nForce-deleting: {name}")
            await force_delete_app(connector, name)
    else:
        if not args.name:
            print("Error: provide an app name or --all-pending")
            sys.exit(1)
        print(f"Force-deleting: {args.name}")
        await force_delete_app(connector, args.name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ArgoCD Diagnostics (using OPI ArgoConnector)")

    # Env file
    parser.add_argument("--env", help="Path to .env file (e.g. scripts/.env.odcn-production)")

    # Connection overrides (env file values are used as fallback)
    parser.add_argument("--host", help="ArgoCD server host (overrides ARGOCD_HOST)")
    parser.add_argument("--port", type=int, help="ArgoCD server port (overrides ARGOCD_PORT)")
    parser.add_argument("--username", help="ArgoCD username (overrides ARGOCD_USERNAME)")
    parser.add_argument("--password", help="ArgoCD password (overrides ARGOCD_PASSWORD)")
    parser.add_argument("--tls", action="store_true", help="Force TLS (auto-detected for port 443)")
    parser.add_argument("--verify-ssl", action="store_true", default=False, help="Verify SSL certificates")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="List all apps with sync/health status")

    # app <name>
    app_parser = subparsers.add_parser("app", help="Detailed status for one application")
    app_parser.add_argument("name", help="Application name")
    app_parser.add_argument("--json", action="store_true", help="Also dump raw JSON")

    # refresh <name|--all-stuck>
    refresh_parser = subparsers.add_parser("refresh", help="Hard-refresh application(s)")
    refresh_parser.add_argument("name", nargs="?", help="Application name")
    refresh_parser.add_argument("--all-stuck", action="store_true", help="Refresh all stuck apps")

    # sync <name>
    sync_parser = subparsers.add_parser("sync", help="Trigger sync for an application")
    sync_parser.add_argument("name", help="Application name")

    # force-delete <name|--all-pending>
    fd_parser = subparsers.add_parser(
        "force-delete",
        help="Force-delete stuck app(s): delete resources, remove finalizers, delete app",
    )
    fd_parser.add_argument("name", nargs="?", help="Application name")
    fd_parser.add_argument("--all-pending", action="store_true", help="Force-delete all apps stuck in pending deletion")
    fd_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.env:
        load_env_file(args.env)

    connector = create_connector(args)

    commands = {
        "status": cmd_status,
        "app": cmd_app,
        "refresh": cmd_refresh,
        "sync": cmd_sync,
        "force-delete": cmd_force_delete,
    }

    handler = commands[args.command]
    asyncio.run(handler(connector, args))


if __name__ == "__main__":
    main()
