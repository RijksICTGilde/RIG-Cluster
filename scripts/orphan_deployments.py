#!/usr/bin/env python3
"""Rapporteer achtergebleven deployments in de GitOps-repo's tegenover de projectbestanden.

De projectbestanden (``zad-projects``) zijn de waarheid. Een deployment die daar
niet (meer) in staat, maar wél nog een manifestmap heeft in de deployments-repo
of nog een ArgoCD Application heeft in de argo-repo, is een wees: ArgoCD blijft
hem syncen, de pods blijven draaien en de bijbehorende service-resources
(database, Keycloak-clients, bucket, PVC's) zijn vermoedelijk ook nooit
opgeruimd.

Het script leest drie lokale checkouts en produceert een rapport per project.
Zonder ``--prune`` muteert het niets:

* **wees-deployments** - map en/of Argo-app zonder deployment in het projectbestand
* **niet-uitgerold**   - deployment in het projectbestand zonder manifestmap
* **wees-projecten**   - map zonder projectbestand (project helemaal verwijderd)

Per wees wordt vermeld: welke services die deployment gebruikte (afgeleid uit de
achtergebleven manifesten én uit de laatste versie van het projectbestand waarin
de deployment nog stond), en welke resource-namen daaruit volgen en dus
handmatig gecontroleerd moeten worden.

Gebruik::

    ./scripts/orphan_deployments.py
    ./scripts/orphan_deployments.py --project wies --json /tmp/wezen.json
    ./scripts/orphan_deployments.py --no-history      # sla de git-archeologie over
    ./scripts/orphan_deployments.py --prune           # verwijder de veilige wees-mappen

De standaardpaden wijzen naar de lokale checkouts in
``~/IdeaProjects/rig-cluster-test-git-repositories``; overschrijf ze met
``--deployments-repo`` / ``--projects-repo`` / ``--argo-repo``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

import yaml

DEFAULT_ROOT = os.path.expanduser("~/IdeaProjects/rig-cluster-test-git-repositories")
DEFAULT_DEPLOYMENTS_REPO = os.path.join(DEFAULT_ROOT, "rig-cluster-application-test-github")
DEFAULT_PROJECTS_REPO = os.path.join(DEFAULT_ROOT, "rig-cluster-projects")
DEFAULT_ARGO_REPO = os.path.join(DEFAULT_ROOT, "argo-applications-github")
DEFAULT_CLUSTER = "odcn-production"

# Bestandsnaam-achtervoegsel -> (servicenaam, wat er buiten de cluster van overblijft).
# Volgorde is niet relevant, de eerste match per bestand telt.
MANIFEST_MARKERS: list[tuple[str, str]] = [
    ("-database-secret.sops.yaml", "postgresql-database"),
    ("-db-cluster.yaml", "namespace-postgresql-database"),
    ("-keycloak-secret.sops.yaml", "keycloak"),
    ("-minio-secret.sops.yaml", "minio-storage"),
    ("-redis-secret.sops.yaml", "redis"),
    ("-oauth2-cookie-secret.sops.yaml", "authorization-wall"),
    ("-authorization-wall-configmap.yaml", "authorization-wall"),
    ("-registry-secret.sops.yaml", "private-registry"),
    ("-attachment-secret.sops.yaml", "attachments"),
    ("-servicemonitor.yaml", "metrics-scraper"),
    ("-pvc.yaml", "persistent-storage"),
    ("-pvc.marked-for-deletion.yaml", "persistent-storage"),
    ("-ingress.yaml", "publish-on-web"),
    ("-ingress-root.yaml", "publish-on-web"),
]

# Deployments die OPI zelf genereert en die dus nooit in het projectbestand staan
# (``project_manager`` rendert "infrastructure" voor de in-namespace PostgreSQL en de
# baseline-netwerkpolicies). Alleen een wees als het hele project weg is.
PLATFORM_DEPLOYMENTS = {"infrastructure"}

# Services waarvan resources buiten de deployment-namespace leven en dus blijven
# staan als alleen de ArgoCD-app verdwijnt.
EXTERNAL_SERVICES = {
    "postgresql-database",
    "keycloak",
    "minio-storage",
    "redis",
    "publish-on-web",
}


def run_git(repo: str, *args: str) -> str:
    """Draai een git-commando in ``repo`` en geef stdout terug (leeg bij fouten)."""
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        print(f"git niet uitvoerbaar: {exc}", file=sys.stderr)
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def sanitize_identifier(value: str) -> str:
    """Zelfde regel als ``opi.utils.naming._sanitize_for_identifier``."""
    return value.replace("-", "_").lower()


def sanitize_lowercase(value: str) -> str:
    """Zelfde regel als ``opi.utils.naming._sanitize_for_lowercase``."""
    return value.lower()


def service_names(entries: Any) -> list[str]:
    """Haal servicenamen uit een services-blok (lijst van strings of dicts, of een dict)."""
    names: list[str] = []
    if isinstance(entries, dict):
        return [str(key) for key in entries]
    if not isinstance(entries, list):
        return names
    for entry in entries:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            if "reference" in entry and isinstance(entry["reference"], str):
                names.append(entry["reference"])
            elif len(entry) == 1:
                names.append(str(next(iter(entry))))
    return names


def _resources_in(node: Any, found: set[str]) -> None:
    """Verzamel alle ``resource:``-waarden binnen één service-blok (revisies)."""
    if isinstance(node, dict):
        resource = node.get("resource")
        if isinstance(resource, str):
            found.add(resource)
        for value in node.values():
            _resources_in(value, found)
    elif isinstance(node, list):
        for value in node:
            _resources_in(value, found)


def collect_resource_names(deployment: dict[str, Any]) -> dict[str, set[str]]:
    """Concrete resource-namen per service uit een deployment-blok.

    De revisie-administratie noteert de daadwerkelijk aangemaakte resource
    (PVC-naam, database-naam met generatiesuffix). Die is exacter dan wat de
    naamgevingsregels afleiden, dus we houden hem per service apart - anders
    belandt een databasenaam in de PVC-lijst.
    """
    per_service: dict[str, set[str]] = {}

    def walk(services: Any) -> None:
        if isinstance(services, dict):
            items = services.items()
        elif isinstance(services, list):
            items = []
            for entry in services:
                if isinstance(entry, dict):
                    name = entry.get("reference") if "reference" in entry else next(iter(entry), None)
                    if isinstance(name, str):
                        items.append((name, entry))
        else:
            return
        for name, block in items:
            found: set[str] = set()
            _resources_in(block, found)
            if found:
                per_service.setdefault(str(name), set()).update(found)

    walk(deployment.get("services"))
    for component in deployment.get("components") or []:
        if isinstance(component, dict):
            walk(component.get("services"))
    return per_service


def deployment_generation(deployment: dict[str, Any]) -> int | None:
    """Zoek de database-generatie (clone/restore ``_vN``) in een deployment-blok."""
    for entry in deployment.get("services") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("reference") if "reference" in entry else next(iter(entry), None)
        if name not in ("postgresql-database", "namespace-postgresql-database"):
            continue
        config = entry.get("config") if "reference" in entry else entry.get(name)
        if isinstance(config, dict):
            generation = config.get("generation")
            if isinstance(generation, int):
                return generation
        generation = entry.get("generation")
        if isinstance(generation, int):
            return generation
    return None


@dataclass
class OrphanDeployment:
    project: str
    deployment: str
    manifest_dir: str | None
    argo_app: str | None
    argo_app_name: str | None
    namespace: str | None
    components: list[str] = field(default_factory=list)
    services_from_manifests: list[str] = field(default_factory=list)
    services_from_history: list[str] = field(default_factory=list)
    manifest_files: int = 0
    last_manifest_commit: dict[str, str] | None = None
    removed_from_project: dict[str, str] | None = None
    generation: int | None = None
    pvc_names: list[str] = field(default_factory=list)
    resources_by_service: dict[str, list[str]] = field(default_factory=dict)

    @property
    def services(self) -> list[str]:
        return sorted(set(self.services_from_manifests) | set(self.services_from_history))

    def resources_to_check(self) -> dict[str, list[str]]:
        """Resource-namen die volgen uit de gebruikte services, volgens OPI's naamgeving."""
        project_id = sanitize_identifier(self.project)
        deployment_id = sanitize_identifier(self.deployment)
        project_low = sanitize_lowercase(self.project)
        deployment_low = sanitize_lowercase(self.deployment)
        services = set(self.services)
        checks: dict[str, list[str]] = {}

        if "postgresql-database" in services:
            recorded = self.resources_by_service.get("postgresql-database", [])
            databases = recorded or [
                f"{project_id}_{deployment_id}_v{self.generation}"
                if self.generation
                else f"{project_id}_{deployment_id}"
            ]
            checks["postgresql (rig-db)"] = [f"database {name}" for name in databases] + [
                f"gebruiker {project_id}_{deployment_id}"
            ]
        if "namespace-postgresql-database" in services:
            checks["postgresql (in namespace)"] = [f"CNPG-cluster in namespace {self.namespace or '?'}"]
        if "keycloak" in services or "authorization-wall" in services:
            clients = [f"{project_low}-{deployment_low}"]
            clients += [f"{project_low}-{deployment_low}-{sanitize_lowercase(c)}" for c in self.components]
            checks["keycloak"] = [f"realm {project_low}-{DEFAULT_CLUSTER}"] + [f"client {c}" for c in clients]
        if "minio-storage" in services:
            bucket = f"{project_low}-{deployment_low}"
            user = f"{project_id}_{deployment_id}"
            checks["minio"] = [f"bucket {bucket}", f"gebruiker {user}", f"policy {user}-{bucket}-policy"]
        if "persistent-storage" in services:
            pvcs = sorted(set(self.pvc_names) | set(self.resources_by_service.get("persistent-storage", [])))
            checks["pvc"] = [f"{name} in namespace {self.namespace or '?'}" for name in pvcs] or [
                f"PVC's in namespace {self.namespace or '?'} (namen niet af te leiden)"
            ]
        if "publish-on-web" in services:
            checks["dns/certificaat"] = ["ingress-hostnames + Let's Encrypt-certificaat controleren"]
        return checks


