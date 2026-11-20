"""RC-513: the two halves of the routing must fit together.

OPI renders the routes of a project, and the routing of the cluster renders the Gateway, its
listeners and the grants those routes hang on. RC-509 and RC-510 built both halves at the
same time against a contract on paper, so nothing measured that they actually line up. This
does: it takes the gateway goldens of OPI and the kustomize build of the routing, and asks
whether every reference of the one is answered by the other.

It also keeps the three layers apart, because that split is what makes the switch one line:

* ``base/`` is cluster independent and standard only: routes, no CRD, no controller, no
  controller specific kind, and no Gateway (whether ZAD creates that or the platform
  delivers it is a choice per cluster);
* ``sandboxed-local/`` is what ArgoCD manages, namespaced only;
* ``platform/sandboxed-local/`` is what the bootstrap installs and ArgoCD never touches.

See docs/sandbox/routing-wisselen.md and features/gateway-api-routing.md.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from opi.core.cluster_config import CLUSTER_CONFIG

REPO_ROOT = Path(__file__).resolve().parents[3]

ROUTING_ROOT = REPO_ROOT / "infrastructure/bootstrap/routing"
SWITCH = ROUTING_ROOT / "sandboxed-local/kustomization.yaml"
NGINX_PLATFORM = ROUTING_ROOT / "platform/sandboxed-local/ingress-nginx/kustomization.yaml"
NGINX_CONTROLLER = ROUTING_ROOT / "sandboxed-local/ingress/controller/kustomization.yaml"
GATEWAY_GOLDENS = Path(__file__).parent / "golden/pipeline-gateway"
CRD_FIXTURE = Path(__file__).parent / "fixtures/gateway-api-crds-v1.6.1.yaml"

#: The label OPI puts on every project namespace (manifests/namespace.yaml.jinja).
TENANT_NAMESPACE_LABELS = {"created-by": "operations-manager"}
#: The namespace of a rendered project. Only the labels decide, so the name is free.
TENANT_NAMESPACE = "rig-golden-basis"

GATEWAY_API_GROUP = "gateway.networking.k8s.io"
#: Which listener protocols a route kind may hang on.
PROTOCOLS_PER_ROUTE = {"HTTPRoute": {"HTTP", "HTTPS"}, "TLSRoute": {"TLS"}}

#: The scope of every kind the three layers render. A kind that is not in here fails the
#: scope tests below instead of landing silently in the wrong half.
CLUSTER_SCOPED_KINDS = frozenset(
    {
        "Namespace",
        "CustomResourceDefinition",
        "ClusterRole",
        "ClusterRoleBinding",
        "ValidatingWebhookConfiguration",
        "MutatingWebhookConfiguration",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
        "IngressClass",
        "GatewayClass",
    }
)
NAMESPACED_KINDS = frozenset(
    {
        "ConfigMap",
        "Secret",
        "Service",
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "Deployment",
        "Job",
        "Ingress",
        "Gateway",
        "HTTPRoute",
        "TLSRoute",
        "ListenerSet",
        "ReferenceGrant",
        "EnvoyProxy",
        "BackendTrafficPolicy",
    }
)

#: What a cluster independent base may contain: the standard Gateway API route kinds, a plain
#: Ingress, and the ConfigMap with the stand. No Gateway, and nothing a controller brings its
#: own CRD for: Fundament runs Istio, where EnvoyProxy and BackendTrafficPolicy do not exist.
BASE_KINDS = frozenset({"HTTPRoute", "TLSRoute", "ListenerSet", "ReferenceGrant", "Ingress", "ConfigMap"})
#: The Gateway API kinds OPI itself may render, for the same reason.
STANDARD_ROUTE_KINDS = frozenset({"HTTPRoute", "TLSRoute", "ListenerSet", "ReferenceGrant"})


def _kustomize() -> str:
    found = shutil.which("kustomize")
    if found is None:
        pytest.skip("kustomize is missing")
    return found


def _build(path: Path) -> tuple[dict[str, Any], ...]:
    result = subprocess.run([_kustomize(), "build", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return tuple(doc for doc in yaml.safe_load_all(result.stdout) if doc)


@cache
def _build_component(component: str) -> tuple[dict[str, Any], ...]:
    """A kustomize Component, built through a throwaway root next to it.

    A Component cannot be built on its own, and building through the switch in git would let
    the stand that happens to be in git decide what is measured.
    """
    root = ROUTING_ROOT / f".contract-{component.replace('/', '-')}"
    root.mkdir(exist_ok=True)
    try:
        (root / "kustomization.yaml").write_text(
            f"apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\ncomponents:\n  - ../{component}\n"
        )
        return _build(root)
    finally:
        shutil.rmtree(root)


def build(stand: str) -> tuple[dict[str, Any], ...]:
    """What ArgoCD manages in this stand: the sandbox overlay, base included."""
    return _build_component(f"sandboxed-local/{stand}")


def build_base(stand: str) -> tuple[dict[str, Any], ...]:
    """The cluster independent half of this stand."""
    return _build_component(f"base/{stand}")


@cache
def build_platform(part: str) -> tuple[dict[str, Any], ...]:
    """What the bootstrap installs and ArgoCD never touches."""
    return _build(ROUTING_ROOT / "platform/sandboxed-local" / part)


def opi_gateway_objects() -> list[dict[str, Any]]:
    """Every Gateway API object in the gateway goldens of the rendering pipeline."""
    objects = []
    for path in sorted(GATEWAY_GOLDENS.rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and str(doc.get("apiVersion", "")).startswith(GATEWAY_API_GROUP):
                doc["_golden"] = str(path.relative_to(GATEWAY_GOLDENS))
                doc["_scenario"] = path.relative_to(GATEWAY_GOLDENS).parts[0]
                objects.append(doc)
    assert objects, f"no gateway goldens under {GATEWAY_GOLDENS}"
    return objects


def opi_listener_sets() -> dict[tuple[str, str], dict[str, Any]]:
    """The ListenerSets OPI renders, by scenario and name: a route of a project may hang on
    its own set instead of on a listener of the shared Gateway."""
    return {
        (obj["_scenario"], obj["metadata"]["name"]): obj
        for obj in opi_gateway_objects()
        if obj["kind"] == "ListenerSet"
    }


def parent_refs_of(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """``parentRefs`` of a route, or the single ``parentRef`` of a ListenerSet."""
    if "parentRef" in obj["spec"]:
        return [obj["spec"]["parentRef"]]
    return list(obj["spec"].get("parentRefs") or [])


def listeners_of_the_gateway() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The one shared Gateway of the gateway stand, with its listeners."""
    gateways = [doc for doc in build("gateway") if doc["kind"] == "Gateway"]
    assert len(gateways) == 1, [gw["metadata"]["name"] for gw in gateways]
    return gateways[0], gateways[0]["spec"]["listeners"]


