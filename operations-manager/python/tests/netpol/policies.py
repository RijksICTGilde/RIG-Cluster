"""De NetworkPolicies ophalen zoals ze in git staan, met de namespace waarin ze landen.

Twee bronnen. De tenant-policies komen uit de Jinja-templates en worden met dezelfde
variabelen gerenderd als `project_manager` dat doet, zodat de matrix de echte policy
toetst en niet een nabootsing. De platform-policies zijn platte YAML in de
odcn-overlays; die worden gelezen en van de namespace voorzien die het deploypad ze
geeft.

Die namespace is niet altijd wat er in het bestand of de overlay staat: de ArgoCD-CMP
injecteert de destination-namespace over de hele build van
`infrastructure/bootstrap/clusters/odcn`, dus een overlay die zelf `rig-system` zegt
landt op productie in `rig-prd-operations`. Zie de kop van
`argocd-application-ron-infrastructure.yaml`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from opi.core.cluster_config import (
    get_backup_namespace,
    get_infrastructure_namespace,
    get_ingress_controller_selector,
    get_namespace,
)
from opi.generation.manifests import render_template
from opi.utils.naming import generate_network_policy_name
from ruamel.yaml import YAML

CLUSTER = "odcn-production"
REPO_ROOT = Path(__file__).resolve().parents[4]

OPS_NAMESPACE = get_namespace(CLUSTER)
BACKUP_NAMESPACE = get_backup_namespace(CLUSTER)
MAIL_NAMESPACE = "rig-prd-ron"
TEMPLATE_DIRECTORY = REPO_ROOT / "operations-manager" / "python" / "manifests"
TOP_LEVEL_NETWORK_POLICY_KIND = re.compile(r"^kind: NetworkPolicy\s*$", re.MULTILINE)

# Elk bestand met een NetworkPolicy, en de namespace waarin productie hem krijgt.
# None betekent: staat in git, maar geen uitrolpad raakt hem op productie. De test
# in test_policy_sources.py faalt zodra er een bestand bijkomt dat hier niet staat,
# zodat een nieuwe policy niet stil buiten de matrix valt.
PLATFORM_POLICY_SOURCES: dict[str, str | None] = {
    # Via de production-infrastructure Application; de CMP zet de namespace.
    "infrastructure/bootstrap/infrastructure/postgresql/database/overlays/odcn/network-policies/postgresql-networkpolicy.yaml": OPS_NAMESPACE,
    "infrastructure/bootstrap/infrastructure/minio/config/overlays/odcn/network-policies/minio-networkpolicy.yaml": OPS_NAMESPACE,
    "infrastructure/bootstrap/infrastructure/redis/controller/overlays/odcn/network-policies/redis-networkpolicy.yaml": OPS_NAMESPACE,
    # Eigen Application met destination rig-prd-ron.
    "infrastructure/bootstrap/infrastructure/mail/controller/overlays/odcn/network-policies/mail-relay-networkpolicy.yaml": MAIL_NAMESPACE,
    # De rig-system bootstrap-overlay, die zelf namespace: rig-prd-operations zet.
    "bootstrap/rig-system/kustomize/overlays/odcn-production/network-policies/argocd-network-policy.yaml": OPS_NAMESPACE,
    "bootstrap/rig-system/kustomize/overlays/odcn-production/network-policies/emergency-restore-allow-all.yaml": OPS_NAMESPACE,
    "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/network-policy.yaml": OPS_NAMESPACE,
    # In clusters/odcn uitgecommentarieerd, dus niet op productie.
    "infrastructure/bootstrap/infrastructure/vault/config/overlays/odcn/network-policies/vault-networkpolicy.yaml": None,
    "infrastructure/bootstrap/infrastructure/pgadmin/config/overlays/odcn/network-policies/pgadmin-networkpolicy.yaml": None,
    # Alleen de sandbox-overlay van de mailrelay.
    "infrastructure/bootstrap/infrastructure/mail/controller/overlays/sandboxed-local/network-policies/mail-relay-networkpolicy.yaml": None,
    # Buiten ArgoCD om: task bootstrap-backup-destination (CLUSTER default odcn) bouwt
    # en applied deze overlay, prune-safe voor de backup-PVC.
    "infrastructure/bootstrap/infrastructure/backup-destination/controller/overlays/odcn/network-policies/minio-networkpolicy.yaml": BACKUP_NAMESPACE,
    # Staging overlays: None omdat ze op dezelfde cluster draaien maar niet in de
    # production matrix hoorden. Zie features/restrictive-network-policies.md voor
    # de volle toelichting.
    "infrastructure/bootstrap/infrastructure/postgresql/database/overlays/odcn-staging/network-policies/postgresql-networkpolicy.yaml": None,
    "infrastructure/bootstrap/infrastructure/minio/config/overlays/odcn-staging/network-policies/minio-networkpolicy.yaml": None,
    "infrastructure/bootstrap/infrastructure/redis/controller/overlays/odcn-staging/network-policies/redis-networkpolicy.yaml": None,
    "infrastructure/bootstrap/infrastructure/mail/controller/overlays/odcn-staging/network-policies/mail-relay-networkpolicy.yaml": None,
    "infrastructure/bootstrap/infrastructure/mail/controller/overlays/odcn-staging/network-policies/mail-sink-networkpolicy.yaml": None,
    "infrastructure/bootstrap/infrastructure/backup-destination/controller/overlays/odcn-staging/network-policies/minio-networkpolicy.yaml": None,
    "bootstrap/rig-system/kustomize/overlays/odcn-staging/network-policies/argocd-network-policy.yaml": None,
    "bootstrap/rig-system/kustomize/overlays/odcn-staging/network-policies/emergency-restore-allow-all.yaml": None,
    "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-staging/network-policy.yaml": None,
}

# Elke netpol-template, en of de matrix hem rendert. De grendel in test_policy_sources.py
# faalt zodra er een template bijkomt die hier niet staat.
TEMPLATE_SOURCES: dict[str, bool] = {
    # Geldt voor elk deployment, dus odcn_policies() rendert hem.
    "tenant-baseline-network-policy.yaml.jinja": True,
    # De acme-http-policy: podSelector {} op de hele tenant-namespace, poorten 80 en
    # 8089 voor de HTTP-01-solver. Alle drie de productiedomeinen dragen issuer
    # letsencrypt, dus elk gepubliceerd deployment krijgt hem; odcn_policies() rendert
    # hem daarom mee.
    "network-policy.yaml.jinja": True,
    # Alleen voor de services cross-domain-access, send-email en vlam, elk met hun eigen
    # config. Buiten de matrix: de inhoud volgt uit het projectbestand.
    "service-network-policy.yaml.jinja": False,
}


def _load_documents(text: str) -> list[dict[str, Any]]:
    return [doc for doc in YAML().load_all(text) if doc]


def platform_policies() -> list[dict[str, Any]]:
    """De platform-policies die op productie staan, met hun namespace gezet."""
    policies: list[dict[str, Any]] = []
    for relative_path, namespace in PLATFORM_POLICY_SOURCES.items():
        if namespace is None:
            continue
        for doc in _load_documents((REPO_ROOT / relative_path).read_text()):
            if doc.get("kind") != "NetworkPolicy":
                continue
            doc.setdefault("metadata", {})["namespace"] = namespace
            policies.append(doc)
    return policies


def tenant_baseline(
    *,
    namespace: str,
    deployment_name: str,
    project_name: str = "demo",
) -> dict[str, Any]:
    """De tenant-baseline voor een deployment, met de variabelen van project_manager."""
    rendered = render_template(
        "tenant-baseline-network-policy.yaml.jinja",
        {
            "name": generate_network_policy_name(f"{deployment_name}-tenant-baseline"),
            "namespace": namespace,
            "deployment_selector": deployment_name,
            "ops_namespace": OPS_NAMESPACE,
            "backup_namespace": BACKUP_NAMESPACE,
            "ingress_controller_selector": get_ingress_controller_selector(CLUSTER),
            "database_operator_namespace": None,
            "project_infra_namespace": get_infrastructure_namespace(CLUSTER, project_name),
        },
    )
    return _load_documents(rendered)[0]


def acme_http(
    *,
    namespace: str,
    deployment_name: str,
) -> dict[str, Any]:
    """De acme-http-policy voor een deployment, met de variabelen van project_manager."""
    rendered = render_template(
        "network-policy.yaml.jinja",
        {
            "name": generate_network_policy_name("acme-http", deployment_name),
            "namespace": namespace,
            "pod_selector": None,
            "ports": [80, 8089],
        },
    )
    return _load_documents(rendered)[0]


def declares_network_policy(text: str) -> bool:
    """Of een YAML-tekst zelf een NetworkPolicy is, niet alleen de soort noemt.

    Een tekstmatch volstaat niet: de AppProjects noemen kind: NetworkPolicy in hun
    lijst met toegestane soorten, en die zijn geen policy.
    """
    return any(doc.get("kind") == "NetworkPolicy" for doc in _load_documents(text))


def discovered_policy_files() -> set[str]:
    """Elk bestand in de repo dat een NetworkPolicy declareert, als repo-pad."""
    found: set[str] = set()
    for directory in ("infrastructure", "bootstrap"):
        for path in (REPO_ROOT / directory).rglob("*.yaml"):
            text = path.read_text()
            if "kind: NetworkPolicy" in text and declares_network_policy(text):
                found.add(str(path.relative_to(REPO_ROOT)))
    return found


def discovered_template_files() -> set[str]:
    """Elke Jinja-template die een NetworkPolicy declareert, als bestandsnaam.

    Een template is geen geldige YAML, dus hier toetst een top-level kind-regel in
    plaats van de geparste soort.
    """
    return {
        path.name
        for path in TEMPLATE_DIRECTORY.rglob("*.jinja")
        if TOP_LEVEL_NETWORK_POLICY_KIND.search(path.read_text())
    }
