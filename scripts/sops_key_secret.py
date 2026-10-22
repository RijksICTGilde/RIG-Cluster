"""Put the new key in the cluster's ``sops-age-key`` secrets and restart what has to reread it.

This is the ONE irreversible step of the cutover, so it asks for confirmation with the cluster
name typed out in full.

    uv run --project operations-manager/python python scripts/set-sops-key-secret.py --dry-run     # says which namespaces, and which it leaves alone
    uv run --project operations-manager/python python scripts/set-sops-key-secret.py               # asks for the cluster name, then does it

**``sops-age-key`` is NOT one secret in one namespace, and this is the trap.** The plan names
``rig-prd-operations``. Measured on the sandbox cluster: **12 namespaces** hold a secret by that
name. One of them (``rig-system``, on production ``rig-prd-operations``) carries the platform key,
and every other one carries a DIFFERENT key, because OPI writes a per-project SOPS key into every
project namespace (``store_project_sops_key_in_namespace``) and the tenant CMP sidecar reads the
secret in ``${ARGOCD_APP_NAMESPACE}``, the namespace of the application being rendered.
Overwriting every ``sops-age-key`` would therefore replace those projects' own keys with the
platform key and make their secrets unreadable.

Until RC-351 ``rig-ron`` held a second copy of the platform key, because the RON renderer read it
from its own destination namespace. The platform apps now render in the ``cmp-platform`` sidecar
with the key as a mount, so that copy is gone; see
``instructions/sops-sleutel-in-het-cluster.md``.

So the selection is by KEY and not by name, exactly like the file side selects by recipient: this
tool reads each secret, derives its public half, and only replaces the ones that currently hold
the OLD platform key. Everything else is listed as left alone, with its own public key next to
it, so the operator can see what was skipped and why.

**The secret holds the whole key file, not just the key line.** ``--from-file=key=<path>``, and
``configmap-sops-plugin.yaml`` greps ``^AGE-SECRET-KEY-`` out of it. This tool keeps that shape.

``--target project-kek`` places the project-KEK (RC-334): the key that wraps project private keys.
Differs from the platform key: comes from the CHOSEN CLUSTER out of ``CLUSTER_CONFIG``, not from
a search. The cluster is ``--cluster`` or ``CLUSTER_TYPE``. See ``features/sops-sleutel-roteren.md``
for the staging/production pentest issue and the full operational context.

    uv run --project operations-manager/python python scripts/set-sops-key-secret.py --target project-kek --cluster odcn-staging --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from key_rotation import (  # type: ignore[reportMissingImports]
    AGE_KEY_MARKER,
    MissingKey,
    ask_for_path,
    cluster_refusal,
    kek_file_for,
    opi_namespace_for,
    public_key_of,
    read_key,
    selected_cluster,
)
from opi.connectors.kubectl import create_kubectl_connector  # type: ignore[reportMissingImports]

REPO = Path(__file__).resolve().parents[1]
CANONICAL_NEW = REPO / "security" / "key.txt"
CANONICAL_OLD = REPO / "security" / "old_key.txt"
# S105 fires on both: they are a Kubernetes resource name and a data key, not a password.
SECRET_NAME = "sops-age-key"  # noqa: S105
KEK_SECRET_NAME = "project-kek"  # noqa: S105
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
    except (ValueError, UnicodeDecodeError):
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


async def write_secret(namespace: str, key_file: Path, secret_name: str = SECRET_NAME) -> None:
    """Replace the secret in one namespace, keeping the whole key file as its value.

    The rendered manifest goes through a 0600 temporary FILE and ``apply -f <path>``, never
    through ``stdin_input``: ``KubectlConnector.run_command`` builds a heredoc around a stdin
    payload and hands the whole thing to ``create_subprocess_shell``, so the manifest -- which is
    the entire key file, base64 encoded under ``data.key`` -- would stand in the argv of
    ``/bin/sh -c``, readable by every local uid from ``ps`` for as long as the apply runs. That is
    the property ``key_rotation.ask_for_path`` refuses a key argument for, and the machine this
    runs on is a shared dev server. With a file, argv carries a PATH.
    """
    kubectl = create_kubectl_connector()
    stdout, stderr, code = await kubectl.run_command(
        [
            "create",
            "secret",
            "generic",
            secret_name,
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
    handle, name = tempfile.mkstemp(prefix=f"{secret_name}-", suffix=".yaml")
    manifest = Path(name)
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(stdout)
        _out, stderr, code = await kubectl.run_command(["apply", "-n", namespace, "-f", str(manifest)])
        if code != 0:
            raise RuntimeError(f"kubectl apply failed in {namespace}: {stderr.strip()}")
    finally:
        manifest.unlink(missing_ok=True)


async def restart_operations_manager(namespace: str) -> bool:
    """Restart the operations-manager so it rereads the key. Returns False when absent.

    The key arrives as a mounted file that OPI reads once at startup, so without this it
    keeps using the old key even after kubelet has refreshed the file.
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