def _namespaces_allowed(selector: dict[str, Any], namespace: str, labels: dict[str, str]) -> bool:
    """Whether a Gateway API ``namespaces`` selector lets this namespace through."""
    source = selector.get("from", "Same")
    if source == "All":
        return True
    if source == "Same":
        return namespace == selector["_own_namespace"]
    match_labels = (selector.get("selector") or {}).get("matchLabels") or {}
    return all(labels.get(key) == value for key, value in match_labels.items())


#: The HSTS header the pinned ingress-nginx sends in the other stand, which the gateway
#: stand has to match (features/gateway-api-routing.md says where the value comes from).
HSTS = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


def _response_headers_of(route: dict[str, Any]) -> list[tuple[str, str]]:
    """The headers every ``ResponseHeaderModifier`` of this route sets."""
    return [
        (header["name"], header["value"])
        for rule in route["spec"]["rules"]
        for entry in rule.get("filters") or []
        if entry["type"] == "ResponseHeaderModifier"
        for header in entry["responseHeaderModifier"].get("set") or []
    ]


def test_the_switch_is_one_line() -> None:
    """The whole switch: one components line, naming one of the two stands."""
    kustomization = yaml.safe_load(SWITCH.read_text())

    assert kustomization["components"] in (["./ingress"], ["./gateway"]), (
        f"{SWITCH.relative_to(REPO_ROOT)} must name exactly one routing component"
    )


