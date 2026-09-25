#!/usr/bin/env python3
"""Find what a deleted ArgoCD Application left behind. Reports; deletes only on request.

Forcing a stuck deletion used to remove the Application's finalizer while its sync
operation was still blocking the cascade. The Application then disappeared without a
single delete request ever being made for its resources. Measured in rig-prd-mpfm-w3h on
24 September 2026: ten vanished Applications, 350 resources left standing (140 of them
secrets), not one carrying a deletionTimestamp, plus eleven directories in the
deployments repo that no Application pointed at any more.

The delete route no longer creates these (RC-226). This tool is for what is already
there, and for proving afterwards that a deletion took everything with it.

Two sources, one test: does the Application this thing belongs to still exist?

* CLUSTER  -- every resource ArgoCD marked as its own, by whichever of its two tracking
              marks this cluster uses (opi.utils.argocd_tracking). The name in the mark
              is its Application.
* GIT      -- every ``<cluster>/<project>/<leaf>`` directory in the deployments repo. Each
              one is a render root an Application should point at with spec.source.path.

Existence is read from the Kubernetes API, never from ArgoCD's: ArgoCD answers an
ambiguous 'permission denied' for applications that do exist, and reading a live
Application as absent would make this tool delete a running deployment.

Usage:
    cd operations-manager/python

    # report on the whole cluster
    uv run python scripts/argocd_orphan_sweep.py

    # one namespace, which is much faster
    uv run python scripts/argocd_orphan_sweep.py --namespace rig-prd-mpfm-w3h

    # add the git side (the directory holding the <cluster>/ folders)
    uv run python scripts/argocd_orphan_sweep.py --deployments-repo /path/to/zad-deployments

    # actually delete what it found. Cluster resources are deleted outright; orphaned
    # repo directories are removed from the checkout, committing them stays yours.
    uv run python scripts/argocd_orphan_sweep.py --namespace rig-prd-mpfm-w3h --delete

Exit codes: 0 when nothing was found (or everything found was deleted), 1 when orphans
remain, 2 when it refused to sweep. That makes it usable as the last step of a delete
test: it measures what is left over, not what appeared to happen.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import posixpath
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Make ``opi`` importable no matter the working directory: the OPI package lives in
# operations-manager/python, the parent of this scripts/ directory. Without this the
# documented ``uv run python scripts/argocd_orphan_sweep.py`` fails with
# ModuleNotFoundError, because Python puts scripts/ (not the package root) on sys.path.
_OPI_ROOT = Path(__file__).resolve().parents[1]
if str(_OPI_ROOT) not in sys.path:
    sys.path.insert(0, str(_OPI_ROOT))

from opi.connectors.kubectl import create_kubectl_connector  # noqa: E402

if TYPE_CHECKING:
    from opi.utils.argocd_tracking import TrackedResource

#: The label OPI puts on every namespace it creates (manifests/namespace.yaml.jinja). The
#: sweep looks ONLY inside those, as an allowlist rather than a list of names to skip.
#:
#: This is not caution, it is a measurement. With the label tracking method, Helm writes
#: ``app.kubernetes.io/instance`` too, and it means the Helm release, not an Application.
#: On the sandbox on 24 September 2026 six resources in ``ingress-nginx`` carried
#: ``instance: ingress-nginx`` while no Application by that name exists, so a sweep that
#: merely skipped kube-system would have offered the ingress controller for deletion.
NAMESPACE_OWNER_LABEL = "created-by"
NAMESPACE_OWNER_VALUE = "operations-manager"

#: Depth of a render root in the deployments repo: ``<cluster>/<project>/<leaf>``, the
#: shape opi.utils.naming produces for this repo.
RENDER_ROOT_DEPTH = 3

CLEAN = "SCHOON"


class SweepRefused(RuntimeError):
    """The inventory cannot be trusted, so nothing is reported and nothing is deleted."""


def orphaned_resources(tracked: list[TrackedResource], existing_applications: set[str]) -> list[TrackedResource]:
    """The tracked resources whose Application no longer exists."""
    return [resource for resource in tracked if resource.app_name not in existing_applications]


def render_roots(repo_root: Path) -> list[str]:
    """Every ``<cluster>/<project>/<leaf>`` directory in a deployments-repo checkout."""
    roots = []
    for path in sorted(repo_root.glob("/".join(["*"] * RENDER_ROOT_DEPTH))):
        if not path.is_dir() or any(part.startswith(".") for part in path.relative_to(repo_root).parts):
            continue
        roots.append(path.relative_to(repo_root).as_posix())
    return roots


def orphaned_paths(repo_root: Path, referenced: set[str]) -> list[str]:
    """The render roots no Application points at with its ``spec.source.path``.

    The reference is normalised first. OPI writes the path bare, but the Applications on
    the cluster carry it as ``./sandboxed-local/<project>/<deployment>`` (measured on the
    sandbox, 24 September 2026), and comparing those two forms literally would call every
    live path an orphan.

    A reference BELOW a render root protects it too. ``RENDER_ROOT_DEPTH`` is the layout of
    this repo today, but a repository entry with a non-empty ``path`` puts the tree a level
    deeper, and then an exact comparison would call every live project directory an orphan
    and ``--delete`` would take them all. Living above a referenced path is proof enough of
    life.
    """
    normalised = {posixpath.normpath(path).strip("/") for path in referenced if path}
    return [
        root
        for root in render_roots(repo_root)
        if not any(reference == root or reference.startswith(f"{root}/") for reference in normalised)
    ]


def format_report(resources: list[TrackedResource], paths: list[str], after_delete: bool = False) -> str:
    """The findings, grouped by the Application that should have taken them along."""
    if not resources and not paths:
        return CLEAN

    verb = "Still standing" if after_delete else "Found"
    lines = []

    if resources:
        by_application: dict[str, list[TrackedResource]] = {}
        for resource in resources:
            by_application.setdefault(resource.app_name, []).append(resource)

        lines.append(f"{verb} {len(resources)} resource(s) whose Application no longer exists:")
        for app_name in sorted(by_application):
            owned = by_application[app_name]
            namespaces = sorted({resource.namespace for resource in owned})
            lines.append(f"  {app_name}  ({len(owned)} resources in {', '.join(namespaces)})")
            lines.extend(
                f"    {resource.kind:<24} {resource.name}" for resource in sorted(owned, key=lambda r: (r.kind, r.name))
            )

    if paths:
        if lines:
            lines.append("")
        lines.append(f"{verb} {len(paths)} deployments-repo path(s) no Application points at:")
        lines += [f"  {path}" for path in paths]

    return "\n".join(lines)


async def inventory(
    namespaces: list[str] | None,
    deployments_repo: Path | None,
) -> tuple[list[TrackedResource], list[str]]:
    """Both sources, measured: the tracked resources and the repo paths that are orphaned."""
    kubectl = create_kubectl_connector()

    applications = await kubectl.list_argocd_applications()
    if not applications:
        # An empty list reads identically whether the cluster holds no Applications or the
        # query failed, and in both readings EVERY tracked resource becomes an orphan. A
        # platform cluster always runs at least user-applications, so this is a failed read.
        raise SweepRefused("the cluster returned no ArgoCD Applications; refusing to call everything an orphan")

    existing = {app.get("metadata", {}).get("name", "") for app in applications}
    referenced = {(app.get("spec", {}).get("source", {}) or {}).get("path", "") for app in applications}
    print(f"{len(existing)} ArgoCD Application(s) exist", file=sys.stderr)

    owned = {
        namespace
        for namespace, value in (await kubectl.get_namespace_label_map(NAMESPACE_OWNER_LABEL)).items()
        if value == NAMESPACE_OWNER_VALUE
    }
    if namespaces is None:
        # Any OPI namespace can hold leftovers, including one whose Applications are all
        # gone, so the list comes from the cluster and not from the Applications that are
        # still standing.
        namespaces = sorted(owned)
    else:
        outside = [namespace for namespace in namespaces if namespace not in owned]
        if outside:
            raise SweepRefused(f"not created by OPI, so not this tool's to sweep: {', '.join(sorted(outside))}")

    resource_types = await kubectl.list_namespaced_resource_types()
    if resource_types is None:
        # Same reason as the refusal above: a failed read must never answer as a measurement.
        raise SweepRefused("the cluster did not answer which resource types it has; nothing could be inventoried")

    orphans: list[TrackedResource] = []
    for namespace in namespaces:
        print(f"  inventorying {namespace}", file=sys.stderr)
        tracked = await kubectl.list_tracked_resources(namespace, resource_types)
        if tracked is None:
            raise SweepRefused(f"namespace '{namespace}' could not be inventoried; refusing to call it clean")
        orphans.extend(orphaned_resources(tracked, existing))

    paths = orphaned_paths(deployments_repo, referenced) if deployments_repo else []
    return orphans, paths


async def remove(
    resources: list[TrackedResource],
    paths: list[str],
    deployments_repo: Path | None,
) -> tuple[list[TrackedResource], list[str]]:
    """Delete the findings, and answer with what is STILL standing afterwards."""
    kubectl = create_kubectl_connector()
    remaining_resources = await kubectl.delete_tracked_resources(resources)

    if paths and deployments_repo is not None:
        for path in paths:
            shutil.rmtree(deployments_repo / path)
        print(
            f"Removed {len(paths)} directory/directories from the checkout at {deployments_repo}. "
            "Committing and pushing them is yours to do.",
            file=sys.stderr,
        )

    return remaining_resources, []


async def _sweep(namespaces: list[str] | None, deployments_repo: Path | None, delete: bool) -> int:
    resources, paths = await inventory(namespaces, deployments_repo)
    print(format_report(resources, paths))

    if delete and (resources or paths):
        resources, paths = await remove(resources, paths, deployments_repo)
        print(format_report(resources, paths, after_delete=True))

    return 1 if resources or paths else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--namespace",
        action="append",
        dest="namespaces",
        help="only this namespace (repeatable); default is every namespace OPI created",
    )
    parser.add_argument(
        "--deployments-repo",
        type=Path,
        help="checkout of the deployments repo (the directory holding the <cluster>/ folders)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="delete what was found instead of only reporting it",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    if args.deployments_repo is not None and not args.deployments_repo.is_dir():
        print(f"Not a directory: {args.deployments_repo}", file=sys.stderr)
        return 2

    try:
        return asyncio.run(_sweep(args.namespaces, args.deployments_repo, args.delete))
    except SweepRefused as e:
        print(f"Refusing to sweep: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