def load_yaml(path: str) -> dict[str, Any] | None:
    try:
        with open(path) as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def read_namespace(manifest_dir: str) -> str | None:
    kustomization = load_yaml(os.path.join(manifest_dir, "kustomization.yaml"))
    if kustomization:
        namespace = kustomization.get("namespace")
        if isinstance(namespace, str):
            return namespace
    return None


def scan_manifest_dir(manifest_dir: str) -> tuple[list[str], list[str], list[str], int]:
    """Geef (services, componenten, pvc-namen, bestandsaantal) op basis van de bestanden."""
    services: set[str] = set()
    components: set[str] = set()
    pvcs: set[str] = set()
    count = 0

    for root, _, files in os.walk(manifest_dir):
        for name in files:
            count += 1
            for marker, service in MANIFEST_MARKERS:
                if name.endswith(marker):
                    services.add(service)
                    break
            if name.endswith("-deployment.yaml"):
                components.add(name[: -len("-deployment.yaml")])
            if name.endswith("-pvc.yaml") or name.endswith("-pvc.marked-for-deletion.yaml"):
                data = load_yaml(os.path.join(root, name))
                metadata = data.get("metadata") if data else None
                if isinstance(metadata, dict) and isinstance(metadata.get("name"), str):
                    pvcs.add(metadata["name"])

    return sorted(services), sorted(components), sorted(pvcs), count


