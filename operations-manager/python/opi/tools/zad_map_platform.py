"""Generate the platform data for the map out of the repo.

``opi/services/zad_map/platform.json`` describes per system what it is, which cluster it
runs in, which OPI connector talks to it, which catalog services lean on it and where to
read further. The file sits in the repo because the sources are NOT in the image:
``infrastructure/`` and ``features/`` are not copied along (see the Dockerfile).

Sources:
- ``infrastructure/bootstrap/clusters/<cluster>/kustomization.yaml``: what runs per
  cluster; a line with a ``#`` in front counts as off;
- ``opi/connectors/*.py`` and how often OPI imports them: what ZAD talks to;
- ``opi/services/catalog/*/help.md``: the title of a catalog service;
- ``features/*.md``: further reading per system, by filename prefix.

Usage::

    uv run python -m opi.tools.zad_map_platform           # write platform.json
    uv run python -m opi.tools.zad_map_platform --check   # exit 1 when it is out of date
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OUTPUT = Path(__file__).resolve().parents[1] / "services" / "zad_map" / "platform.json"

#: The clusters a state is recorded for, keyed by their directory name in ``clusters/``.
CLUSTERS = {
    "odcn": "Productie (ODCN)",
    "odcn-staging": "Staging (ODCN)",
    "sandboxed-local": "Sandbox",
}

#: id: (title, group, what it is). The names follow the directories in
#: ``infrastructure/bootstrap/infrastructure``.
SYSTEMS: dict[str, tuple[str, str, str]] = {
    "kubernetes": ("Kubernetes API", "platform", "Het cluster zelf: namespaces, workloads, netwerkbeleid en opslag."),
    "argocd": ("ArgoCD", "gitops", "Rolt uit wat ZAD in de deployments-repo zet, per project een Application."),
    "git": ("Git-repo's", "gitops", "De drie repo's die ZAD beheert: projects, argo-applications en deployments."),
    "forgejo": ("Forgejo", "gitops", "Eigen Git-server in de sandbox; in productie staan de repo's op GitHub."),
    "keycloak": ("Keycloak", "identity", "Inloggen: een realm per project, en het platform-realm voor ZAD zelf."),
    "postgresql": ("PostgreSQL", "data", "De gedeelde databaseserver voor projecten en voor ZAD zelf."),
    "redis": ("Redis", "data", "Gedeelde cache voor projecten."),
    "minio": ("MinIO", "data", "Objectopslag (S3) voor projecten en backups."),
    "seaweedfs": ("SeaweedFS", "data", "Objectopslag (S3), opvolger-kandidaat naast MinIO."),
    "backup-destination": ("Backups (Kopia)", "data", "Kopia-repository waar backups van volumes en databases landen."),
    "mail": ("Mailrelay", "link", "Uitgaande mail voor projecten en Keycloak."),
    "external-dns": ("External DNS", "network", "Zet DNS-records voor eigen domeinen."),
    "transip": ("TransIP", "network", "DNS-provider achter de rijksapp-domeinen."),
    "chisel": ("Chisel", "network", "Tunnel naar diensten buiten het cluster."),
    "registries": ("Container registries", "platform", "Images ophalen en controleren (Skopeo), ook eigen registries."),
    "registry": ("Lokale registry", "platform", "Eigen container registry in de sandbox."),
    "prometheus": ("Prometheus / Mimir", "ops", "Metrics: resource tuning, gezondheid en dashboards."),
    "vpa": ("VPA", "ops", "Vertical Pod Autoscaler-aanbevelingen voor resource tuning."),
    "secrets": ("Secrets (SOPS)", "platform", "Platformgeheimen, versleuteld met SOPS en AGE."),
    "vault": ("Vault", "platform", "Geheimenbeheer; staat in productie uit."),
    "pgadmin": ("pgAdmin", "ops", "Beheer-UI voor PostgreSQL; staat in productie uit."),
    "falco": ("Falco", "ops", "Runtime-detectie van verdacht gedrag in containers."),
    "kyverno": ("Kyverno", "platform", "Admission-beleid op het cluster."),
}

#: The layer in the picture. ``odcn`` is the foundation the cluster provides (in the
#: sandbox: Kind with our own additions); ``zad`` is what ZAD runs itself next to the
#: Operations Manager.
LAYER = {
    "git": "gitops",
    "forgejo": "gitops",
    "argocd": "gitops",
    "keycloak": "services",
    "postgresql": "services",
    "redis": "services",
    "minio": "services",
    "seaweedfs": "services",
    "backup-destination": "services",
    "mail": "services",
    "external-dns": "network",
    "transip": "network",
    "chisel": "network",
    "secrets": "zad",
    "vault": "zad",
    "pgadmin": "zad",
    "kubernetes": "odcn",
    "vpa": "odcn",
    "kyverno": "odcn",
    "falco": "odcn",
    "prometheus": "odcn",
    "registries": "odcn",
    "registry": "odcn",
}

#: HAND-MAINTAINED: which connector belongs to which system. This does not follow from the
#: code: a connector names its system nowhere, and ``subdomain``, ``http`` and ``opi`` are
#: internal.
CONNECTOR_SYSTEM = {
    "kubectl": "kubernetes",
    "argo": "argocd",
    "git": "git",
    "keycloak": "keycloak",
    "postgres": "postgresql",
    "minio_mc": "minio",
    "seaweedfs_shell": "seaweedfs",
    "kopia": "backup-destination",
    "mail": "mail",
    "transip": "transip",
    "chisel_connector": "chisel",
    "skopeo": "registries",
    "prometheus": "prometheus",
    "grafana_prometheus": "prometheus",
    "vpa": "vpa",
}

#: HAND-MAINTAINED: which system a catalog service leans on. This is not data in the code
#: either: the imports in a service package cover only a few services, and a service that
#: works through a manager names its system nowhere.
SERVICE_SYSTEM = {
    "postgresql-database": "postgresql",
    "namespace-postgresql-database": "kubernetes",
    "keycloak": "keycloak",
    "authorization-wall": "keycloak",
    "invite": "keycloak",
    "minio-storage": "minio",
    "seaweedfs-storage": "seaweedfs",
    "redis": "redis",
    "namespace-redis": "kubernetes",
    "send-email": "mail",
    "publish-on-web": "external-dns",
    "metrics-scraper": "prometheus",
    "resource-tuning": "vpa",
    "persistent-storage": "kubernetes",
    "temp-storage": "kubernetes",
    "sleep-mode": "kubernetes",
    "image-registries": "registries",
    "attachments": "kubernetes",
    "cross-domain-access": "kubernetes",
}

#: The directory name in the catalog sometimes differs from the service name in the project file.
CATALOG_DIR = {
    "minio-storage": "minio",
    "seaweedfs-storage": "seaweedfs",
    "namespace-postgresql-database": "namespace_postgres",
}

#: Feature documents per system, by filename prefix.
DOC_PREFIX = {
    "argocd": ("argocd",),
    "keycloak": ("keycloak",),
    "backup-destination": ("backup",),
    "forgejo": ("forgejo",),
    "git": ("git-",),
    "falco": ("falco",),
    "kyverno": ("kyverno",),
    "registries": ("image-registries",),
    "vpa": ("auto-resource-tuning",),
    "postgresql": ("namespace-postgresql", "postgres"),
    "mail": ("send-email", "keycloak-mail", "no-mail"),
    "external-dns": ("external-domains", "domain"),
    "minio": ("minio",),
    "seaweedfs": ("seaweedfs",),
    "prometheus": ("capsule-proxy-prometheus", "prometheus", "metrics"),
    "redis": ("redis", "namespace-redis"),
}

_RESOURCE = re.compile(r"\s*(#)?\s*-\s*\.\./\.\./infrastructure/([a-z-]+)/")


def cluster_status(root: Path) -> dict[str, dict[str, str]]:
    """What each cluster kustomization names: ``aan``, or ``uit`` when the line is commented out."""
    status: dict[str, dict[str, str]] = {}
    for cluster in CLUSTERS:
        text = (root / "infrastructure/bootstrap/clusters" / cluster / "kustomization.yaml").read_text()
        for line in text.splitlines():
            match = _RESOURCE.match(line)
            if not match:
                continue
            name = match.group(2)
            state = "uit" if match.group(1) else "aan"
            if status.setdefault(name, {}).get(cluster) != "aan":
                status[name][cluster] = state
    return status


def state_per_cluster(root: Path, system_id: str, listed: dict[str, str]) -> dict[str, str]:
    """``aan``/``uit`` per the cluster kustomization; ``elders`` when there is an overlay but
    no line; ``afwezig`` when the system does not exist in that cluster; ``cluster`` when we
    have no rollout for it; ``extern`` outside ``infrastructure/``."""
    folder = root / "infrastructure/bootstrap/infrastructure" / system_id
    if not folder.is_dir():
        return dict.fromkeys(CLUSTERS, "extern")
    overlays = {path.name for path in folder.rglob("overlays/*")}
    result = {}
    for cluster in CLUSTERS:
        if not overlays and cluster not in listed:
            # Only policy or config, no rollout of ours: the cluster provides it itself.
            result[cluster] = "cluster"
        elif cluster in listed:
            result[cluster] = listed[cluster]
        elif cluster in overlays:
            result[cluster] = "elders"
        else:
            result[cluster] = "afwezig"
    return result


def connector_usage(opi: Path) -> dict[str, int]:
    """How often OPI imports each connector, outside the connectors themselves."""
    counts: dict[str, int] = {}
    for path in sorted(opi.rglob("*.py")):
        if "connectors" in path.parts and path.parent.name == "connectors":
            continue
        for match in re.finditer(r"from opi\.connectors\.([a-z_]+)", path.read_text(errors="ignore")):
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    return counts


def doc_title(path: Path) -> str:
    for line in path.read_text(errors="ignore").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def build(root: Path) -> dict:
    opi = root / "operations-manager/python/opi"
    status = cluster_status(root)
    usage = connector_usage(opi)
    catalog = opi / "services/catalog"
    features = sorted((root / "features").glob("*.md"))

    systems = []
    for system_id, (title, group, what) in SYSTEMS.items():
        connectors = sorted(name for name, target in CONNECTOR_SYSTEM.items() if target == system_id and name in usage)
        services = []
        for service, target in SERVICE_SYSTEM.items():
            if target != system_id:
                continue
            folder = catalog / CATALOG_DIR.get(service, service.replace("-", "_"))
            help_file = folder / "help.md"
            services.append({"name": service, "title": doc_title(help_file) if help_file.exists() else service})
        prefixes = DOC_PREFIX.get(system_id, ())
        docs = [
            {"file": f"features/{path.name}", "title": doc_title(path)}
            for path in features
            if prefixes and path.name.startswith(prefixes)
        ]
        systems.append(
            {
                "id": system_id,
                "title": title,
                "group": group,
                "layer": LAYER[system_id],
                "what": what,
                "clusters": state_per_cluster(root, system_id, status.get(system_id, {})),
                "connectors": [
                    {"name": name, "file": f"opi/connectors/{name}.py", "uses": usage[name]} for name in connectors
                ],
                "services": services,
                "docs": docs[:8],
            }
        )
    return {"clusters": CLUSTERS, "systems": systems}


def as_text(data: dict) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 als platform.json verouderd is")
    args = parser.parse_args(argv)

    text = as_text(build(ROOT))
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != text:
            print(f"{OUTPUT} loopt achter op de repo. Bijwerken: uv run python -m opi.tools.zad_map_platform")
            return 1
        return 0
    OUTPUT.write_text(text)
    print(f"{len(json.loads(text)['systems'])} systemen geschreven naar {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
