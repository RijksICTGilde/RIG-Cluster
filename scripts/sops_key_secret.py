"""Put the new key in the cluster's ``sops-age-key`` secrets and restart what has to reread it.

This is the ONE irreversible step of the cutover, so it asks for confirmation with the cluster
name typed out in full.

    scripts/set-sops-key-secret.py --dry-run     # says which namespaces, and which it leaves alone
    scripts/set-sops-key-secret.py               # asks for the cluster name, then does it

**``sops-age-key`` is NOT one secret in one namespace, and this is the trap.** The plan names
``rig-prd-operations``. Measured on the sandbox cluster: **12 namespaces** hold a secret by that
name. Two of them (``rig-system`` and ``rig-ron``) carry the platform key -- ``sops-plugin.sh``
reads the secret in ``${ARGOCD_APP_NAMESPACE}``, the namespace of the application being rendered,
so the RON renderer needs its own copy -- and the other ten each carry a DIFFERENT one, because
OPI writes a per-project SOPS key into every project namespace
(``store_project_sops_key_in_namespace``). Overwriting every ``sops-age-key`` would therefore
replace ten projects' own keys with the platform key and make their secrets unreadable.

So the selection is by KEY and not by name, exactly like the file side selects by recipient: this
tool reads each secret, derives its public half, and only replaces the ones that currently hold
the OLD platform key. Everything else is listed as left alone, with its own public key next to
it, so the operator can see what was skipped and why.

**The secret holds the whole key file, not just the key line.** ``--from-file=key=<path>``, and
``sops-plugin.sh`` greps ``^AGE-SECRET-KEY-`` out of it. This tool keeps that shape.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from key_rotation import (  # type: ignore[reportMissingImports]
    AGE_KEY_MARKER,
    MissingKey,
    ask_for_path,
    public_key_of,
    read_key,
)
from opi.connectors.kubectl import create_kubectl_connector  # type: ignore[reportMissingImports]

REPO = Path(__file__).resolve().parents[1]
CANONICAL_NEW = REPO / "security" / "key.txt"
CANONICAL_OLD = REPO / "security" / "old_key.txt"
# S105 fires on both: they are a Kubernetes resource name and a data key, not a password.
SECRET_NAME = "sops-age-key"  # noqa: S105
SECRET_FIELD = "key"  # noqa: S105
DEPLOYMENT = "operations-manager"


@dataclass(frozen=True)
class SecretHolder:
    """One namespace with a ``sops-age-key`` secret, and the public half of what is in it."""

    namespace: str
    public_key: str | None


def public_key_from_secret_data(encoded: str) -> str | None:
    """The public half of the key inside a secret's base64 ``key`` field, or None."""
    try:
        content = base64.b64decode(encoded).decode()
    except ValueError, UnicodeDecodeError:
        return None
    for line in content.splitlines():
        if line.strip().startswith(AGE_KEY_MARKER):
            try:
                return public_key_of(line.strip())
            except MissingKey:
                return None
    return None


def holders_from_secret_list(payload: dict) -> list[SecretHolder]:
    """Turn a ``kubectl get secret -o json`` listing into one holder per namespace."""
    items = payload["items"] if payload.get("kind", "").endswith("List") else [payload]
    holders = [
        SecretHolder(
            namespace=item["metadata"]["namespace"],
            public_key=public_key_from_secret_data((item.get("data") or {}).get(SECRET_FIELD, "")),
        )
        for item in items
    ]
    return sorted(holders, key=lambda holder: holder.namespace)


async def find_holders() -> list[SecretHolder]:
    """Every namespace carrying ``sops-age-key``, with the key each one holds.

    Read rather than configured: the set differs per cluster and grows with every project, so a
    hard-coded list silently falls behind -- and, worse, would not tell the platform key apart
    from a project's own.
    """
    kubectl = create_kubectl_connector()
    # A field selector and not "get secret <name> --all-namespaces": kubectl refuses the latter
    # with "a resource cannot be retrieved by name across all namespaces" (measured).
    stdout, stderr, code = await kubectl.run_command(
        ["get", "secret", "--all-namespaces", f"--field-selector=metadata.name={SECRET_NAME}", "-o", "json"]
    )
    if code != 0:
        raise RuntimeError(f"kubectl get secret failed: {stderr.strip()}")
    return holders_from_secret_list(json.loads(stdout))


async def write_secret(namespace: str, key_file: Path) -> None:
    """Replace the secret in one namespace, keeping the whole key file as its value."""
    kubectl = create_kubectl_connector()
    stdout, stderr, code = await kubectl.run_command(
        [
            "create",
            "secret",
            "generic",
            SECRET_NAME,
            f"--from-file={SECRET_FIELD}={key_file}",
            "-n",
            namespace,
            "--dry-run=client",
            "-o",
            "yaml",
        ]
    )
    if code != 0:
        raise RuntimeError(f"kubectl create secret --dry-run failed in {namespace}: {stderr.strip()}")
    _out, stderr, code = await kubectl.run_command(["apply", "-n", namespace, "-f", "-"], stdin_input=stdout)
    if code != 0:
        raise RuntimeError(f"kubectl apply failed in {namespace}: {stderr.strip()}")