def test_the_switch_matches_the_stand_of_cluster_config() -> None:
    """``CLUSTER_CONFIG`` is only the fallback for a cluster without the ConfigMap
    zad-routing, so on the sandbox the two must still start out equal."""
    (component,) = yaml.safe_load(SWITCH.read_text())["components"]
    stand = CLUSTER_CONFIG["sandboxed-local"]["ingress"].get("routing", "ingress")

    assert component.rsplit("/", 1)[-1] == stand


@pytest.mark.parametrize("stand", ["ingress", "gateway"])
def test_what_argocd_manages_is_namespaced_only(stand: str) -> None:
    """The AppProject zad-routing has ``clusterResourceWhitelist: []``, so a cluster scoped
    resource in a stand would drop the whole sync. They belong to the platform layer."""
    cluster_scoped = [
        f"{doc['kind']}/{doc['metadata']['name']}" for doc in build(stand) if doc["kind"] in CLUSTER_SCOPED_KINDS
    ]
    unknown = [doc["kind"] for doc in build(stand) if doc["kind"] not in CLUSTER_SCOPED_KINDS | NAMESPACED_KINDS]

    assert cluster_scoped == []
    assert unknown == [], "a kind of unknown scope: add it to one of the two sets above"


@pytest.mark.parametrize("stand", ["ingress", "gateway"])
def test_every_stand_names_a_namespace_everywhere(stand: str) -> None:
    """The Application renders without the CMP plugin, which would force one namespace on
    everything, so every resource must carry its own."""
    without = [
        f"{doc['kind']}/{doc['metadata']['name']}" for doc in build(stand) if not doc["metadata"].get("namespace")
    ]

    assert without == []


@pytest.mark.parametrize("stand", ["ingress", "gateway"])
def test_the_base_holds_standard_kinds_only(stand: str) -> None:
    """The base is cluster independent, so it may not assume a controller. EnvoyProxy and
    BackendTrafficPolicy are CRDs of Envoy Gateway and belong in the overlay of a cluster
    that runs it; on Fundament, which runs Istio, they do not exist."""
    foreign = sorted({doc["kind"] for doc in build_base(stand)} - BASE_KINDS)

    assert foreign == []


@pytest.mark.parametrize("stand", ["ingress", "gateway"])
def test_the_base_does_not_create_the_shared_gateway(stand: str) -> None:
    """Whether ZAD creates the Gateway or the platform delivers it is a choice per cluster,
    so the base makes neither. On the sandbox ZAD makes it, in the overlay."""
    assert [doc["kind"] for doc in build_base(stand) if doc["kind"] in ("Gateway", "GatewayClass")] == []


def test_opi_renders_standard_route_kinds_only() -> None:
    """Same requirement for what OPI renders per deployment: a controller specific kind would
    tie every project to one controller. The goldens hold HTTPRoute, TLSRoute and
    ListenerSet, all three standard Gateway API."""
    kinds = {obj["kind"] for obj in opi_gateway_objects()}

    assert kinds - STANDARD_ROUTE_KINDS == set()
    assert kinds, "the goldens must hold routes, or this proves nothing"


@pytest.mark.parametrize("stand", ["ingress", "gateway"])
def test_every_stand_brings_its_own_entrypoint_and_stand(stand: str) -> None:
    """The ExternalName CoreDNS points at, and the ConfigMap OPI reads its stand from."""
    documents = {(doc["kind"], doc["metadata"]["name"]): doc for doc in build(stand)}

    ingang = documents[("Service", "zad-ingang")]
    assert ingang["spec"]["type"] == "ExternalName"
    assert ingang["spec"]["externalName"].endswith(".svc.cluster.local")
    assert documents[("ConfigMap", "zad-routing")]["data"]["routing"] == stand


def test_in_gateway_stand_every_platform_route_sends_hsts() -> None:
    """The Gateway API has no setting for ``Strict-Transport-Security``, where ingress-nginx
    sends it itself, so every route on the https listener carries it as a filter. Not the
    port 80 redirect: that answers nothing of its own."""
    routes = [
        doc
        for doc in build("gateway")
        if doc["kind"] == "HTTPRoute" and {ref.get("sectionName") for ref in parent_refs_of(doc)} == {"https"}
    ]
    assert len(routes) == 8, [route["metadata"]["name"] for route in routes]

    without = [
        f"{route['metadata']['namespace']}/{route['metadata']['name']}"
        for route in routes
        if HSTS not in _response_headers_of(route)
    ]
    assert without == []

    (redirect,) = [doc for doc in build("gateway") if doc["metadata"]["name"] == "https-redirect"]
    assert _response_headers_of(redirect) == []


