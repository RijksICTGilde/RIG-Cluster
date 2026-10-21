"""De flowmatrix inlezen en tegen de policies evalueren.

Gedeeld door de unit test en de gemeten variant op een echt cluster, zodat beide
precies dezelfde regels gebruiken.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from tests.netpol.evaluator import Endpoint, External, Pod, Verdict, evaluate
from tests.netpol.policies import acme_http, platform_policies, tenant_baseline

MATRIX_PATH = Path(__file__).parent / "flows_odcn.yaml"

TENANT_NAMESPACE = "rig-prd-demo"
TENANT_PROJECT = "demo"

# De tijdelijke allow-all over rig-prd-operations. De matrix evalueert ook zonder dit
# bestand, want dat is de toestand waar de uitsluitlijst erin naartoe krimpt.
ALLOW_ALL_NAME = "emergency-restore-allow-all"


@dataclass(frozen=True)
class Flow:
    van: str
    naar: str
    poort: int
    verwacht: str
    waarom: str
    # Weggelaten betekent: gelijk aan `verwacht`. Zo hoeft een regel die niet op de
    # allow-all leunt de kolom niet te herhalen.
    zonder_allow_all: str = ""

    def __post_init__(self) -> None:
        if not self.zonder_allow_all:
            object.__setattr__(self, "zonder_allow_all", self.verwacht)


def load_matrix(path: Path = MATRIX_PATH) -> tuple[dict[str, Endpoint], list[Flow]]:
    document = YAML().load(path.read_text())
    endpoints: dict[str, Endpoint] = {}
    for name, spec in document["endpoints"].items():
        if "ip" in spec:
            endpoints[name] = External(ip=str(spec["ip"]))
        else:
            endpoints[name] = Pod(
                namespace=spec["namespace"],
                labels=dict(spec.get("labels") or {}),
                namespace_labels=dict(spec.get("namespace_labels") or {}),
            )

    flows = []
    for entry in document["flows"]:
        for key in ("verwacht", "zonder_allow_all"):
            waarde = entry.get(key, entry["verwacht"])
            if waarde not in ("open", "dicht"):
                raise ValueError(f"{key} moet open of dicht zijn, niet {waarde!r}")
        for role in ("van", "naar"):
            if entry[role] not in endpoints:
                raise ValueError(f"onbekend endpoint {entry[role]!r} in {role}")
        flows.append(
            Flow(
                van=entry["van"],
                naar=entry["naar"],
                poort=int(entry["poort"]),
                verwacht=entry["verwacht"],
                waarom=entry.get("waarom", ""),
                zonder_allow_all=entry.get("zonder_allow_all", ""),
            )
        )
    return endpoints, flows


def odcn_policies() -> list[dict[str, Any]]:
    """Alle policies die de matrix raakt: platform plus de twee tenant-deployments.

    Elk gepubliceerd deployment krijgt naast de baseline ook de acme-http-policy, omdat
    alle drie de productiedomeinen issuer letsencrypt dragen.
    """
    policies = platform_policies()
    for deployment_name in ("web", "api"):
        policies.append(
            tenant_baseline(
                namespace=TENANT_NAMESPACE,
                deployment_name=deployment_name,
                project_name=TENANT_PROJECT,
            )
        )
        policies.append(acme_http(namespace=TENANT_NAMESPACE, deployment_name=deployment_name))
    return policies


def odcn_policies_zonder_allow_all() -> list[dict[str, Any]]:
    """Dezelfde policies, zonder de tijdelijke allow-all over rig-prd-operations."""
    return [p for p in odcn_policies() if p["metadata"]["name"] != ALLOW_ALL_NAME]


def evaluate_flow(flow: Flow, endpoints: dict[str, Endpoint], policies: list[dict[str, Any]]) -> Verdict:
    return evaluate(policies, endpoints[flow.van], endpoints[flow.naar], flow.poort)
