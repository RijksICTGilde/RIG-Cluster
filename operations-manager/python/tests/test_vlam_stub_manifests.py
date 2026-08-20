"""Wat de sandbox-stub neerzet moet passen op wat de vlam-dienst uitdeelt (RC-144).

De stub in ``tests/e2e/helpers/vlam_stub.py`` bestaat om de plaatshouder-coordinaten van de
sandbox waar te maken. Dat lukt alleen als hij precies de labels en de naam draagt waar de
netwerkregel en het adres van de dienst op selecteren -- en die twee komen uit
``vlam_endpoint()``. Loopt een van beide weg, dan is het gevolg geen foutmelding maar een
time-out in een sandboxrun van een uur, en dat is de duurste manier om dit te ontdekken.

Deze toetsen draaien zonder cluster: ze leggen de gerenderde YAML naast het endpoint.
"""

from __future__ import annotations

import pytest
import yaml
from opi.services.catalog.vlam.endpoint import vlam_endpoint
from tests.e2e.helpers import vlam_stub

_CLUSTER = "sandboxed-local"


@pytest.fixture
def endpoint():
    resolved = vlam_endpoint(_CLUSTER)
    assert resolved is not None, f"cluster '{_CLUSTER}' kent geen VLAM-endpoint"
    return resolved


@pytest.fixture
def documents(endpoint) -> dict[str, dict]:
    from opi.core.cluster_config import get_vlam_config

    rendered = vlam_stub._manifests(endpoint, get_vlam_config(_CLUSTER))
    return {doc["kind"]: doc for doc in yaml.safe_load_all(rendered)}


def test_the_stub_lands_in_the_namespace_the_endpoint_names(documents, endpoint) -> None:
    assert documents["Namespace"]["metadata"]["name"] == endpoint.namespace
    for kind in ("ConfigMap", "Deployment", "Service", "NetworkPolicy"):
        assert documents[kind]["metadata"]["namespace"] == endpoint.namespace


def test_the_service_name_is_the_host_in_the_address(documents, endpoint) -> None:
    """The address is ``http://<service>.<namespace>.svc.cluster.local:<port>``."""
    service = documents["Service"]
    host = f"{service['metadata']['name']}.{service['metadata']['namespace']}.svc.cluster.local"
    assert endpoint.api_url == f"http://{host}:{endpoint.port}"
    assert [port["port"] for port in service["spec"]["ports"]] == [endpoint.port]


def test_the_pod_carries_exactly_the_labels_the_egress_rule_selects(documents, endpoint) -> None:
    """The consumer's egress peer pins ``app`` AND ``project``; both must be on the pod."""
    labels = documents["Deployment"]["spec"]["template"]["metadata"]["labels"]
    for key, value in endpoint.pod_labels.items():
        assert labels.get(key) == value, f"pod-label {key}={labels.get(key)!r}, endpoint wil {value!r}"
    assert (
        documents["Deployment"]["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"]
        == endpoint.port
    )


def test_the_stub_answers_the_path_the_probe_calls(documents) -> None:
    """The probe in the Go image calls /v1/models; a stub on another path proves nothing."""
    config = documents["ConfigMap"]["data"]["haproxy.cfg"]
    assert "path /v1/models" in config
    assert vlam_stub.STUB_MODEL_ID in config
    assert "monitor-uri /healthz" in config


def test_the_closed_door_selects_the_stub_and_only_closes_ingress(documents, endpoint) -> None:
    """The baseline stand-in must actually cover the stub pod, or the wildcard proves nothing."""
    spec = documents["NetworkPolicy"]["spec"]
    assert spec["policyTypes"] == ["Ingress"]
    labels = documents["Deployment"]["spec"]["template"]["metadata"]["labels"]
    assert labels["deployment"] == spec["podSelector"]["matchLabels"]["deployment"]


def test_the_open_rule_is_rendered_by_the_service_itself(endpoint) -> None:
    """One ingress entry, no ``from`` selector, only the endpoint's port, on the stub pod.

    A wildcard peer renders as an entry WITHOUT ``from``. If that ever regresses to a rule
    with a peer selector, the sandbox stub would be unreachable and the failure would look
    like a broken egress rule at the consumer.
    """
    policy = yaml.safe_load(vlam_stub.wildcard_policy(_CLUSTER, endpoint))

    assert policy["kind"] == "NetworkPolicy"
    assert policy["metadata"]["namespace"] == endpoint.namespace
    assert policy["spec"]["podSelector"]["matchLabels"]["app"] == endpoint.pod_labels["app"]
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    entries = policy["spec"]["ingress"]
    assert len(entries) == 1, f"verwacht een inkomende regel, gevonden: {entries}"
    assert "from" not in entries[0], f"de wildcard-regel hoort geen from-selector te hebben: {entries[0]}"
    assert [port["port"] for port in entries[0]["ports"]] == [endpoint.port]