# 1. Every parentRef points at a listener that exists, with a protocol that fits.
def test_every_parentref_of_opi_points_at_an_existing_listener() -> None:
    gateway, listeners = listeners_of_the_gateway()
    gateway_listeners = {listener["name"]: listener for listener in listeners}
    sets = opi_listener_sets()
    problems = []

    for obj in opi_gateway_objects():
        where = f"{obj['_golden']} {obj['kind']}/{obj['metadata']['name']}"
        for ref in parent_refs_of(obj):
            kind = ref.get("kind", "Gateway")
            if kind == "Gateway":
                if (ref["name"], ref.get("namespace")) != (
                    gateway["metadata"]["name"],
                    gateway["metadata"]["namespace"],
                ):
                    problems.append(
                        f"{where}: parentRef {ref.get('namespace')}/{ref['name']} is not the shared Gateway"
                    )
                    continue
                available = gateway_listeners
            elif kind == "ListenerSet":
                # A ListenerSet of OPI itself, in the namespace of the project; that one
                # hangs on the Gateway in turn, which the same loop validates.
                listener_set = sets.get((obj["_scenario"], ref["name"]))
                if listener_set is None:
                    problems.append(f"{where}: parentRef ListenerSet/{ref['name']} is not rendered by OPI")
                    continue
                available = {listener["name"]: listener for listener in listener_set["spec"]["listeners"]}
            else:
                problems.append(f"{where}: parentRef kind {kind}")
                continue

            section = ref.get("sectionName")
            if section is None:
                # A ListenerSet names no sectionName: it hangs on the Gateway as a whole.
                continue
            if section not in available:
                problems.append(f"{where}: sectionName '{section}' is not a listener of {kind}/{ref['name']}")
                continue
            allowed = PROTOCOLS_PER_ROUTE.get(obj["kind"])
            if allowed is not None and available[section]["protocol"] not in allowed:
                problems.append(
                    f"{where}: listener '{section}' is {available[section]['protocol']}, "
                    f"and a {obj['kind']} needs one of {sorted(allowed)}"
                )

    assert problems == []


# 2. The allowedListeners of the Gateway allow the namespace OPI renders in.
def test_the_gateway_allows_the_listenerset_of_a_project_namespace() -> None:
    gateway, _ = listeners_of_the_gateway()
    listener_sets = [obj for obj in opi_gateway_objects() if obj["kind"] == "ListenerSet"]
    assert listener_sets, "the goldens must contain a ListenerSet, or this proves nothing"

    selector = dict(gateway["spec"]["allowedListeners"]["namespaces"])
    selector["_own_namespace"] = gateway["metadata"]["namespace"]

    assert _namespaces_allowed(selector, TENANT_NAMESPACE, TENANT_NAMESPACE_LABELS)
    for (scenario, name), listener_set in opi_listener_sets().items():
        parent = listener_set["spec"]["parentRef"]
        assert (parent["name"], parent["namespace"]) == (
            gateway["metadata"]["name"],
            gateway["metadata"]["namespace"],
        ), f"{scenario}: ListenerSet {name} hangs on another Gateway"


# 3. The allowedRoutes of every listener allow the routes of OPI.
def test_every_listener_allows_the_routes_of_a_project_namespace() -> None:
    """Both halves: a listener of the shared Gateway that OPI hangs routes on, and a listener
    of a ListenerSet OPI renders itself."""
    gateway, listeners = listeners_of_the_gateway()
    refused = []

    on_the_gateway = {
        ref.get("sectionName")
        for obj in opi_gateway_objects()
        if obj["kind"] in PROTOCOLS_PER_ROUTE
        for ref in parent_refs_of(obj)
        if ref.get("kind", "Gateway") == "Gateway"
    }
    for listener in listeners:
        if listener["name"] not in on_the_gateway:
            continue
        if not _namespaces_allowed(
            _selector_of(listener, gateway["metadata"]["namespace"]), TENANT_NAMESPACE, TENANT_NAMESPACE_LABELS
        ):
            refused.append(f"Gateway listener {listener['name']}")

    for (scenario, name), listener_set in opi_listener_sets().items():
        namespace = listener_set["metadata"]["namespace"]
        refused += [
            f"{scenario} ListenerSet {name} listener {listener['name']}"
            for listener in listener_set["spec"]["listeners"]
            if not _namespaces_allowed(_selector_of(listener, namespace), namespace, TENANT_NAMESPACE_LABELS)
        ]

    assert refused == []


