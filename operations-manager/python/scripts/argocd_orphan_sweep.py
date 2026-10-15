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

Three layers, read from the projects file down:

* ARGO     -- every Application in the argo repo whose render root belongs to no
              deployment in the projects repo. ArgoCD keeps it, and what it deploys,
              running for as long as it stands in git. Reported only: removing it is a
              commit to the argo repo.
* GIT      -- every ``<cluster>/<project>/<leaf>`` directory in the deployments repo. Each
              one is a render root an Application should point at with spec.source.path.
* CLUSTER  -- every resource ArgoCD marked as its own, by whichever of its two tracking
              marks this cluster uses (opi.utils.argocd_tracking). The name in the mark
              is its Application.

In a Capsule tenant the account may not list every type the cluster serves, so a
namespace inventory is never complete over all of them. With the deployments repo given,
completeness is judged over the kinds that repo renders instead, and a rendered type that
does not answer still refuses. SCHOON then means 'nothing in the kinds this repo renders
today', which is narrower than 'nothing at all': a kind nothing renders any more, and one
whose ``kind:`` line is a template, fall outside it.

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

    # all three layers; required in a tenant, where the deployments repo decides completeness
    uv run python scripts/argocd_orphan_sweep.py --cluster odcn-production \
        --projects-repo /path/to/projects --argo-repo /path/to/argo-applications \
        --deployments-repo /path/to/zad-deployments

    # actually delete what it found. Cluster resources are deleted outright.
    uv run python scripts/argocd_orphan_sweep.py --namespace rig-prd-mpfm-w3h --delete

    # deleting orphaned repo directories too (removed from the checkout, committing them
    # stays yours). That refuses without all three repos AND --cluster: short of them the
    # directory of a deployment whose Application is momentarily absent reads as an orphan.
    uv run python scripts/argocd_orphan_sweep.py --cluster odcn-production \
        --projects-repo /path/to/projects --argo-repo /path/to/argo-applications \
        --deployments-repo /path/to/zad-deployments --delete

Exit codes: 0 when nothing was found (or everything found was deleted), 1 when something
remains, 2 when it refused to sweep. That makes it usable as the last step of a delete
test: it measures what is left over, not what appeared to happen.