async def namespace_runs_opi(namespace: str) -> bool:
    """True als ``deployment/operations-manager`` in deze namespace staat.

    Een controle op EEN namespace, niet een zoekopdracht over het cluster: de namespace komt
    uit het gekozen cluster, en de deploymentnaam bewijst alleen dat OPI daar echt draait.
    """
    kubectl = create_kubectl_connector()
    _out, stderr, code = await kubectl.run_command(["get", "deployment", DEPLOYMENT, "-n", namespace, "-o", "name"])
    if code != 0 and "not found" not in stderr.lower():
        raise RuntimeError(f"kubectl get deployment in {namespace} failed: {stderr.strip()}")
    return code == 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="say which namespaces would change and stop")
    parser.add_argument(
        "--target",
        choices=[SECRET_NAME, KEK_SECRET_NAME],
        default=SECRET_NAME,
        help="which key to place: the platform key (default) or the project-KEK",
    )
    parser.add_argument(
        "--cluster",
        default=selected_cluster(),
        help="the cluster whose OPI namespace and KEK file to use; default CLUSTER_TYPE",
    )
    parser.add_argument(
        "--kek",
        help="key file to place as the project-KEK; default security/project-kek-<cluster>.txt",
    )
    parser.add_argument("--old-key", default=str(CANONICAL_OLD))
    parser.add_argument("--new-key", default=str(CANONICAL_NEW))
    parser.add_argument(
        "--confirm-cluster",
        help=(
            "the cluster name, for a non-interactive run; the current kubectl context, or with "
            "--target project-kek the chosen cluster"
        ),
    )
    return parser


def report_holders(holders: list[SecretHolder], old_public: str, new_public: str) -> list[SecretHolder]:
    """Print every holder, and return the ones that carry the old platform key.

    A secret whose value yields no public key gets its OWN heading: "carries a different key" is
    a statement about what is in it, and about an unreadable one nothing is known. It may be the
    platform key in a shape this tool does not recognise, and the final check measures FILES, so
    nothing downstream would catch that.
    """
    targets = [holder for holder in holders if holder.public_key == old_public]
    done = [holder for holder in holders if holder.public_key == new_public]
    unreadable = [holder for holder in holders if holder.public_key is None]
    others = [holder for holder in holders if holder not in targets and holder not in done and holder not in unreadable]

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
            print(f"  {holder.namespace}  {holder.public_key}")
    if unreadable:
        print(f"\n{len(unreadable)} hold a secret this tool cannot read a key out of, and are left alone:")
        for holder in unreadable:
            print(f"  {holder.namespace}")
        print("Check those by hand. If one of them holds the platform key in another shape, this")
        print("run leaves the old key live there, and the final check measures files and not")
        print("cluster secrets, so it will not notice.")
    return targets


async def main_project_kek(arguments: argparse.Namespace) -> int:
    """Place the project-KEK in the OPI namespace of the CHOSEN cluster, and nowhere else.

    No old key and no key comparison: the KEK is not rotated here, it is PLACED. The target
    comes from the cluster and not from a search, and the confirmation asks for the cluster and
    not for the kubectl context: staging and production share that context, so typing it would
    prove nothing about which of the two gets the key.

    That confirmation only catches a slip in an interactive run: with ``--confirm-cluster`` it
    compares two values off the same command line. The guard that holds is ``namespace_runs_opi``.
    """
    cluster = arguments.cluster.strip()
    refusal = cluster_refusal(cluster)
    if refusal:
        print(f"FAIL {refusal}", file=sys.stderr)
        return 2
    try:
        namespace = opi_namespace_for(cluster)
    except RuntimeError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2

    try:
        reader = (lambda _question: "") if arguments.confirm_cluster else input
        key_file = ask_for_path("project-KEK", arguments.kek or kek_file_for(cluster), reader=reader)
        private_key = read_key(key_file)
    except MissingKey as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2
    public_key = public_key_of(private_key)

    try:
        context = await current_cluster()
        runs_opi = await namespace_runs_opi(namespace)
    except RuntimeError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 2

    print(f"\nCluster: {cluster}  (kubectl context: {context})")
    print(f"Project-KEK (public): {public_key}")
    print(f"Key file: {key_file}")
    print(f"\nSecret '{KEK_SECRET_NAME}' goes into one namespace: {namespace}")
    if not runs_opi:
        print(
            f"\nFAIL {namespace} does not run deployment/{DEPLOYMENT}; wrong cluster or wrong context.", file=sys.stderr
        )
        return 2
    print("\nAfter the swap the deployment is restarted: OPI reads the key once, at startup.")

    if arguments.dry_run:
        print("\nDry run: nothing was changed.")
        return 0

    print("\nThis replaces whatever is in that secret now. A project file wrapped with an")
    print("earlier KEK no longer opens afterwards.")
    typed = arguments.confirm_cluster or input(f"Type the cluster name ({cluster}): ")
    if typed.strip() != cluster:
        print("FAIL cluster name does not match; nothing changed.", file=sys.stderr)
        return 1

    await write_secret(namespace, key_file, KEK_SECRET_NAME)
    print(f"  secret placed in {namespace}")
    if await restart_operations_manager(namespace):
        print(f"  deployment/{DEPLOYMENT} restarted and ready in {namespace}")

    print("\nDone. Now convert the project files, from the repository root:")
    print("  uv run --project operations-manager/python python scripts/rewrap-project-keys.py")
    print(f"  --cluster {cluster} --projects <zad-projects-clone>/projects --deployments <clone> --dry-run")
    return 0


async def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.target == KEK_SECRET_NAME:
        return await main_project_kek(arguments)

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
    print("Then, from the repository root, the final check of step 6 -- both clones, because")
    print("without them the fourth and fifth place are not walked:")
    print("  uv run --project operations-manager/python python scripts/rotate-sops-key.py --assert-old-key-dead")
    print("  --projects <zad-projects-clone>/projects --argo-applications <zad-argo-clone>")
    return 0