def _selector_of(listener: dict[str, Any], own_namespace: str) -> dict[str, Any]:
    selector = dict((listener.get("allowedRoutes") or {}).get("namespaces") or {})
    selector["_own_namespace"] = own_namespace
    return selector


# 4. Every certificateRefs of a listener is covered.
def test_every_certificateref_of_the_gateway_is_covered() -> None:
    gateway, listeners = listeners_of_the_gateway()
    grants = [doc for doc in build("gateway") if doc["kind"] == "ReferenceGrant"]
    uncovered = []

    for listener in listeners:
        for ref in (listener.get("tls") or {}).get("certificateRefs") or []:
            namespace = ref.get("namespace") or gateway["metadata"]["namespace"]
            if namespace == gateway["metadata"]["namespace"]:
                continue
            if not any(_grant_covers(grant, gateway, namespace, ref) for grant in grants):
                uncovered.append(f"listener {listener['name']} -> {namespace}/{ref['name']}")

    assert uncovered == []


def _grant_covers(grant: dict[str, Any], gateway: dict[str, Any], namespace: str, ref: dict[str, Any]) -> bool:
    if grant["metadata"]["namespace"] != namespace:
        return False
    froms = grant["spec"]["from"]
    if not any(
        entry["kind"] == "Gateway"
        and entry["group"] == GATEWAY_API_GROUP
        and entry["namespace"] == gateway["metadata"]["namespace"]
        for entry in froms
    ):
        return False
    return any(
        entry["kind"] == ref.get("kind", "Secret")
        and entry["group"] == ref.get("group", "")
        and entry.get("name", ref["name"]) == ref["name"]
        for entry in grant["spec"]["to"]
    )


# 5. The controller_selector of CLUSTER_CONFIG finds the proxy pods of this Gateway.
def test_the_controller_selector_matches_the_proxy_of_this_gateway() -> None:
    """The labels and the namespace are those of Envoy Gateway v1.9.4:
    ``OwningGatewayNameLabel``/``OwningGatewayNamespaceLabel`` in
    internal/gatewayapi/translator.go, and ``DefaultNamespace`` in
    internal/envoygateway/config/config.go, which is where the proxy of a Gateway lands
    without gatewayNamespaceMode. The platform layer below must agree with that.
    """
    gateway, _ = listeners_of_the_gateway()
    selector = CLUSTER_CONFIG["sandboxed-local"]["ingress"]["gateway"]["controller_selector"]
    controller = next(
        doc
        for doc in build_platform("envoy-gateway")
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "envoy-gateway"
    )

    assert selector["namespace"] == controller["metadata"]["namespace"]
    assert selector["pod_labels"] == {
        "gateway.envoyproxy.io/owning-gateway-name": gateway["metadata"]["name"],
        "gateway.envoyproxy.io/owning-gateway-namespace": gateway["metadata"]["namespace"],
    }


# 6. Every Gateway API object of both halves validates against the schema of the CRDs.
@cache
def _schemas() -> dict[tuple[str, str], Draft202012Validator]:
    validators = {}
    for crd in yaml.safe_load_all(CRD_FIXTURE.read_text()):
        if not crd:
            continue
        kind = crd["spec"]["names"]["kind"]
        for version in crd["spec"]["versions"]:
            schema = version["schema"]["openAPIV3Schema"]
            validators[(kind, version["name"])] = Draft202012Validator(_to_json_schema(schema))
    return validators


def _to_json_schema(node: Any) -> Any:
    """Drop the Kubernetes-only vocabulary a JSON Schema validator would choke on."""
    if isinstance(node, dict):
        cleaned = {key: _to_json_schema(value) for key, value in node.items() if not key.startswith("x-kubernetes-")}
        return cleaned
    if isinstance(node, list):
        return [_to_json_schema(value) for value in node]
    return node