def last_commit(repo: str, path: str) -> dict[str, str] | None:
    output = run_git(repo, "log", "-1", "--format=%h%x09%ad%x09%s", "--date=short", "--", path)
    if not output.strip():
        return None
    sha, date, subject = output.strip().split("\t", 2)
    return {"sha": sha, "date": date, "subject": subject}


def find_deployment_in_history(
    repo: str, project: str, deployment: str, max_candidates: int = 25
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, str] | None]:
    """Zoek de laatste versie van het projectbestand waarin ``deployment`` nog stond.

    Geeft (projectdata, deployment-blok, verwijder-commit) terug. De verwijder-commit
    is de commit waarin de deployment uit het bestand verdween.
    """
    rel_path = f"projects/{project}.yaml"
    output = run_git(
        repo, "log", "--format=%H%x09%ad%x09%s", "--date=short", "-S", f"- name: {deployment}", "--", rel_path
    )
    if not output.strip():
        return None, None, None

    for line in output.strip().splitlines()[:max_candidates]:
        sha, date, subject = line.split("\t", 2)
        blob = run_git(repo, "show", f"{sha}^:{rel_path}")
        if not blob:
            continue
        try:
            data = yaml.safe_load(blob)
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        for entry in data.get("deployments") or []:
            if isinstance(entry, dict) and entry.get("name") == deployment:
                return data, entry, {"sha": sha[:9], "date": date, "subject": subject}
    return None, None, None