async def restart_operations_manager(namespace: str) -> bool:
    """Restart the operations-manager so it rereads its env var. Returns False when absent.

    The key arrives as an env var from ``secretKeyRef``, which is resolved once when the pod
    starts, so without this it keeps using the old key until something else restarts it.
    """
    kubectl = create_kubectl_connector()
    _out, _err, code = await kubectl.run_command(["get", "deployment", DEPLOYMENT, "-n", namespace])
    if code != 0:
        return False
    _out, stderr, code = await kubectl.run_command(["rollout", "restart", f"deployment/{DEPLOYMENT}", "-n", namespace])
    if code != 0:
        raise RuntimeError(f"rollout restart failed in {namespace}: {stderr.strip()}")
    _out, stderr, code = await kubectl.run_command(
        ["rollout", "status", f"deployment/{DEPLOYMENT}", "-n", namespace, "--timeout=180s"]
    )
    if code != 0:
        raise RuntimeError(f"rollout did not become ready in {namespace}: {stderr.strip()}")
    return True


async def current_cluster() -> str:
    """The current kubectl context, which is what the confirmation asks you to type."""
    kubectl = create_kubectl_connector()
    stdout, stderr, code = await kubectl.run_command(["config", "current-context"])
    if code != 0:
        raise RuntimeError(f"kubectl config current-context failed: {stderr.strip()}")
    return stdout.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="say which namespaces would change and stop")
    parser.add_argument("--old-key", default=str(CANONICAL_OLD))
    parser.add_argument("--new-key", default=str(CANONICAL_NEW))
    parser.add_argument(
        "--confirm-cluster",
        help="the cluster name, for a non-interactive run; it must equal the current kubectl context",
    )
    return parser


def report_holders(holders: list[SecretHolder], old_public: str, new_public: str) -> list[SecretHolder]:
    """Print every holder, and return the ones that carry the old platform key."""
    targets = [holder for holder in holders if holder.public_key == old_public]
    done = [holder for holder in holders if holder.public_key == new_public]
    others = [holder for holder in holders if holder not in targets and holder not in done]

    print(f"\n{len(holders)} namespaces hold a '{SECRET_NAME}' secret.")
    print(f"\n{len(targets)} carry the OLD platform key and WILL be replaced:")
    for holder in targets:
        print(f"  {holder.namespace}")
    if done:
        print(f"\n{len(done)} already carry the new platform key (nothing to do):")
        for holder in done:
            print(f"  {holder.namespace}")
    if others:
        print(f"\n{len(others)} carry a DIFFERENT key and are left alone (a project's own key):")
        for holder in others:
            print(f"  {holder.namespace}  {holder.public_key or 'unreadable'}")
    return targets


async def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    try:
        reader = (lambda _question: "") if arguments.confirm_cluster else input
        old_private = read_key(ask_for_path("old key", arguments.old_key, reader=reader))
        key_file = ask_for_path("new key", arguments.new_key, reader=reader)
        new_private = read_key(key_file)
    except MissingKey as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2
    old_public = public_key_of(old_private)
    new_public = public_key_of(new_private)
    if old_public == new_public:
        print("FAIL the old and the new key are the same key", file=sys.stderr)
        return 2

    try:
        cluster = await current_cluster()
        holders = await find_holders()
    except RuntimeError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2

    print(f"\nCluster (kubectl context): {cluster}")
    print(f"Old platform key (public): {old_public}")
    print(f"New platform key (public): {new_public}")

    targets = report_holders(holders, old_public, new_public)
    if not targets:
        print("\nNothing to do: no namespace carries the old platform key.")
        return 0

    print(f"\nAfter the swap, deployment/{DEPLOYMENT} is restarted where it exists.")
    print("The sops-plugin next to ArgoCD needs no restart: it reads the secret on every render.")

    if arguments.dry_run:
        print("\nDry run: nothing was changed.")
        return 0

    print("\nThis is the one irreversible step of the cutover.")
    typed = arguments.confirm_cluster or input(f"Type the cluster name ({cluster}): ")
    if typed.strip() != cluster:
        print("FAIL cluster name does not match; nothing changed.", file=sys.stderr)
        return 1

    for holder in targets:
        await write_secret(holder.namespace, key_file)
        print(f"  secret replaced in {holder.namespace}")
    for holder in targets:
        if await restart_operations_manager(holder.namespace):
            print(f"  deployment/{DEPLOYMENT} restarted and ready in {holder.namespace}")

    print("\nDone. Smoke test now:")
    print("  - OPI reads a sops file (check the logs for a decryption error)")
    print("  - ArgoCD renders an application without an error")
    print("  - a project can reach its repository")
    print("Then: scripts/rotate-sops-key.py --assert-old-key-dead --projects <clone>/projects")
    return 0