def _gateway_api_objects_of_both_halves() -> list[tuple[str, dict[str, Any]]]:
    objects = []
    for obj in opi_gateway_objects():
        where = obj.pop("_golden")
        obj.pop("_scenario")
        objects.append((where, obj))
    objects += [
        ("routing/gateway build", doc)
        for doc in build("gateway")
        if str(doc.get("apiVersion", "")).startswith(GATEWAY_API_GROUP)
    ]
    return objects


def test_every_gateway_api_object_validates_against_the_pinned_crds() -> None:
    """Structure, types and bounds out of the openAPIV3Schema. NOT the CEL rules
    (x-kubernetes-validations) and no unknown fields either: the API server prunes those
    instead of refusing them, so a schema without additionalProperties never sees them.
    """
    validators = _schemas()
    errors = []

    for where, obj in _gateway_api_objects_of_both_halves():
        version = obj["apiVersion"].split("/", 1)[1]
        validator = validators.get((obj["kind"], version))
        assert validator is not None, f"{where}: no CRD fixture for {obj['kind']} {version}"
        errors += [
            f"{where} {obj['kind']}/{obj['metadata']['name']}: {'/'.join(map(str, error.path))}: {error.message}"
            for error in validator.iter_errors(json.loads(json.dumps(obj)))
        ]

    assert errors == []


# 7. In the ingress stand: the Ingresses of the platform services name the IngressClass of
#    the platform layer, and the controller picks up the class-less Ingresses of OPI.
def test_in_ingress_stand_every_ingress_names_the_ingressclass_of_the_platform() -> None:
    classes = [doc for doc in build_platform("ingress-nginx") if doc["kind"] == "IngressClass"]
    assert len(classes) == 1, [cls["metadata"]["name"] for cls in classes]
    name = classes[0]["metadata"]["name"]

    wrong = [
        f"{doc['metadata']['namespace']}/{doc['metadata']['name']}: {doc['spec'].get('ingressClassName')}"
        for doc in build("ingress")
        if doc["kind"] == "Ingress" and doc["spec"].get("ingressClassName") != name
    ]

    assert wrong == []


def test_the_tenant_ingresses_of_opi_reach_the_same_controller() -> None:
    """OPI renders an Ingress without an ``ingressClassName``, so the controller of the stand
    must pick up a class-less Ingress: either because its IngressClass is the default, or
    because it runs with ``--watch-ingress-without-class``. That is what
    ``_verify_ingress_class`` in opi/services/routing_readiness.py leans on."""
    (ingress_class,) = [doc for doc in build_platform("ingress-nginx") if doc["kind"] == "IngressClass"]
    controller = next(
        doc
        for doc in build("ingress")
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "ingress-nginx-controller"
    )

    annotations = ingress_class["metadata"].get("annotations") or {}
    args = controller["spec"]["template"]["spec"]["containers"][0]["args"]
    assert (
        annotations.get("ingressclass.kubernetes.io/is-default-class") == "true"
        or "--watch-ingress-without-class=true" in args
    ), (
        f"IngressClass {ingress_class['metadata']['name']} is not the default and the controller does not "
        "watch class-less Ingresses, so a tenant Ingress of OPI gets no controller"
    )
    assert f"--ingress-class={ingress_class['metadata']['name']}" in args, (
        "the controller and its IngressClass must carry the same name"
    )


def test_the_admission_webhook_only_validates_creates_and_updates() -> None:
    """In gateway stand the webhook of nginx stays behind without a backend, because it is
    platform and the controller of the stand is gone. That is acceptable precisely because it
    only sees CREATE and UPDATE of an Ingress, and OPI creates none in that stand; the prune
    of the Ingresses of the other stand is a DELETE and goes past it."""
    (webhook,) = [doc for doc in build_platform("ingress-nginx") if doc["kind"] == "ValidatingWebhookConfiguration"]

    for entry in webhook["webhooks"]:
        for rule in entry["rules"]:
            assert set(rule["operations"]) <= {"CREATE", "UPDATE"}, rule
            assert rule["resources"] == ["ingresses"], rule


# The split of the nginx install manifest: the cluster scoped half is platform and always on
# the cluster, the namespaced half belongs to the stand. The two lists must stay each other's
# complement, or a version bump moves a resource into the wrong half unnoticed.
def _nginx_manifest_url(kustomization: Path) -> str:
    (url,) = re.findall(r"^\s*-\s*(https://\S+)$", kustomization.read_text(), re.MULTILINE)
    return url