def services_for_deployment(project_data: dict[str, Any], deployment: dict[str, Any]) -> list[str]:
    """Services die deze deployment gebruikte: catalogus-componenten + deployment-blok."""
    catalog = {
        component.get("name"): component
        for component in project_data.get("components") or []
        if isinstance(component, dict) and isinstance(component.get("name"), str)
    }
    names: set[str] = set(service_names(deployment.get("services")))
    for entry in deployment.get("components") or []:
        if not isinstance(entry, dict):
            continue
        names.update(service_names(entry.get("services")))
        reference = entry.get("reference") or entry.get("name")
        component = catalog.get(reference)
        if isinstance(component, dict):
            names.update(service_names(component.get("services")))
    # Project-brede services tellen alleen mee als geen enkel component ze noemt;
    # ze staan in het projectbestand als aanbod, niet als gebruik.
    return sorted(names)


def project_deployments(projects_repo: str, project: str, cluster: str) -> set[str] | None:
    """Live deploymentnamen voor een project, of None als het projectbestand ontbreekt."""
    path = os.path.join(projects_repo, "projects", f"{project}.yaml")
    if not os.path.isfile(path):
        return None
    data = load_yaml(path)
    if data is None:
        return set()
    names: set[str] = set()
    for deployment in data.get("deployments") or []:
        if not isinstance(deployment, dict):
            continue
        name = deployment.get("name")
        if not isinstance(name, str):
            continue
        deployment_cluster = deployment.get("cluster")
        if deployment_cluster and deployment_cluster != cluster:
            continue
        names.add(name)
    return names


def argo_applications(argo_repo: str, cluster: str) -> dict[tuple[str, str], dict[str, str]]:
    """Index alle ArgoCD Applications van een cluster op (project, deployment).

    De mapnaam in de argo-repo is niet betrouwbaar (``algor-odc/infrastructure``
    krijgt bijvoorbeeld een eigen map ``algor-odc-infrastructure``). ``spec.source.path``
    wijst wél altijd naar ``./<cluster>/<project>/<deployment>`` in de deployments-repo,
    dus dat is de koppeling die we volgen.
    """
    index: dict[tuple[str, str], dict[str, str]] = {}
    cluster_dir = os.path.join(argo_repo, cluster)
    if not os.path.isdir(cluster_dir):
        return index

    for root, _, files in os.walk(cluster_dir):
        for name in sorted(files):
            if not name.endswith("-argocd-application.yaml"):
                continue
            full_path = os.path.join(root, name)
            data = load_yaml(full_path)
            if not data:
                continue
            source = (data.get("spec") or {}).get("source") or {}
            path = source.get("path")
            if not isinstance(path, str):
                continue
            parts = path.strip("./").split("/")
            if len(parts) < 3 or parts[0] != cluster:
                continue
            project, deployment = parts[1], parts[2]
            destination = (data.get("spec") or {}).get("destination") or {}
            index[(project, deployment)] = {
                "file": os.path.relpath(full_path, argo_repo),
                "app": str((data.get("metadata") or {}).get("name", "")),
                "namespace": str(destination.get("namespace", "")),
            }
    return index