What remains is not only orphans: a resource whose mark cannot be placed either way is
reported too and never deleted (``undecidable_resources``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import posixpath
import re
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

from opi.connectors.kubectl import (  # noqa: E402
    KubectlConnectionError,
    KubectlExecutionError,
    create_kubectl_connector,
)
from opi.core.cluster_config import get_infrastructure_namespace, get_prefixed_namespace  # noqa: E402
from opi.core.config import settings  # noqa: E402
from opi.services.schema_migration import migrate_to_latest  # noqa: E402
from opi.utils.argocd_tracking import LABEL_VALUE_MAX, may_be_cut_from  # noqa: E402
from opi.utils.naming import (  # noqa: E402
    generate_deployment_manifest_path,
    generate_infrastructure_manifest_path,
    generate_project_level_manifest_path,
)
from ruamel.yaml import YAML, YAMLError  # noqa: E402

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

#: What OPI writes an Application as in the argo repo: ``<cluster>/<project>/<name>-argocd-application.yaml``.
ARGO_APPLICATION_GLOB = "*/*-argocd-application.yaml"

#: The projects repo keeps its files here.
PROJECTS_DIR = "projects"

_yaml = YAML(typ="safe")

_DOCUMENT_SEPARATOR = re.compile(r"(?m)^---")

#: ``apiVersion:`` or ``kind:`` at the start of a line, so at the top of a document and
#: never the nested ``kind:`` of an ownerReference or a roleRef.
_TOP_LEVEL_FIELD = re.compile(r"(?m)^(apiVersion|kind):[ \t]*['\"]?([^'\"\s#]*)")


class SweepRefused(RuntimeError):
    """The inventory cannot be trusted, so nothing is reported and nothing is deleted."""


def orphaned_resources(tracked: list[TrackedResource], existing_applications: set[str]) -> list[TrackedResource]:
    """The tracked resources whose Application no longer exists.

    A resource this tool cannot place is not one of them: ``undecidable_resources``.
    """
    return [
        resource
        for resource in tracked
        if resource.app_name not in existing_applications and not may_be_cut_from(resource, existing_applications)
    ]


def undecidable_resources(tracked: list[TrackedResource], existing_applications: set[str]) -> list[TrackedResource]:
    """The tracked resources this tool cannot place either way.

    Their mark sits on the label cap and a living Application's name starts with it, so the
    resource is either that Application's, marked with its name cut to the cap, or the last
    thing left of an Application that was called exactly this and is gone. ``may_be_cut_from``
    says why nothing in the mark decides between those two.

    They are kept out of the orphan list, because ``--delete`` acts on it and the first
    reading is a running deployment. They are reported all the same: reading them as owned
    without saying so is a real orphan answered with SCHOON.

    A mark that IS the name of a living Application is nothing to report: the resource is
    then that one's or the longer sister's, and neither of those is an orphan.
    """
    return [
        resource
        for resource in tracked
        if resource.app_name not in existing_applications and may_be_cut_from(resource, existing_applications)
    ]


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

    A reference BELOW a render root protects it too: a repository entry with a non-empty
    ``path`` puts the tree a level deeper than ``RENDER_ROOT_DEPTH``, and an exact
    comparison would then call every live project directory an orphan, which ``--delete``
    would take along.
    """
    normalised = {posixpath.normpath(path).strip("/") for path in referenced if path}
    return [
        root
        for root in render_roots(repo_root)
        if not any(reference == root or reference.startswith(f"{root}/") for reference in normalised)
    ]


def _load_yaml(path: Path) -> object:
    try:
        return _yaml.load(path.read_text())
    except YAMLError as e:
        # A file that cannot be read is a file whose content is unknown, and unknown is
        # exactly where this tool would otherwise draw a wrong conclusion.
        raise SweepRefused(f"{path} does not parse: {e}") from e


def _projects(projects_repo: Path) -> list[tuple[str, dict]]:
    """Every project file as ``(name, data)``, read after migration: the repo holds every
    schema version from 2 to the latest."""
    project_files = sorted((projects_repo / PROJECTS_DIR).glob("*.yaml"))
    if not project_files:
        raise SweepRefused(
            f"no project files under {projects_repo / PROJECTS_DIR}; every Application would be an orphan"
        )

    projects = []
    for path in project_files:
        data = _load_yaml(path)
        if not isinstance(data, dict):
            raise SweepRefused(f"{path} is not a project file")
        data, _ = migrate_to_latest(data)
        projects.append((data.get("name") or path.stem, data))
    return projects


def helmfile_projects(projects_repo: Path) -> set[str]:
    """The projects with a deployment rendered by helmfile, which this tool leaves alone.

    Their manifests come from charts OPI passes through, not from OPI's own templates, so
    what is theirs is not this tool's to judge. Same test OPI applies
    (ProjectManager._deployment_uses_helmfile).
    """
    return {
        name
        for name, data in _projects(projects_repo)
        if any(deployment.get("helmfile") for deployment in data.get("deployments") or [])
    }


def helmfile_namespaces(projects_repo: Path, cluster: str) -> set[str]:
    """The namespaces of the helmfile projects on this cluster, their infrastructure one included."""
    namespaces = set()
    for name, data in _projects(projects_repo):
        deployments = data.get("deployments") or []
        if not any(deployment.get("helmfile") for deployment in deployments):
            continue
        namespaces.add(get_infrastructure_namespace(cluster, name))
        namespaces |= {
            get_prefixed_namespace(cluster, deployment.get("namespace") or name)
            for deployment in deployments
            if deployment.get("cluster", cluster) == cluster
        }
    return namespaces


def expected_render_roots(projects_repo: Path, cluster: str) -> frozenset[str]:
    """The render roots the projects repo asks for on this cluster.

    The paths come from the naming functions OPI writes them with, so they cannot drift
    from the repo. Every project keeps its project level and infrastructure root, also
    without a deployment here: claiming too much leaves an orphan unreported, claiming
    too little offers a live Application for removal.
    """
    roots = set()
    for project, data in _projects(projects_repo):
        roots.add(generate_project_level_manifest_path(cluster, project))
        roots.add(generate_infrastructure_manifest_path(cluster, project))
        for deployment in data.get("deployments") or []:
            # Same choice as above: a deployment without a cluster protects its root here.
            if deployment.get("cluster", cluster) == cluster:
                roots.add(generate_deployment_manifest_path(cluster, project, deployment["name"]))
    return frozenset(roots)


def orphaned_applications(
    argo_repo: Path, cluster: str, expected: frozenset[str], skipped_projects: frozenset[str] = frozenset()
) -> list[tuple[str, str]]:
    """The Applications in the argo repo whose render root no deployment asks for, as ``(name, root)``."""
    orphans = []
    for path in sorted((argo_repo / cluster).glob(ARGO_APPLICATION_GLOB)):
        if path.parent.name in skipped_projects:
            continue
        doc = _load_yaml(path)
        if not isinstance(doc, dict) or doc.get("kind") != "Application":
            continue
        source_path = ((doc.get("spec") or {}).get("source") or {}).get("path")
        if not source_path:
            # Several sources, or none: not a shape OPI writes, so not one to judge here.
            continue
        root = posixpath.normpath(source_path).strip("/")
        if root not in expected:
            orphans.append(((doc.get("metadata") or {}).get("name", path.name), root))
    return orphans


def rendered_kinds(
    deployments_repo: Path, cluster: str, skipped_projects: frozenset[str] = frozenset()
) -> tuple[set[str], list[str]]:
    """Every kind the deployments repo renders for this cluster, as ``Kind.group`` (core: ``Kind``).

    Read from the top-level ``apiVersion:`` and ``kind:`` lines rather than parsed: the
    helmfile projects carry Helm chart templates, which are not YAML until ArgoCD renders
    them. An ``apiVersion`` that is a template gives ``Kind.*``, every group serving that
    kind. A ``kind`` that is a template cannot be read at all; those files come back as
    the second value, to be reported rather than silently left out.
    """
    kinds = set()
    unreadable = []
    for path in sorted((deployments_repo / cluster).rglob("*.yaml")):
        parts = path.relative_to(deployments_repo).parts
        if any(part.startswith(".") for part in parts) or parts[1] in skipped_projects:
            continue
        for document in _DOCUMENT_SEPARATOR.split(path.read_text(errors="replace")):
            fields = dict(_TOP_LEVEL_FIELD.findall(document))
            kind = fields.get("kind", "")
            if not kind:
                continue
            if kind.startswith("ENC["):
                # A SOPS file with its kind encrypted. OPI encrypts only what it writes as
                # .to-sops.yaml, and those are Secrets: on odcn-production, 9 October 2026,
                # all 707 carried a 6-byte kind and a 2-byte apiVersion.
                kinds.add("Secret")
            elif not kind.isalnum():
                unreadable.append(path.relative_to(deployments_repo).as_posix())
            elif "{{" in fields.get("apiVersion", ""):
                kinds.add(f"{kind}.*")
            else:
                group = fields.get("apiVersion", "").rpartition("/")[0]
                kinds.add(f"{kind}.{group}" if group else kind)
    return kinds, unreadable


def rendered_types(kinds: set[str], served: dict[str, str]) -> list[str]:
    """The kubectl type names of the rendered kinds this cluster serves.

    A rendered kind the cluster does not serve cannot have been created there.
    """
    types = set()
    for kind in kinds:
        if kind.endswith(".*"):
            name = kind[:-2]
            types |= {type_name for key, type_name in served.items() if key.partition(".")[0] == name}
        elif kind in served:
            types.add(served[kind])
    return sorted(types)


def format_report(
    resources: list[TrackedResource],
    undecidable: list[TrackedResource],
    paths: list[str],
    after_delete: bool = False,
    applications: list[tuple[str, str]] | None = None,
) -> str:
    """The findings: the Applications git still keeps for nothing, the orphans grouped by
    their Application, then what could not be placed, then the repo paths."""
    if not resources and not paths and not undecidable and not applications:
        return CLEAN

    verb = "Still standing" if after_delete else "Found"
    lines = []

    if applications:
        lines.append(
            f"{verb} {len(applications)} Application(s) in the argo repo for a deployment the projects repo "
            "no longer has. Not deleted here: removing them is a commit to the argo repo."
        )
        lines += [f"  {name}  ({root})" for name, root in applications]

    if resources:
        by_application: dict[str, list[TrackedResource]] = {}
        for resource in resources:
            by_application.setdefault(resource.app_name, []).append(resource)

        if lines:
            lines.append("")
        lines.append(f"{verb} {len(resources)} resource(s) whose Application no longer exists:")
        for app_name in sorted(by_application):
            owned = by_application[app_name]
            namespaces = sorted({resource.namespace for resource in owned})
            lines.append(f"  {app_name}  ({len(owned)} resources in {', '.join(namespaces)})")
            lines.extend(
                f"    {resource.kind:<24} {resource.name}" for resource in sorted(owned, key=lambda r: (r.kind, r.name))
            )

    if undecidable:
        if lines:
            lines.append("")
        lines.append(
            f"{verb} {len(undecidable)} resource(s) whose mark sits on the {LABEL_VALUE_MAX}-character label cap "
            "and could be either a living Application's or an orphan's. Never deleted, judge them by hand:"
        )
        lines += [
            f"  {resource.app_name}  {resource.namespace}/{resource.kind.lower()}/{resource.name}"
            for resource in sorted(undecidable, key=lambda r: (r.app_name, r.namespace, r.kind, r.name))
        ]

    if paths:
        if lines:
            lines.append("")
        lines.append(f"{verb} {len(paths)} deployments-repo path(s) no Application points at:")
        lines += [f"  {path}" for path in paths]

    return "\n".join(lines)


async def inventory(
    namespaces: list[str] | None,
    deployments_repo: Path | None,
    cluster: str | None = None,
    skipped_projects: frozenset[str] = frozenset(),
    skipped_namespaces: frozenset[str] = frozenset(),
    expected_roots: frozenset[str] = frozenset(),
) -> tuple[list[TrackedResource], list[TrackedResource], list[str]]:
    """Both sources, measured: the orphaned resources, the undecidable ones, and the repo paths.

    ``cluster`` names the ``<cluster>/`` folder of the deployments repo whose kinds decide
    whether a namespace that answered for part of its types still counts as measured.
    The skipped projects and namespaces are the helmfile ones (``helmfile_projects``).
    ``expected_roots`` are the roots the projects repo asks for (``expected_render_roots``),
    which protect a directory the same way a live Application pointing at it does.
    """
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

    try:
        label_map = await kubectl.get_namespace_label_map(NAMESPACE_OWNER_LABEL)
    except KubectlExecutionError as e:
        # The only read here that answers a failure with KubectlExecutionError; the other
        # three answer with a sentinel. Left to escape it ends the run on exit 1, and exit 1
        # means "there are orphans". KubectlConnectionError, which all four can raise, is
        # caught in main().
        raise SweepRefused(f"the cluster did not answer which namespaces OPI created: {e}") from e

    owned = {
        namespace
        for namespace, value in label_map.items()
        if value == NAMESPACE_OWNER_VALUE and namespace not in skipped_namespaces
    }
    if namespaces is None:
        # Any OPI namespace can hold leftovers, including one whose Applications are all
        # gone, so the list comes from the cluster and not from the Applications that are
        # still standing.
        namespaces = sorted(owned)
    else:
        outside = [namespace for namespace in namespaces if namespace not in owned]
        if outside:
            raise SweepRefused(
                f"not created by OPI or rendered by helmfile, so not this tool's to sweep: {', '.join(sorted(outside))}"
            )

    resource_types = await kubectl.list_namespaced_resource_types()
    if resource_types is None:
        # Same reason as the refusal above: a failed read must never answer as a measurement.
        raise SweepRefused("the cluster did not answer which resource types it has; nothing could be inventoried")

    types_we_render = None
    if deployments_repo is not None and cluster:
        served = await kubectl.list_namespaced_kinds()
        if served is None:
            raise SweepRefused("the cluster did not answer which kinds it serves; completeness cannot be judged")
        kinds, unreadable = rendered_kinds(deployments_repo, cluster, skipped_projects)
        types_we_render = rendered_types(kinds, served)
        if unreadable:
            print(
                f"  {len(unreadable)} manifest(s) template their kind, which is not judged for completeness: "
                + ", ".join(unreadable),
                file=sys.stderr,
            )

    orphans: list[TrackedResource] = []
    undecidable: list[TrackedResource] = []
    for namespace in namespaces:
        print(f"  inventorying {namespace}", file=sys.stderr)
        inventory_of_namespace = await kubectl.list_tracked_resources(namespace, resource_types)
        if inventory_of_namespace is None:
            raise SweepRefused(f"namespace '{namespace}' could not be inventoried; refusing to call it clean")
        tracked, complete = inventory_of_namespace
        if not complete:
            # A namespace that answered for part of its types is no more measured than one
            # that did not answer at all: the types that stayed silent are exactly where an
            # orphan would be. The type list is discovered ONCE for the whole run, against
            # the verbs of the cluster, so a type this service account may not list in this
            # namespace gets here rather than being caught by the discovery refusal above.
            #
            # In a Capsule tenant that is every namespace: the account may not list ~90 of
            # the types the cluster serves. What decides then is whether every kind WE
            # render answered, asked again on its own.
            if types_we_render is None:
                raise SweepRefused(
                    f"namespace '{namespace}' answered for only part of its resource types; refusing to call it "
                    "clean (pass --deployments-repo and --cluster to judge it by the kinds that repo renders)"
                )
            rendered_inventory = await kubectl.list_tracked_resources(namespace, types_we_render)
            if rendered_inventory is None or not rendered_inventory[1]:
                raise SweepRefused(
                    f"namespace '{namespace}' did not answer for every kind the deployments repo renders; "
                    "refusing to call it clean"
                )
            tracked = list(dict.fromkeys([*tracked, *rendered_inventory[0]]))
        orphans.extend(orphaned_resources(tracked, existing))
        undecidable.extend(undecidable_resources(tracked, existing))

    # Only this cluster's folder: the Applications read above are this cluster's, so every
    # path of another cluster would read as one no Application points at. Measured on
    # odcn-production, 9 October 2026: the whole local/ tree was reported.
    #
    # What the projects repo asks for protects a directory too: an Application a force just
    # removed leaves its deployment standing there, and its directory needed.
    protected = referenced | expected_roots
    paths = [
        path
        for path in (orphaned_paths(deployments_repo, protected) if deployments_repo else [])
        if path.split("/")[1] not in skipped_projects and (not cluster or path.split("/")[0] == cluster)
    ]
    return orphans, undecidable, paths


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


async def _sweep(
    namespaces: list[str] | None,
    deployments_repo: Path | None,
    delete: bool,
    cluster: str | None = None,
    projects_repo: Path | None = None,
    argo_repo: Path | None = None,
) -> int:
    applications = []
    skipped_projects: frozenset[str] = frozenset()
    skipped_namespaces: frozenset[str] = frozenset()
    expected: frozenset[str] = frozenset()
    if projects_repo is not None and argo_repo is not None and cluster:
        skipped_projects = frozenset(helmfile_projects(projects_repo))
        skipped_namespaces = frozenset(helmfile_namespaces(projects_repo, cluster))
        print(f"helmfile projects left alone: {', '.join(sorted(skipped_projects)) or '-'}", file=sys.stderr)
        expected = expected_render_roots(projects_repo, cluster)
        applications = orphaned_applications(argo_repo, cluster, expected, skipped_projects)

    resources, undecidable, paths = await inventory(
        namespaces, deployments_repo, cluster, skipped_projects, skipped_namespaces, expected
    )
    print(format_report(resources, undecidable, paths, applications=applications))

    if delete and (resources or paths):
        resources, paths = await remove(resources, paths, deployments_repo)
        print(format_report(resources, undecidable, paths, after_delete=True, applications=applications))

    return 1 if resources or undecidable or paths or applications else 0


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
        "--projects-repo",
        type=Path,
        help="checkout of the projects repo (the directory holding projects/)",
    )
    parser.add_argument(
        "--argo-repo",
        type=Path,
        help="checkout of the argo-applications repo (the directory holding the <cluster>/ folders)",
    )
    parser.add_argument(
        "--cluster",
        default=settings.CLUSTER_MANAGER,
        help="the <cluster>/ folder to read in the repos (default: CLUSTER_MANAGER)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help=(
            "delete what was found instead of only reporting it; with --deployments-repo it needs "
            "--projects-repo, --argo-repo and --cluster as well"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    for repo in (args.deployments_repo, args.projects_repo, args.argo_repo):
        if repo is not None and not repo.is_dir():
            print(f"Not a directory: {repo}", file=sys.stderr)
            return 2
    if (args.projects_repo is None) != (args.argo_repo is None):
        print("--projects-repo and --argo-repo go together: the one is measured against the other", file=sys.stderr)
        return 2
    if (
        args.delete
        and args.deployments_repo is not None
        and not (args.projects_repo and args.argo_repo and args.cluster)
    ):
        # Exactly the condition under which _sweep computes the protection. Short of it a
        # directory is judged on the Applications the cluster has RIGHT NOW, and one a
        # force just removed leaves its deployment standing there.
        print(
            "--delete with --deployments-repo needs --projects-repo, --argo-repo and --cluster "
            f"(cluster is now '{args.cluster}'): without all three the directory of a deployment whose "
            "Application is momentarily absent reads as an orphan",
            file=sys.stderr,
        )
        return 2

    # Which cluster and which ArgoCD this run reads, before it reads them: a sweep pointed
    # at the sandbox's rig-system while the context is production refuses, but only after
    # minutes, and says nothing about why.
    print(f"cluster {args.cluster}, ArgoCD namespace {settings.ARGOCD_MANAGER}", file=sys.stderr)

    try:
        return asyncio.run(
            _sweep(
                args.namespaces,
                args.deployments_repo,
                args.delete,
                args.cluster,
                args.projects_repo,
                args.argo_repo,
            )
        )
    except (SweepRefused, KubectlConnectionError) as e:
        # Every read in inventory() raises KubectlConnectionError once the cluster is
        # unreachable, and exit 1 would mean "there are orphans".
        print(f"Refusing to sweep: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
