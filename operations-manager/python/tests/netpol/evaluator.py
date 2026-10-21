"""Evaluator voor NetworkPolicies: welke verbindingen laten de policies toe.

Volgt Kubernetes-semantiek: een pod zonder selecterende policy is open; zodra één
policy hem selecteert geldt de vereniging van alle regels. Een verbinding moet langs
egress bij de bron en ingress bij het doel.

Dit is een berekening, geen meting op een cluster. Een CNI kan constructies missen;
daarvoor is `measure.py` (`task netpol:meet`).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

# Kubernetes zet dit label zelf op elke namespace, en de policies in deze repo
# selecteren namespaces er bijna allemaal op. De evaluator vult het daarom aan,
# zodat een testregel alleen de labels hoeft te noemen die iemand zelf zette.
NAMESPACE_NAME_LABEL = "kubernetes.io/metadata.name"


@dataclass(frozen=True)
class Pod:
    """Een pod als bron of doel van een verbinding."""

    namespace: str
    labels: dict[str, str] = field(default_factory=dict)
    namespace_labels: dict[str, str] = field(default_factory=dict)

    def all_namespace_labels(self) -> dict[str, str]:
        return {NAMESPACE_NAME_LABEL: self.namespace, **self.namespace_labels}


@dataclass(frozen=True)
class External:
    """Een doel buiten het cluster, geadresseerd op IP (v4 of v6)."""

    ip: str


Endpoint = Pod | External


@dataclass(frozen=True)
class Verdict:
    """De uitkomst, met de policy die het toeliet of de reden dat het dicht is."""

    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


def _matches_expressions(labels: dict[str, str], expressions: list[dict[str, Any]]) -> bool:
    for expression in expressions:
        key = expression["key"]
        operator = expression["operator"]
        values = expression.get("values", [])
        present = key in labels
        if operator == "In":
            if not present or labels[key] not in values:
                return False
        elif operator == "NotIn":
            # Een pod zonder het label matcht ook: er staat geen verboden waarde.
            if present and labels[key] in values:
                return False
        elif operator == "Exists":
            if not present:
                return False
        elif operator == "DoesNotExist":
            if present:
                return False
        else:
            raise ValueError(f"onbekende matchExpressions-operator: {operator!r}")
    return True


def matches_selector(labels: dict[str, str], selector: dict[str, Any] | None) -> bool:
    """Of labels aan een LabelSelector voldoen.

    `None` betekent "geen selector" en wordt door de aanroeper uitgelegd; een leeg
    mapping `{}` selecteert alles.
    """
    if selector is None:
        return False
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    return _matches_expressions(labels, selector.get("matchExpressions") or [])


def _ip_matches_block(ip: str, block: dict[str, Any]) -> bool:
    address = ipaddress.ip_address(ip)
    network = ipaddress.ip_network(block["cidr"], strict=False)
    if address.version != network.version or address not in network:
        return False
    return all(address not in ipaddress.ip_network(excluded, strict=False) for excluded in block.get("except") or [])


def _ipblock_covers_every_pod(block: dict[str, Any]) -> bool:
    """Of een ipBlock elk adres van zijn familie dekt, en daarmee ook elk pod-IP.

    Dit is de valkuil van `0.0.0.0/0`: een CNI legt de CIDR tegen het echte
    bestemmingsadres, dus zo'n regel opent ook bestemmingen binnen het cluster. Wie
    alleen internet bedoelt, zet de pod-CIDR in `except`.
    """
    return ipaddress.ip_network(block["cidr"], strict=False).prefixlen == 0 and not block.get("except")


def _unjudged_peer(peer: dict[str, Any], target: Endpoint) -> str | None:
    """De reden dat deze peer niet tegen dit doel te leggen is, of None."""
    if "ipBlock" not in peer or not isinstance(target, Pod):
        return None
    block = peer["ipBlock"]
    if _ipblock_covers_every_pod(block):
        return None
    return f"ipBlock {block['cidr']} tegen een pod-doel, en dit model kent geen pod-IP"


def _peer_matches(peer: dict[str, Any], target: Endpoint, policy_namespace: str) -> bool:
    if not peer:
        # `- {}` als peer: alles.
        return True

    if "ipBlock" in peer:
        if isinstance(target, External):
            return _ip_matches_block(target.ip, peer["ipBlock"])
        # Een pod heeft in dit model geen IP, en op een dual-stack cluster een adres in
        # beide families: een catch-all dekt die allemaal. Een smaller blok matcht niet,
        # en _unjudged_peer zegt waarom.
        return _ipblock_covers_every_pod(peer["ipBlock"])

    if not isinstance(target, Pod):
        return False

    namespace_selector = peer.get("namespaceSelector")
    pod_selector = peer.get("podSelector")

    if namespace_selector is None:
        # Geen namespaceSelector: alleen de namespace van de policy zelf.
        if target.namespace != policy_namespace:
            return False
    elif not matches_selector(target.all_namespace_labels(), namespace_selector):
        return False

    # podSelector en namespaceSelector in EEN peer betekent AND.
    return pod_selector is None or matches_selector(target.labels, pod_selector)


def _port_matches(rule_ports: list[dict[str, Any]] | None, port: int, protocol: str) -> bool:
    if not rule_ports:
        # Geen ports in de regel: elke poort.
        return True
    for entry in rule_ports:
        if (entry.get("protocol") or "TCP").upper() != protocol.upper():
            continue
        declared = entry.get("port")
        if declared is None:
            return True
        end_port = entry.get("endPort")
        if end_port is not None and isinstance(declared, int):
            if declared <= port <= end_port:
                return True
        elif declared == port:
            return True
    return False


def _selects(policy: dict[str, Any], pod: Pod) -> bool:
    spec = policy.get("spec") or {}
    if policy.get("metadata", {}).get("namespace") != pod.namespace:
        return False
    return matches_selector(pod.labels, spec.get("podSelector") or {})


def _policy_types(policy: dict[str, Any]) -> set[str]:
    """De richtingen die een policy afdwingt.

    Zonder expliciete `policyTypes` leidt Kubernetes ze af: Ingress altijd, Egress
    alleen als er een egress-sectie staat.
    """
    spec = policy.get("spec") or {}
    declared = spec.get("policyTypes")
    if declared:
        return set(declared)
    types = {"Ingress"}
    if "egress" in spec:
        types.add("Egress")
    return types


def _direction_allowed(
    policies: list[dict[str, Any]],
    pod: Pod,
    peer: Endpoint,
    port: int,
    protocol: str,
    *,
    direction: str,
) -> Verdict:
    rule_key = "egress" if direction == "Egress" else "ingress"
    peer_key = "to" if direction == "Egress" else "from"

    selecting = [p for p in policies if _selects(p, pod) and direction in _policy_types(p)]
    if not selecting:
        return Verdict(True, f"geen {direction}-policy selecteert deze pod")

    onbeoordeeld: list[str] = []
    for policy in selecting:
        name = policy.get("metadata", {}).get("name", "<naamloos>")
        for rule in policy["spec"].get(rule_key) or []:
            rule = rule or {}
            if not _port_matches(rule.get("ports"), port, protocol):
                continue
            peers = rule.get(peer_key)
            if not peers:
                # Geen of een lege peer-lijst: elke bron of bestemming. Een lege lijst
                # is in de API hetzelfde als geen lijst, en operations-manager-policy
                # schrijft het zo (`from: []`, `to: []`).
                return Verdict(True, f"{name} ({rule_key}, elke peer)")
            for entry in peers:
                entry = entry or {}
                if _peer_matches(entry, peer, policy["metadata"]["namespace"]):
                    return Verdict(True, f"{name} ({rule_key})")
                reden = _unjudged_peer(entry, peer)
                if reden:
                    onbeoordeeld.append(f"{name}: {reden}")

    names = ", ".join(p.get("metadata", {}).get("name", "<naamloos>") for p in selecting)
    reason = f"geen {rule_key}-regel past, wel geselecteerd door: {names}"
    if onbeoordeeld:
        reason += f"; niet te beoordelen: {'; '.join(dict.fromkeys(onbeoordeeld))}"
    return Verdict(False, reason)


def evaluate(
    policies: list[dict[str, Any]],
    source: Endpoint,
    target: Endpoint,
    port: int,
    protocol: str = "TCP",
) -> Verdict:
    """Of `source` het `target` op deze poort mag bereiken.

    Een verbinding moet langs de egress van de bron en de ingress van het doel. Voor
    een endpoint buiten het cluster bestaat de andere kant niet en vervalt die helft.
    """
    if isinstance(source, Pod):
        egress = _direction_allowed(policies, source, target, port, protocol, direction="Egress")
        if not egress:
            return Verdict(False, f"egress geweigerd: {egress.reason}")
    else:
        egress = Verdict(True, "bron buiten het cluster")

    if isinstance(target, Pod):
        ingress = _direction_allowed(policies, target, source, port, protocol, direction="Ingress")
        if not ingress:
            return Verdict(False, f"ingress geweigerd: {ingress.reason}")
    else:
        ingress = Verdict(True, "doel buiten het cluster")

    return Verdict(True, f"egress: {egress.reason}; ingress: {ingress.reason}")