def analyse(args: argparse.Namespace) -> dict[str, Any]:
    cluster_dir = os.path.join(args.deployments_repo, args.cluster)
    if not os.path.isdir(cluster_dir):
        sys.exit(f"Geen clustermap gevonden: {cluster_dir}")

    report: dict[str, Any] = {
        "cluster": args.cluster,
        "deployments_repo": args.deployments_repo,
        "projects_repo": args.projects_repo,
        "argo_repo": args.argo_repo if os.path.isdir(args.argo_repo) else None,
        "projects": [],
    }

    manifest_projects = sorted(
        name for name in os.listdir(cluster_dir) if os.path.isdir(os.path.join(cluster_dir, name))
    )
    argo_index = argo_applications(args.argo_repo, args.cluster) if report["argo_repo"] else {}
    argo_projects = {project for project, _ in argo_index}

    for project in sorted(set(manifest_projects) | argo_projects):
        if args.project and project != args.project:
            continue

        live = project_deployments(args.projects_repo, project, args.cluster)
        project_dir = os.path.join(cluster_dir, project)
        manifest_dirs = (
            sorted(name for name in os.listdir(project_dir) if os.path.isdir(os.path.join(project_dir, name)))
            if os.path.isdir(project_dir)
            else []
        )
        apps = {deployment: info for (app_project, deployment), info in argo_index.items() if app_project == project}

        entry: dict[str, Any] = {
            "project": project,
            "project_file_present": live is not None,
            "live_deployments": sorted(live) if live is not None else [],
            "orphans": [],
            "not_rolled_out": [],
        }

        if live is None:
            entry["project_file_removed"] = last_commit_of_removed_project(args.projects_repo, project)

        live_set = live or set()
        if live is not None:
            live_set = live_set | PLATFORM_DEPLOYMENTS
        for deployment in sorted(set(manifest_dirs) | set(apps)):
            if deployment in live_set:
                continue
            entry["orphans"].append(build_orphan(args, project, deployment, project_dir, manifest_dirs, apps))

        for deployment in sorted(live or set()):
            if deployment not in manifest_dirs:
                entry["not_rolled_out"].append(deployment)

        if entry["orphans"] or entry["not_rolled_out"] or live is None:
            report["projects"].append(entry)

    return report


def last_commit_of_removed_project(projects_repo: str, project: str) -> dict[str, str] | None:
    output = run_git(
        projects_repo,
        "log",
        "-1",
        "--diff-filter=D",
        "--format=%h%x09%ad%x09%s",
        "--date=short",
        "--",
        f"projects/{project}.yaml",
    )
    if not output.strip():
        return None
    sha, date, subject = output.strip().split("\t", 2)
    return {"sha": sha, "date": date, "subject": subject}


