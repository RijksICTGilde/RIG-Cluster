"""The routing API per cluster type: the setting, the choice per hostname, what renders.

OPI renders either Ingress resources or Gateway API resources per cluster type. These tests
measure the three places that choice is made: the cluster config, ``publication_for`` and the
templates.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.cluster_config import (
    CLUSTER_CONFIG,
    RoutingApi,
    get_gateway_config,
    get_ingress_controller_selector,
    get_routing_api,
    renders_gateway_api,
)
from opi.core.config import settings
from opi.generation.manifests import render_template
from opi.manager.project.manifests.application import _path_manifest_type, _remove_stale_route_manifests
from opi.manager.project.manifests.pruning import select_obsolete_component_manifests
from opi.manager.project.manifests.routing import (
    LISTENER_SET_MANIFEST,
    ListenerSetBuilder,
    PublicationRequest,
    RouteParent,
    covered_by_listener,
    publication_for,
    remove_listener_set,
)
from opi.manager.run_support import BUNDLE_KINDS, HTTPROUTE_KIND, bundle_kinds
from opi.services.routing_readiness import (
    RoutingEntrypointMissingError,
    RoutingEntrypointUnknownError,
    verify_routing_entrypoint,
)
from opi.utils.naming import generate_manifest_name
from ruamel.yaml import YAML

GATEWAY_CLUSTER = "sandboxed-local"


@pytest.fixture
def gateway_routing(monkeypatch: pytest.MonkeyPatch) -> str:
    """Put the sandbox cluster in gateway routing for the duration of one test."""
    ingress = {**CLUSTER_CONFIG[GATEWAY_CLUSTER]["ingress"], "routing": RoutingApi.GATEWAY}
    monkeypatch.setitem(CLUSTER_CONFIG[GATEWAY_CLUSTER], "ingress", ingress)
    return GATEWAY_CLUSTER


class TestTheSetting:
    def test_every_cluster_type_is_still_on_ingress(self) -> None:
        for cluster in CLUSTER_CONFIG:
            assert get_routing_api(cluster) is RoutingApi.INGRESS, f"{cluster} is not on ingress"

    def test_the_routing_api_comes_from_the_config(self, gateway_routing: str) -> None:
        assert get_routing_api(gateway_routing) is RoutingApi.GATEWAY
        assert renders_gateway_api(gateway_routing) is True

    def test_the_gateway_getter_refuses_in_ingress_routing(self) -> None:
        with pytest.raises(ValueError, match="renders Ingress resources"):
            get_gateway_config(GATEWAY_CLUSTER)

    def test_the_gateway_getter_returns_the_block(self, gateway_routing: str) -> None:
        gateway = get_gateway_config(gateway_routing)
        assert gateway["name"] == "rig-gateway"
        assert gateway["https_listener"]["hostname"] == "*.sandbox.rijksapp.dev"

    def test_gateway_routing_without_a_block_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            CLUSTER_CONFIG["odcn-production"],
            "ingress",
            {"enable_tls": True, "ip_whitelist": "0.0.0.0/0,::/0", "routing": RoutingApi.GATEWAY},
        )
        with pytest.raises(ValueError, match=r"without an ingress\.gateway block"):
            get_gateway_config("odcn-production")

    @pytest.mark.parametrize("whitelist", ["147.181.0.0/16", "10.0.0.0/8,::/0", ""])
    def test_a_restricted_ip_whitelist_refuses_gateway_routing(
        self, monkeypatch: pytest.MonkeyPatch, whitelist: str
    ) -> None:
        """The Gateway API has no HAProxy whitelist, so the restriction may not fall away."""
        ingress = {**CLUSTER_CONFIG[GATEWAY_CLUSTER]["ingress"]}
        ingress.update({"routing": RoutingApi.GATEWAY, "ip_whitelist": whitelist})
        monkeypatch.setitem(CLUSTER_CONFIG[GATEWAY_CLUSTER], "ingress", ingress)

        with pytest.raises(ValueError, match="no equivalent of the"):
            get_routing_api(GATEWAY_CLUSTER)

    @pytest.mark.parametrize("whitelist", ["0.0.0.0/0,::/0", "0.0.0.0"])
    def test_an_open_ip_whitelist_is_allowed(self, monkeypatch: pytest.MonkeyPatch, whitelist: str) -> None:
        ingress = {**CLUSTER_CONFIG[GATEWAY_CLUSTER]["ingress"]}
        ingress.update({"routing": RoutingApi.GATEWAY, "ip_whitelist": whitelist})
        monkeypatch.setitem(CLUSTER_CONFIG[GATEWAY_CLUSTER], "ingress", ingress)

        assert get_routing_api(GATEWAY_CLUSTER) is RoutingApi.GATEWAY

    def test_the_controller_selector_follows_the_routing_api(self, gateway_routing: str) -> None:
        """Every NetworkPolicy that admits the entrypoint reads this selector, so they follow."""
        selector = get_ingress_controller_selector(gateway_routing)
        assert selector["namespace"] == "envoy-gateway-system"
        assert selector["pod_labels"]["gateway.envoyproxy.io/owning-gateway-name"] == "rig-gateway"

    def test_the_controller_selector_is_nginx_in_ingress_routing(self) -> None:
        assert get_ingress_controller_selector(GATEWAY_CLUSTER)["namespace"] == "ingress-nginx"


class TestWhichListenerCoversAHostname:
    @pytest.mark.parametrize(
        ("listener", "hostname", "covered"),
        [
            ("*.sandbox.rijksapp.dev", "app.sandbox.rijksapp.dev", True),
            # A wildcard covers exactly one label, so a dotted address is not covered.
            ("*.sandbox.rijksapp.dev", "poc.app.sandbox.rijksapp.dev", False),
            ("*.sandbox.rijksapp.dev", "sandbox.rijksapp.dev", False),
            ("*.sandbox.rijksapp.dev", "app.voorbeeld.nl", False),
            ("app.voorbeeld.nl", "app.voorbeeld.nl", True),
            ("app.voorbeeld.nl", "anders.voorbeeld.nl", False),
        ],
    )
    def test_coverage(self, listener: str, hostname: str, covered: bool) -> None:
        assert covered_by_listener(listener, hostname) is covered


def _request(hostname: str, **overrides: Any) -> PublicationRequest:
    overrides.setdefault("tls_secret_name", "productie-web-tls")
    return PublicationRequest(cluster=GATEWAY_CLUSTER, hostname=hostname, **overrides)


def _builder(cluster: str) -> ListenerSetBuilder:
    return ListenerSetBuilder(cluster=cluster, deployment_name="productie", namespace="rig-p")


class TestTheChoicePerHostname:
    def test_ingress_routing_yields_an_ingress(self) -> None:
        publication = publication_for(None, _request("app.sandbox.rijksapp.dev"))
        assert publication.template == "ingress.yaml.jinja"
        assert publication.manifest_type == "ingress"
        assert publication.parent is None

    def test_the_cluster_zone_rides_the_shared_listener(self, gateway_routing: str) -> None:
        listeners = _builder(gateway_routing)
        publication = publication_for(listeners, _request("app.sandbox.rijksapp.dev"))

        assert publication.manifest_type == "httproute"
        assert publication.parent == RouteParent("Gateway", "rig-gateway", "rig-gateway", "https")
        assert listeners._managed == {}
        assert listeners._own == {}

    def test_an_own_domain_claims_a_listener_of_its_own(self, gateway_routing: str) -> None:
        listeners = _builder(gateway_routing)
        publication = publication_for(
            listeners, _request("app.voorbeeld.nl", issuer_name="letsencrypt-voorbeeld-nl-productie")
        )

        assert publication.parent is not None
        assert publication.parent.kind == "ListenerSet"
        assert publication.parent.name == "productie-listeners"
        assert publication.parent.section_name == "app-voorbeeld-nl"
        assert listeners._managed["app.voorbeeld.nl"]["tls_secret_name"] == "productie-web-tls"

    def test_an_own_certificate_goes_into_a_set_of_its_own(self, gateway_routing: str) -> None:
        """cert-manager.io/issuer applies to a whole ListenerSet, so a provided cert sits apart."""
        listeners = _builder(gateway_routing)
        publication = publication_for(
            listeners, _request("app.voorbeeld.nl", provided_tls_secret="productie-web-provided-tls")
        )

        assert publication.parent is not None
        assert publication.parent.name == "productie-own-listeners"
        assert listeners._managed == {}
        assert listeners._own["app.voorbeeld.nl"]["tls_secret_name"] == "productie-web-provided-tls"

    def test_two_components_on_one_hostname_share_one_listener(self, gateway_routing: str) -> None:
        """Two listeners on one hostname is a conflict on the Gateway."""
        listeners = _builder(gateway_routing)
        web = publication_for(
            listeners, _request("app.voorbeeld.nl", tls_secret_name="productie-web-tls", issuer_name="le")
        )
        api = publication_for(
            listeners, _request("app.voorbeeld.nl", tls_secret_name="productie-api-tls", issuer_name="le")
        )

        assert web.parent == api.parent
        assert list(listeners._managed) == ["app.voorbeeld.nl"]
        # The first certificate in sort order, so the outcome does not depend on the order
        # the components are rendered in.
        assert listeners._managed["app.voorbeeld.nl"]["tls_secret_name"] == "productie-api-tls"

    def test_the_component_order_does_not_decide(self, gateway_routing: str) -> None:
        listeners = _builder(gateway_routing)
        publication_for(listeners, _request("app.voorbeeld.nl", tls_secret_name="productie-api-tls", issuer_name="le"))
        publication_for(listeners, _request("app.voorbeeld.nl", tls_secret_name="productie-web-tls", issuer_name="le"))

        assert listeners._managed["app.voorbeeld.nl"]["tls_secret_name"] == "productie-api-tls"

    def test_passthrough_yields_a_tlsroute(self, gateway_routing: str) -> None:
        listeners = _builder(gateway_routing)
        publication = publication_for(listeners, _request("app.voorbeeld.nl", passthrough=True))

        assert publication.template == "tlsroute.yaml.jinja"
        assert publication.manifest_type == "tlsroute"
        assert listeners._own["app.voorbeeld.nl"]["passthrough"] is True
        assert "tls_secret_name" not in listeners._own["app.voorbeeld.nl"]


class TestThePathManifestType:
    def test_the_root_path_carries_no_suffix(self) -> None:
        assert _path_manifest_type("httproute", "/") == "httproute"
        assert _path_manifest_type("ingress", "") == "ingress"

    def test_a_path_becomes_a_suffix(self) -> None:
        assert _path_manifest_type("httproute", "/api") == "httproute-api"

    def test_a_nested_path_loses_its_slashes(self) -> None:
        assert _path_manifest_type("httproute", "/api/v2") == "httproute-apiv2"


class TestRemovingAStaleRouteManifestOnAnOwnerSwitch:
    """When the root or bare-domain address moves to another component, the manifest of the
    PREVIOUS owner is removed in every routing API, because only the API of the current round
    is about to be regenerated."""

    def test_every_routing_api_file_of_the_old_owner_is_removed(self, tmp_path: Path) -> None:
        for route_type in ("ingress", "httproute", "tlsroute"):
            (tmp_path / f"{generate_manifest_name('oud-component', f'{route_type}-root')}.yaml").write_text(
                f"kind: {route_type}\n"
            )
        untouched = tmp_path / f"{generate_manifest_name('oud-component', 'deployment')}.yaml"
        untouched.write_text("kind: Deployment\n")

        _remove_stale_route_manifests(str(tmp_path), "oud-component", "root", "test")

        assert list(tmp_path.iterdir()) == [untouched]

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        _remove_stale_route_manifests(str(tmp_path), "geen-bestand", "bare-domain", "test")

        assert list(tmp_path.iterdir()) == []


class TestTheListenerSetAsAFile:
    def test_without_a_claim_no_file_is_written(self, gateway_routing: str, tmp_path: Path) -> None:
        created: list[str] = []
        _builder(gateway_routing).emit(MagicMock(), str(tmp_path), created)

        assert created == []
        assert not (tmp_path / f"{LISTENER_SET_MANIFEST}.yaml").exists()

    def test_a_round_without_a_claim_removes_the_previous_one(self, tmp_path: Path) -> None:
        stale = tmp_path / f"{LISTENER_SET_MANIFEST}.yaml"
        stale.write_text("kind: ListenerSet\n")

        remove_listener_set(str(tmp_path))

        assert not stale.exists()

    def test_the_component_prune_does_not_see_the_listener_set(self, tmp_path: Path) -> None:
        """Why remove_listener_set exists: the name carries no component prefix."""
        (tmp_path / f"{LISTENER_SET_MANIFEST}.yaml").write_text("kind: ListenerSet\n")
        (tmp_path / "web-httproute.yaml").write_text("kind: HTTPRoute\n")

        obsolete = select_obsolete_component_manifests(str(tmp_path), {"web"}, set(), "productie")

        assert obsolete == ["web-httproute.yaml"]


def _ingress_snippet_headers() -> list[tuple[str, str]]:
    """The four security headers as the nginx snippet sets them, read from the template."""
    source = (Path(settings.MANIFESTS_PATH) / "ingress.yaml.jinja").read_text()
    return re.findall(r'more_set_headers "([^:]+): ([^"]*)";', source)


class TestTheRenderedHttpRoute:
    @staticmethod
    def _route(**overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "name": "productie-web",
            "hostname": "app.sandbox.rijksapp.dev",
            "service_name": "productie-web",
            "service_port": 8080,
            "path": "/",
            "enable_tls": True,
            "parent": {
                "kind": "Gateway",
                "name": "rig-gateway",
                "namespace": "rig-gateway",
                "section_name": "https",
            },
        }
        values.update(overrides)
        return YAML(typ="safe").load(render_template("httproute.yaml.jinja", values))

    def test_every_rule_switches_the_request_timeout_off(self) -> None:
        """Without a value Envoy cuts a request off after 15s, and 300s would break downloads."""
        for rule in self._route()["spec"]["rules"]:
            assert rule["timeouts"]["request"] == "0s"

    def test_the_security_headers_equal_the_snippet(self) -> None:
        modifier = next(
            f for f in self._route()["spec"]["rules"][0]["filters"] if f["type"] == "ResponseHeaderModifier"
        )
        emitted = [(header["name"], header["value"]) for header in modifier["responseHeaderModifier"]["set"]]
        expected = _ingress_snippet_headers()

        assert expected, "the snippet of ingress.yaml.jinja was not read"
        assert emitted[: len(expected)] == expected
        assert ("Strict-Transport-Security", "max-age=31536000;includeSubDomains;preload") in emitted

    def test_no_hsts_without_tls(self) -> None:
        modifier = next(
            f
            for f in self._route(enable_tls=False)["spec"]["rules"][0]["filters"]
            if f["type"] == "ResponseHeaderModifier"
        )
        names = [header["name"] for header in modifier["responseHeaderModifier"]["set"]]
        assert "Strict-Transport-Security" not in names

    def test_the_rewrite_becomes_a_urlrewrite_filter(self) -> None:
        route = self._route(path="/api", rewrite="/")
        rewrite = next(f for f in route["spec"]["rules"][0]["filters"] if f["type"] == "URLRewrite")
        assert rewrite["urlRewrite"]["path"] == {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/"}

    def test_without_a_rewrite_there_is_no_filter(self) -> None:
        types = [f["type"] for f in self._route()["spec"]["rules"][0]["filters"]]
        assert "URLRewrite" not in types


class TestTearingDownARunBundle:
    @pytest.mark.asyncio
    async def test_httproute_joins_when_the_cluster_serves_it(self) -> None:
        kubectl = AsyncMock()
        kubectl.list_namespaced_kinds.return_value = {HTTPROUTE_KIND: "httproutes.gateway.networking.k8s.io"}

        assert await bundle_kinds(kubectl) == f"{BUNDLE_KINDS},httproutes.gateway.networking.k8s.io"

    @pytest.mark.asyncio
    async def test_without_the_gateway_api_the_list_stays_as_it_was(self) -> None:
        """A type the cluster does not have makes kubectl refuse the whole command."""
        kubectl = AsyncMock()
        kubectl.list_namespaced_kinds.return_value = {"Secret": "secrets"}

        assert await bundle_kinds(kubectl) == BUNDLE_KINDS

    @pytest.mark.asyncio
    async def test_a_failed_discovery_falls_back_to_the_bare_list(self) -> None:
        kubectl = AsyncMock()
        kubectl.list_namespaced_kinds.return_value = None

        assert await bundle_kinds(kubectl) == BUNDLE_KINDS

    @pytest.mark.asyncio
    async def test_the_reaper_leaves_the_pods_out(self) -> None:
        kubectl = AsyncMock()
        kubectl.list_namespaced_kinds.return_value = {}

        kinds = await bundle_kinds(kubectl, exclude=frozenset({"pod"}))

        assert "pod" not in kinds.split(",")


class TestTheEntrypointForABulkReprocess:
    """The guard of :reprocess-all. Nobody points every project at an entrypoint that is absent."""

    @staticmethod
    def _kubectl(exists: bool | None, gateway: dict[str, Any] | None) -> AsyncMock:
        kubectl = AsyncMock()
        kubectl.resource_exists.return_value = exists
        kubectl.get_namespaced_resource.return_value = gateway
        return kubectl

    @pytest.mark.asyncio
    async def test_a_programmed_gateway_is_in_order(self, gateway_routing: str) -> None:
        kubectl = self._kubectl(True, {"status": {"conditions": [{"type": "Programmed", "status": "True"}]}})
        with patch("opi.services.routing_readiness.create_kubectl_connector", return_value=kubectl):
            assert "Programmed" in await verify_routing_entrypoint(gateway_routing)

    @pytest.mark.asyncio
    async def test_an_absent_gateway_is_definitively_wrong(self, gateway_routing: str) -> None:
        kubectl = self._kubectl(False, None)
        with (
            patch("opi.services.routing_readiness.create_kubectl_connector", return_value=kubectl),
            pytest.raises(RoutingEntrypointMissingError, match="bestaat niet"),
        ):
            await verify_routing_entrypoint(gateway_routing)

    @pytest.mark.asyncio
    async def test_a_failed_check_never_reads_as_absent(self, gateway_routing: str) -> None:
        kubectl = self._kubectl(None, None)
        with (
            patch("opi.services.routing_readiness.create_kubectl_connector", return_value=kubectl),
            pytest.raises(RoutingEntrypointUnknownError),
        ):
            await verify_routing_entrypoint(gateway_routing)

    @pytest.mark.asyncio
    async def test_a_gateway_without_programmed_accepts_no_routes_yet(self, gateway_routing: str) -> None:
        kubectl = self._kubectl(True, {"status": {"conditions": [{"type": "Programmed", "status": "False"}]}})
        with (
            patch("opi.services.routing_readiness.create_kubectl_connector", return_value=kubectl),
            pytest.raises(RoutingEntrypointMissingError, match="Programmed"),
        ):
            await verify_routing_entrypoint(gateway_routing)

    @pytest.mark.asyncio
    async def test_ingress_routing_wants_an_ingressclass(self) -> None:
        """Not a default one: an Ingress without ingressClassName is served anyway, which is
        what the sandbox does, and its only IngressClass carries no default annotation."""
        kubectl = AsyncMock()
        kubectl.get_cluster_resources.return_value = [{"metadata": {"name": "nginx", "annotations": {}}}]
        with patch("opi.services.routing_readiness.create_kubectl_connector", return_value=kubectl):
            assert "nginx" in await verify_routing_entrypoint(GATEWAY_CLUSTER)

    @pytest.mark.asyncio
    async def test_without_any_ingressclass_there_is_no_entrypoint(self) -> None:
        kubectl = AsyncMock()
        kubectl.get_cluster_resources.return_value = []
        with (
            patch("opi.services.routing_readiness.create_kubectl_connector", return_value=kubectl),
            pytest.raises(RoutingEntrypointMissingError, match="geen enkele IngressClass"),
        ):
            await verify_routing_entrypoint(GATEWAY_CLUSTER)

    @pytest.mark.asyncio
    async def test_an_unreadable_listing_never_reads_as_none_present(self) -> None:
        kubectl = AsyncMock()
        kubectl.get_cluster_resources.return_value = None
        with (
            patch("opi.services.routing_readiness.create_kubectl_connector", return_value=kubectl),
            pytest.raises(RoutingEntrypointUnknownError),
        ):
            await verify_routing_entrypoint(GATEWAY_CLUSTER)
