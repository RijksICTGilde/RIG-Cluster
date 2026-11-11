"""Gedeelde hulp voor de groep ``kubernetes_live``: OPI tegen een echte Kubernetes-API.

``task test-cluster-up`` zet het wegwerpcluster op (kind, met de node-image die de sandbox
pint) en ``task test-kubernetes`` draait de groep ertegen met een eigen KUBECONFIG. Het
terugleeskanaal is een eigen ``kubectl`` met een expliciete ``--context``, los van de
connector onder toets: een returnwaarde van de connector zegt niets over wat het cluster deed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

import pytest
from opi.services.catalog.base import APPLICATION_CONTAINER_NAME
from tests.integration.live import KUBERNETES

#: Alles wat deze groep maakt begint hiermee, en de opruiming gaat op dit voorvoegsel.
PREFIX = "zadlive"

#: De namespaces van de groep. Een tweede namespace, want de vraag is niet alleen wat er
#: staat maar ook of een actie binnen zijn eigen namespace blijft.
NAMESPACE = f"{PREFIX}-one"
OTHER_NAMESPACE = f"{PREFIX}-two"

#: De deploymentnaam die de labels van de werklasten dragen, zoals OPI ze rendert.
DEPLOYMENT = f"{PREFIX}-d1"

#: De image van de werklasten die deze groep neerzet. Gereedschap van de meting, geen dienst
#: die het platform uitrolt; ``tests/test_test_images_follow_pins.py`` zondert hem daarom uit.
WORKLOAD_IMAGE = "busybox:1.37"


def context() -> str:
    """De kubectl-context van het wegwerpcluster, of een skip (in CI een fout)."""
    return KUBERNETES.omgeving("KUBERNETES_LIVE_CONTEXT")


def assert_test_cluster() -> None:
    """Weiger tegen iets anders dan het wegwerpcluster van task test-cluster-up te draaien.

    De hostnaam-grendel van ``LiveGroup`` past hier niet: een cluster is geen host, en de
    API-server van kind staat op een willekeurige poort op localhost. Wat dit cluster
    wegwerpbaar maakt, is dat het een kind-cluster is en dat de context de afgesproken naam
    heeft; deze groep maakt en verwijdert namespaces, dus op een echt cluster is hij
    destructief.
    """
    naam = context()
    if not naam.startswith("kind-"):
        pytest.fail(
            f"KUBERNETES_LIVE_CONTEXT is '{naam}', geen kind-context. Deze groep maakt en "
            "verwijdert namespaces en draait alleen tegen het cluster van task test-cluster-up."
        )
    beschikbaar = Kubectl().ok("config", "get-contexts", "-o", "name").splitlines()
    if naam not in beschikbaar:
        pytest.fail(f"De context '{naam}' staat niet in de KUBECONFIG; draai task test-cluster-up.")


@dataclass(frozen=True)
class Kubectl:
    """Het terugleeskanaal: een eigen kubectl, naast de connector onder toets."""

    def run(self, *args: str, stdin: str | None = None) -> tuple[str, int]:
        done = subprocess.run(
            ["kubectl", "--context", context(), *args],
            capture_output=True,
            text=True,
            input=stdin,
            timeout=120,
            check=False,
        )
        return (done.stdout.strip() or done.stderr.strip()), done.returncode

    def ok(self, *args: str, stdin: str | None = None) -> str:
        output, code = self.run(*args, stdin=stdin)
        assert code == 0, f"kubectl {' '.join(args)}: {output}"
        return output

    def apply(self, manifest: str) -> str:
        return self.ok("apply", "-f", "-", stdin=manifest)

    def get(self, kind: str, name: str, namespace: str | None = None) -> dict[str, Any] | None:
        """Het object zoals het cluster het heeft, of None als het er niet is."""
        args = ["get", kind, name, "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        output, code = self.run(*args)
        if code != 0:
            return None
        return json.loads(output)

    def names(self, kind: str, namespace: str | None = None, selector: str | None = None) -> set[str]:
        args = ["get", kind, "-o", "name"]
        if namespace:
            args += ["-n", namespace]
        if selector:
            args += ["-l", selector]
        output, code = self.run(*args)
        if code != 0:
            return set()
        return {line.split("/", 1)[-1] for line in output.splitlines() if line.strip()}

    def exists(self, kind: str, name: str, namespace: str | None = None) -> bool:
        return self.get(kind, name, namespace) is not None


def namespace_manifest(namespace: str, labels: dict[str, str] | None = None) -> str:
    lines = ["apiVersion: v1", "kind: Namespace", "metadata:", f"  name: {namespace}"]
    if labels:
        lines.append("  labels:")
        lines += [f"    {key}: {value!r}" for key, value in labels.items()]
    return "\n".join(lines) + "\n"


def logging_deployment_manifest(name: str, namespace: str, deployment: str = DEPLOYMENT) -> str:
    """Een deployment in de vorm die OPI rendert, die blijft loggen.

    De labels en de containernaam komen uit ``manifests/deployment.yaml.jinja``: zonder
    ``component=application``, het ``deployment``-label en een container ``app`` ziet
    ``get_application_pods`` deze pods niet, en dan meet die toets niets.
    """
    return f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app: {name}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
        deployment: {deployment}
        component: application
    spec:
      containers:
        - name: {APPLICATION_CONTAINER_NAME}
          image: {WORKLOAD_IMAGE}
          command: ["/bin/sh", "-c"]
          args:
            - 'i=0; while true; do i=$((i+1)); echo "regel $i"; sleep 1; done'
          resources:
            requests:
              memory: 16Mi
              cpu: 10m
            limits:
              memory: 32Mi
"""


def wait_for_deployment(name: str, namespace: str, timeout: int = 180) -> None:
    """Wacht op de conditie zelf: kubectl meldt zodra de deployment beschikbaar is."""
    Kubectl().ok("wait", "--for=condition=Available", f"deployment/{name}", "-n", namespace, f"--timeout={timeout}s")


def drop_by_prefix(prefix: str = PREFIX) -> None:
    """Haal elke namespace met dit voorvoegsel weg, ook die van een afgebroken run.

    De grendel staat hier en niet alleen in de fixture: een toets die geen connector vraagt
    komt daar niet langs, en dan zou alleen de volgorde van de fixtures hem aanroepen.
    """
    assert_test_cluster()
    kubectl = Kubectl()
    namespaces = sorted(name for name in kubectl.names("namespace") if name.startswith(prefix))
    if not namespaces:
        return
    kubectl.run("delete", "namespace", *namespaces, "--wait=false", "--ignore-not-found")
    for namespace in namespaces:
        kubectl.run("wait", "--for=delete", f"namespace/{namespace}", "--timeout=180s")