def build_orphan(
    args: argparse.Namespace,
    project: str,
    deployment: str,
    project_dir: str,
    manifest_dirs: list[str],
    apps: dict[str, dict[str, str]],
) -> dict[str, Any]:
    manifest_dir = os.path.join(project_dir, deployment) if deployment in manifest_dirs else None
    app = apps.get(deployment)
    orphan = OrphanDeployment(
        project=project,
        deployment=deployment,
        manifest_dir=os.path.relpath(manifest_dir, args.deployments_repo) if manifest_dir else None,
        argo_app=app["file"] if app else None,
        argo_app_name=app["app"] if app else None,
        namespace=read_namespace(manifest_dir) if manifest_dir else (app or {}).get("namespace") or None,
    )

    if manifest_dir:
        services, components, pvcs, count = scan_manifest_dir(manifest_dir)
        orphan.services_from_manifests = services
        orphan.components = components
        orphan.pvc_names = pvcs
        orphan.manifest_files = count
        orphan.last_manifest_commit = last_commit(args.deployments_repo, manifest_dir)

    if args.history:
        project_data, deployment_block, removed = find_deployment_in_history(args.projects_repo, project, deployment)
        if project_data and deployment_block:
            orphan.services_from_history = services_for_deployment(project_data, deployment_block)
            orphan.removed_from_project = removed
            orphan.generation = deployment_generation(deployment_block)
            if not orphan.namespace and isinstance(deployment_block.get("namespace"), str):
                orphan.namespace = deployment_block["namespace"]
            orphan.resources_by_service = {
                service: sorted(names) for service, names in collect_resource_names(deployment_block).items()
            }
            if not orphan.components:
                orphan.components = [
                    str(component.get("reference") or component.get("name"))
                    for component in deployment_block.get("components") or []
                    if isinstance(component, dict) and (component.get("reference") or component.get("name"))
                ]

    result = {
        "deployment": orphan.deployment,
        "manifest_dir": orphan.manifest_dir,
        "argo_app": orphan.argo_app,
        "argo_app_name": orphan.argo_app_name,
        "namespace": orphan.namespace,
        "components": orphan.components,
        "manifest_files": orphan.manifest_files,
        "services": orphan.services,
        "services_from_manifests": orphan.services_from_manifests,
        "services_from_history": orphan.services_from_history,
        "external_services": sorted(set(orphan.services) & EXTERNAL_SERVICES),
        "last_manifest_commit": orphan.last_manifest_commit,
        "removed_from_project": orphan.removed_from_project,
        "database_generation": orphan.generation,
        "recorded_resources": orphan.resources_by_service,
        "resources_to_check": orphan.resources_to_check(),
    }
    return result


def print_report(report: dict[str, Any]) -> None:
    total_orphans = sum(len(p["orphans"]) for p in report["projects"])
    total_projects = sum(1 for p in report["projects"] if p["orphans"])
    print(f"Cluster: {report['cluster']}")
    print(f"Deployments-repo: {report['deployments_repo']}")
    print(f"Projecten-repo:   {report['projects_repo']}")
    print(f"Argo-repo:        {report['argo_repo'] or '(niet gevonden, overgeslagen)'}")
    print()
    print(f"{total_orphans} wees-deployments in {total_projects} projecten")
    print("=" * 78)

    for project in report["projects"]:
        if not project["orphans"] and not project["not_rolled_out"] and project["project_file_present"]:
            continue
        print()
        header = project["project"]
        if not project["project_file_present"]:
            removed = project.get("project_file_removed")
            note = (
                f" (projectbestand verwijderd: {removed['date']} {removed['sha']})"
                if removed
                else " (GEEN projectbestand)"
            )
            header += note
        print(header)
        print("-" * len(header))
        if project["project_file_present"]:
            print(f"  live deployments: {', '.join(project['live_deployments']) or '(geen)'}")

        for orphan in project["orphans"]:
            where = []
            if orphan["manifest_dir"]:
                where.append(f"manifesten ({orphan['manifest_files']} bestanden)")
            if orphan["argo_app"]:
                where.append("argo-app")
            print(f"\n  * {orphan['deployment']}  [{' + '.join(where)}]")
            if orphan["namespace"]:
                print(f"      namespace:    {orphan['namespace']}")
            if orphan["components"]:
                print(f"      componenten:  {', '.join(orphan['components'])}")
            print(f"      services:     {', '.join(orphan['services']) or '(geen herkend)'}")
            if orphan["last_manifest_commit"]:
                commit = orphan["last_manifest_commit"]
                print(f"      laatste wijziging manifesten: {commit['date']} {commit['sha']} {commit['subject']}")
            if orphan["removed_from_project"]:
                removed = orphan["removed_from_project"]
                print(f"      uit projectbestand:           {removed['date']} {removed['sha']} {removed['subject']}")
            else:
                print("      uit projectbestand:           niet teruggevonden in de git-historie")
            checks = orphan["resources_to_check"]
            if checks:
                print("      te controleren:")
                for kind, items in checks.items():
                    for item in items:
                        print(f"        - [{kind}] {item}")

        if project["not_rolled_out"]:
            print(
                f"\n  niet uitgerold (wel in projectbestand, geen manifesten): {', '.join(project['not_rolled_out'])}"
            )

    print()
    print("=" * 78)
    summary: dict[str, int] = {}
    for project in report["projects"]:
        for orphan in project["orphans"]:
            for service in orphan["external_services"]:
                summary[service] = summary.get(service, 0) + 1
    if summary:
        print("Wezen per service met resources buiten de deployment-namespace:")
        for service, count in sorted(summary.items(), key=lambda item: -item[1]):
            print(f"  {service}: {count}")


