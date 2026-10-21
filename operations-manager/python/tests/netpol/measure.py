"""De flowmatrix echt meten op een cluster, in plaats van hem te berekenen.

De evaluator in `evaluator.py` leest de policies zoals Kubernetes ze bedoelt. Een CNI
kan daar vanaf wijken, en dan is een groene unit test geen uitspraak over het netwerk.
Deze module zet per endpoint uit de matrix een pod neer met de juiste labels in een
namespace met de juiste labels, rolt de policies uit, probeert per regel een
TCP-verbinding, en legt de gemeten uitkomst naast de berekende.

Draaien gaat via `task netpol:meet` (zie Taskfile.yaml); die maakt het wegwerpcluster
en ruimt het op. Deze module gaat ervan uit dat KUBECONFIG naar dat cluster wijst en
raakt geen enkel ander cluster aan.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from io import StringIO
from typing import Any

from ruamel.yaml import YAML
from tests.netpol.evaluator import External, Pod
from tests.netpol.matrix import Flow, evaluate_flow, load_matrix, odcn_policies

# busybox draagt de nc die we als listener en als prober gebruiken.
PROBE_IMAGE = "busybox:1.37"
PROBE_TIMEOUT_SECONDS = 3

# De meting mag alleen op het wegwerpcluster draaien. Niet de gedeelde sandbox
# uitsluiten maar het meetcluster eisen: de endpoints dragen productienamen en
# productielabels, dus op elk ander cluster landen de probe-pods in bestaande
# namespaces en nemen ze het verkeer van een bestaande Service over.
# kind noemt de context van een cluster kind-<naam>.
MEETCLUSTER_CONTEXT = f"kind-{os.environ.get('NETPOL_CLUSTER', 'rig-netpol-meting')}"


def kubectl(*args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=check,
    )


def _pod_name(endpoint_name: str) -> str:
    return f"probe-{endpoint_name}".replace("_", "-").lower()


def _listen_ports(endpoint_name: str, flows: list[Flow]) -> list[int]:
    return sorted({f.poort for f in flows if f.naar == endpoint_name})


def _listener_command(ports: list[int]) -> str:
    """Een nc-listener per poort, en daarna wachten.

    `nc -lk` ontbreekt in busybox, dus elke poort krijgt een lus die na elke
    verbinding opnieuw gaat luisteren. Geen `-e`: die vlag zit niet in elke
    busybox-build, en voor een TCP-handshake is hij niet nodig.
    """
    if not ports:
        return "sleep infinity"
    lussen = " ".join(f"while true; do nc -l -p {p} >/dev/null 2>&1; done &" for p in ports)
    return f"{lussen} sleep infinity"


def apply_namespaces(endpoints: dict[str, Pod | External]) -> set[str]:
    namespaces = {e.namespace: e.namespace_labels for e in endpoints.values() if isinstance(e, Pod)}
    for namespace, labels in namespaces.items():
        manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace, "labels": dict(labels)},
        }
        kubectl("apply", "-f", "-", input_text=json.dumps(manifest))
    return set(namespaces)


def apply_probe_pods(endpoints: dict[str, Pod | External], flows: list[Flow]) -> None:
    for name, endpoint in endpoints.items():
        if not isinstance(endpoint, Pod):
            continue
        ports = _listen_ports(name, flows)
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": _pod_name(name),
                "namespace": endpoint.namespace,
                "labels": dict(endpoint.labels),
            },
            "spec": {
                "containers": [
                    {
                        "name": "probe",
                        "image": PROBE_IMAGE,
                        "command": ["sh", "-c", _listener_command(ports)],
                        "ports": [{"containerPort": p} for p in ports],
                    }
                ],
                "terminationGracePeriodSeconds": 1,
            },
        }
        kubectl("apply", "-f", "-", input_text=json.dumps(manifest))

    for name, endpoint in endpoints.items():
        if isinstance(endpoint, Pod):
            kubectl(
                "wait",
                "--for=condition=Ready",
                f"pod/{_pod_name(name)}",
                "-n",
                endpoint.namespace,
                "--timeout=120s",
            )


def apply_policies(policies: list[dict[str, Any]], namespaces: set[str]) -> list[str]:
    """De policies uitrollen waarvan de namespace in dit cluster bestaat."""
    applied = []
    yaml = YAML()
    yaml.default_flow_style = False
    for policy in policies:
        namespace = policy["metadata"]["namespace"]
        if namespace not in namespaces:
            continue
        stream = StringIO()
        yaml.dump(policy, stream)
        kubectl("apply", "-f", "-", input_text=stream.getvalue())
        applied.append(f"{namespace}/{policy['metadata']['name']}")
    return applied


def _target_address(endpoint: Pod | External, endpoint_name: str) -> str:
    if isinstance(endpoint, External):
        return endpoint.ip
    result = kubectl("get", "pod", _pod_name(endpoint_name), "-n", endpoint.namespace, "-o", "jsonpath={.status.podIP}")
    return result.stdout.strip()


def measure_flow(flow: Flow, endpoints: dict[str, Pod | External]) -> bool:
    """Of de bron het doel echt bereikt. True is open."""
    source = endpoints[flow.van]
    if not isinstance(source, Pod):
        raise TypeError(f"een bron buiten het cluster kan niet meten: {flow.van}")
    address = _target_address(endpoints[flow.naar], flow.naar)
    if not address:
        raise RuntimeError(f"geen adres voor doel {flow.naar}")
    # nc zonder stdin maakt alleen de TCP-handshake en stopt: exitcode 0 is open. Een
    # gedropt pakket loopt in de timeout, precies het gedrag van een weigerende policy.
    # Geen `-z`, die vlag ontbreekt in sommige busybox-builds. busybox nc neemt een
    # IPv6-adres zonder blokhaken.
    result = kubectl(
        "exec",
        _pod_name(flow.van),
        "-n",
        source.namespace,
        "--",
        "sh",
        "-c",
        f"nc -w {PROBE_TIMEOUT_SECONDS} {address} {flow.poort} < /dev/null",
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="de flowmatrix meten op het cluster waar KUBECONFIG naar wijst")
    parser.add_argument("--only", help="alleen regels waarvan de bron of het doel deze naam heeft")
    args = parser.parse_args()

    endpoints, flows = load_matrix()
    if args.only:
        flows = [f for f in flows if args.only in (f.van, f.naar)]

    context = kubectl("config", "current-context", check=False).stdout.strip()
    print(f"cluster: {context}")
    if context != MEETCLUSTER_CONTEXT:
        print(
            f"WEIGERT: de context is {context or '<geen>'} en niet {MEETCLUSTER_CONTEXT}. "
            "Deze meting maakt namespaces en pods met de labels van productie "
            "(app: minio, app: rig-redis, cnpg.io/cluster: rig-db) en rolt policies uit. "
            "Draai `task netpol:meet`, dat maakt het wegwerpcluster met een eigen kubeconfig.",
            file=sys.stderr,
        )
        return 2

    namespaces = apply_namespaces(endpoints)
    apply_probe_pods(endpoints, flows)
    policies = odcn_policies()
    applied = apply_policies(policies, namespaces)
    print(f"{len(applied)} policies uitgerold: {', '.join(applied)}\n")

    afwijkingen = 0
    print(f"{'bron':26s} {'doel':24s} {'poort':>6s}  {'berekend':9s} {'gemeten':9s} uitkomst")
    for flow in flows:
        berekend = "open" if evaluate_flow(flow, endpoints, policies).allowed else "dicht"
        gemeten = "open" if measure_flow(flow, endpoints) else "dicht"
        eens = berekend == gemeten
        if not eens:
            afwijkingen += 1
        print(
            f"{flow.van:26s} {flow.naar:24s} {flow.poort:>6d}  {berekend:9s} {gemeten:9s} {'' if eens else 'WIJKT AF'}"
        )

    print()
    if afwijkingen:
        print(
            f"{afwijkingen} regel(s) wijken af: de evaluator of de matrix klopt niet, deze CNI mist de "
            "constructie, of deze host komt niet bij het doel (de internetregels meten dicht zonder "
            "route naar 93.184.216.34 of zonder IPv6-egress)"
        )
        return 1
    print("de meting en de evaluator zijn het over elke regel eens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