def _nginx_upstream() -> tuple[dict[str, Any], ...]:
    """The manifest both halves are cut out of, built straight from the pinned URL."""
    root = ROUTING_ROOT / ".contract-nginx-upstream"
    root.mkdir(exist_ok=True)
    try:
        (root / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
            f"resources:\n  - {_nginx_manifest_url(NGINX_PLATFORM)}\n"
        )
        return _build(root)
    finally:
        shutil.rmtree(root)


def _identities(documents: tuple[dict[str, Any], ...]) -> set[tuple[str, str, str]]:
    return {(doc["kind"], doc["metadata"].get("namespace") or "", doc["metadata"]["name"]) for doc in documents}


def test_both_nginx_halves_are_cut_out_of_the_same_pinned_manifest() -> None:
    assert _nginx_manifest_url(NGINX_PLATFORM) == _nginx_manifest_url(NGINX_CONTROLLER)


def test_the_two_nginx_halves_are_the_whole_manifest_and_nothing_twice() -> None:
    platform = _identities(build_platform("ingress-nginx"))
    controller = _identities(_build(NGINX_CONTROLLER.parent))

    assert platform & controller == set(), "a resource in both halves: ArgoCD and the bootstrap would fight over it"
    assert platform | controller == _identities(_nginx_upstream()), (
        "the two halves are not the manifest: a version bump added or removed a resource, so "
        "the delete lists in both kustomizations need to follow"
    )


def test_the_nginx_platform_half_is_cluster_scoped_only() -> None:
    assert [
        f"{kind}/{name}"
        for kind, _, name in _identities(build_platform("ingress-nginx"))
        if kind not in CLUSTER_SCOPED_KINDS
    ] == []


def test_the_nginx_controller_half_is_namespaced_only() -> None:
    assert [
        f"{kind}/{name}"
        for kind, _, name in _identities(_build(NGINX_CONTROLLER.parent))
        if kind not in NAMESPACED_KINDS
    ] == []


# Both allowlists of ArgoCD must cover every kind a stand brings along: a kind missing from
# the AppProject drops the WHOLE sync, and a kind missing from resourceInclusions is silently
# not rolled out (features/argocd-resource-allowlist.md).
OVERLAY = REPO_ROOT / "bootstrap/rig-system/kustomize/overlays/sandboxed-local"
APPPROJECT = OVERLAY / "argocd-appproject-zad-routing.yaml"
ARGOCD_CR = OVERLAY / "argocd-deployment.yaml"


def _kinds_of_both_stands() -> set[tuple[str, str]]:
    """(apiGroup, kind) of everything the two stands render."""
    kinds = set()
    for stand in ("ingress", "gateway"):
        for doc in build(stand):
            api_version = doc["apiVersion"]
            group = api_version.rsplit("/", 1)[0] if "/" in api_version else ""
            kinds.add((group, doc["kind"]))
    return kinds


def test_the_appproject_covers_every_kind_of_both_stands() -> None:
    appproject = yaml.safe_load(APPPROJECT.read_text())["spec"]
    allowed = {(entry["group"], entry["kind"]) for entry in appproject["namespaceResourceWhitelist"]}

    assert appproject["clusterResourceWhitelist"] == [], (
        "the routing brings no cluster scoped resource any more: those are platform"
    )
    assert _kinds_of_both_stands() - allowed == set()


def test_the_appproject_allows_every_namespace_of_both_stands() -> None:
    allowed = {entry["namespace"] for entry in yaml.safe_load(APPPROJECT.read_text())["spec"]["destinations"]}
    used = {
        doc["metadata"]["namespace"]
        for stand in ("ingress", "gateway")
        for doc in build(stand)
        if doc["metadata"].get("namespace")
    }

    assert used - allowed == set()


def test_resourceinclusions_covers_every_kind_of_both_stands() -> None:
    inclusions = yaml.safe_load(yaml.safe_load(ARGOCD_CR.read_text())["spec"]["resourceInclusions"])
    allowed = {(group, kind) for entry in inclusions for group in entry["apiGroups"] for kind in entry["kinds"]}

    assert _kinds_of_both_stands() - allowed == set()