def prune(report: dict[str, Any], deployments_repo: str) -> None:
    """Verwijder de manifestmappen van wezen die geen ArgoCD Application meer hebben.

    Een wees mét Application blijft staan: ArgoCD's resources-finalizer heeft het
    source-pad nodig om te bepalen welke K8s-resources hij moet opruimen, en de
    manifesten weghalen terwijl de finalizer nog loopt geeft een deadlock (zelfde
    reden waarom ``delete_deployment`` stap 7 zichzelf dan overslaat).

    Commit en push blijven aan de gebruiker: dit raakt de GitOps-waarheid.
    """
    removed: list[str] = []
    skipped: list[str] = []

    for project in report["projects"]:
        for orphan in project["orphans"]:
            if not orphan["manifest_dir"]:
                continue
            if orphan["argo_app"]:
                skipped.append(f"{project['project']}/{orphan['deployment']} (heeft nog een ArgoCD Application)")
                continue
            full_path = os.path.join(deployments_repo, orphan["manifest_dir"])
            if not os.path.isdir(full_path):
                continue
            shutil.rmtree(full_path)
            removed.append(orphan["manifest_dir"])

    print()
    print("=" * 78)
    print(f"{len(removed)} manifestmappen verwijderd uit de werkkopie:")
    for path in removed:
        print(f"  {path}")
    if skipped:
        print(f"\n{len(skipped)} overgeslagen:")
        for item in skipped:
            print(f"  {item}")
    if removed:
        print("\nNog te doen (bewust niet automatisch):")
        print(f"  git -C {deployments_repo} add -A && git -C {deployments_repo} commit && git push")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--deployments-repo", default=DEFAULT_DEPLOYMENTS_REPO)
    parser.add_argument("--projects-repo", default=DEFAULT_PROJECTS_REPO)
    parser.add_argument("--argo-repo", default=DEFAULT_ARGO_REPO)
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    parser.add_argument("--project", help="beperk het rapport tot één project")
    parser.add_argument("--json", dest="json_path", help="schrijf het volledige rapport als JSON naar dit pad")
    parser.add_argument(
        "--no-history",
        dest="history",
        action="store_false",
        help="sla de git-archeologie in de projecten-repo over (sneller, minder detail)",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="verwijder de manifestmappen van wezen zonder ArgoCD Application (commit/push blijft handwerk)",
    )
    args = parser.parse_args()

    report = analyse(args)
    print_report(report)

    if args.json_path:
        with open(args.json_path, "w") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        print(f"\nJSON geschreven naar {args.json_path}")

    if args.prune:
        prune(report, args.deployments_repo)


if __name__ == "__main__":
    main()
