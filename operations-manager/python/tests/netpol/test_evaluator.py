"""De semantiek van de NetworkPolicy-evaluator, regel voor regel."""

from __future__ import annotations

from typing import Any

from tests.netpol.evaluator import External, Pod, evaluate, matches_selector

TENANT = Pod(namespace="rig-prd-demo", labels={"deployment": "web"})
PEER = Pod(namespace="rig-prd-demo", labels={"deployment": "api"})
OTHER_NAMESPACE = Pod(namespace="rig-prd-operations", labels={"app": "minio"})


def policy(spec: dict[str, Any], *, name: str = "p", namespace: str = "rig-prd-demo") -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }


class TestZonderPolicy:
    def test_een_pod_zonder_policy_is_open(self):
        assert evaluate([], TENANT, PEER, 8000)

    def test_een_policy_in_een_andere_namespace_selecteert_niet(self):
        deny = policy({"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}, namespace="rig-prd-anders")
        assert evaluate([deny], TENANT, PEER, 8000)


class TestDefaultDeny:
    def test_lege_podselector_met_beide_richtingen_sluit_alles(self):
        deny = policy({"podSelector": {}, "policyTypes": ["Ingress", "Egress"]})
        assert not evaluate([deny], TENANT, PEER, 8000)

    def test_alleen_ingress_in_policytypes_laat_egress_ongemoeid(self):
        """Een policy die alleen Ingress afdwingt mag egress niet raken."""
        deny = policy({"podSelector": {}, "policyTypes": ["Ingress"]})
        # Naar een pod in een andere namespace: daar selecteert deze policy niets,
        # dus de ingress-kant is open en de egress-kant wordt niet afgedwongen.
        assert evaluate([deny], TENANT, OTHER_NAMESPACE, 9000)

    def test_zonder_policytypes_volgt_ingress_altijd(self):
        """Zonder expliciete policyTypes leidt Kubernetes Ingress af, Egress alleen
        als er een egress-sectie staat."""
        deny = policy({"podSelector": {}, "ingress": []})
        assert not evaluate([deny], PEER, TENANT, 8000)
        # Egress van TENANT wordt niet afgedwongen: er is geen egress-sectie.
        assert evaluate([deny], TENANT, OTHER_NAMESPACE, 9000)

    def test_een_egress_sectie_zonder_policytypes_wordt_wel_afgedwongen(self):
        """De andere kant van de vorige regel: met een egress-sectie leidt
        Kubernetes Egress af, ook zonder expliciete policyTypes."""
        deny = policy({"podSelector": {}, "egress": []})
        assert not evaluate([deny], TENANT, OTHER_NAMESPACE, 9000)


class TestAllowAll:
    def test_lege_ingress_regel_laat_alles_toe(self):
        allow = policy({"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{}]})
        assert evaluate([allow], PEER, TENANT, 8000)

    def test_ingress_regel_met_alleen_ports_laat_elke_bron_toe(self):
        allow = policy(
            {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"port": 8000, "protocol": "TCP"}]}],
            }
        )
        assert evaluate([allow], PEER, TENANT, 8000)
        assert not evaluate([allow], PEER, TENANT, 8001)

    def test_een_lege_peer_lijst_laat_elke_peer_toe(self):
        """`from: []` is volgens de API hetzelfde als geen `from`: elke peer.

        operations-manager-policy schrijft het zo, voor ingress en voor egress.
        """
        ingress = policy(
            {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": [], "ports": [{"port": 8000}]}]},
            name="ingress-leeg",
        )
        egress = policy(
            {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{"to": [], "ports": [{"port": 443}]}]},
            name="egress-leeg",
        )
        assert evaluate([ingress], PEER, TENANT, 8000)
        assert evaluate([egress], TENANT, External("93.184.216.34"), 443)


class TestVerenigingVanPolicies:
    def test_twee_policies_tellen_bij_elkaar_op(self):
        """Selecteren meerdere policies dezelfde pod, dan is het toegestane verkeer de
        vereniging van hun regels."""
        deny = policy({"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []}, name="deny")
        allow = policy({"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{}]}, name="allow")
        assert evaluate([deny, allow], PEER, TENANT, 8000)


class TestPeerSelectors:
    def test_podselector_zonder_namespaceselector_blijft_in_eigen_namespace(self):
        allow = policy(
            {
                "podSelector": {"matchLabels": {"deployment": "web"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"podSelector": {"matchLabels": {"deployment": "api"}}}]}],
            }
        )
        assert evaluate([allow], PEER, TENANT, 8000)
        buiten = Pod(namespace="rig-prd-operations", labels={"deployment": "api"})
        assert not evaluate([allow], buiten, TENANT, 8000)

    def test_namespaceselector_en_podselector_in_een_peer_is_een_and(self):
        allow = policy(
            {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {"kubernetes.io/metadata.name": "openshift-ingress"}
                                },
                                "podSelector": {"matchLabels": {"router": "rig"}},
                            }
                        ]
                    }
                ],
            }
        )
        router = Pod(namespace="openshift-ingress", labels={"router": "rig"})
        andere_router = Pod(namespace="openshift-ingress", labels={"router": "anders"})
        assert evaluate([allow], router, TENANT, 8000)
        assert not evaluate([allow], andere_router, TENANT, 8000)

    def test_twee_peers_in_een_lijst_is_een_or(self):
        allow = policy(
            {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {"podSelector": {"matchLabels": {"deployment": "api"}}},
                            {"podSelector": {"matchLabels": {"deployment": "worker"}}},
                        ]
                    }
                ],
            }
        )
        assert evaluate([allow], PEER, TENANT, 8000)
        assert evaluate([allow], Pod(namespace="rig-prd-demo", labels={"deployment": "worker"}), TENANT, 8000)
        assert not evaluate([allow], Pod(namespace="rig-prd-demo", labels={"deployment": "anders"}), TENANT, 8000)

    def test_lege_namespaceselector_dekt_elke_namespace(self):
        allow = policy(
            {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [
                    {"to": [{"namespaceSelector": {}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}]}
                ],
            }
        )
        dns = Pod(namespace="kube-system", labels={"k8s-app": "kube-dns"})
        assert evaluate([allow], TENANT, dns, 53, "UDP")


class TestMatchExpressions:
    def test_in_selecteert_de_genoemde_waarden(self):
        selector = {"matchExpressions": [{"key": "tier", "operator": "In", "values": ["web", "api"]}]}
        assert matches_selector({"tier": "web"}, selector)
        assert not matches_selector({"tier": "batch"}, selector)

    def test_notin_selecteert_ook_pods_zonder_het_label(self):
        """NotIn matcht een pod zonder dat label: er staat geen verboden waarde."""
        selector = {"matchExpressions": [{"key": "tier", "operator": "NotIn", "values": ["batch"]}]}
        assert matches_selector({"tier": "web"}, selector)
        assert matches_selector({}, selector)
        assert not matches_selector({"tier": "batch"}, selector)

    def test_exists_en_doesnotexist(self):
        bestaat = {"matchExpressions": [{"key": "tier", "operator": "Exists"}]}
        bestaat_niet = {"matchExpressions": [{"key": "tier", "operator": "DoesNotExist"}]}
        assert matches_selector({"tier": "web"}, bestaat)
        assert not matches_selector({}, bestaat)
        assert matches_selector({}, bestaat_niet)
        assert not matches_selector({"tier": "web"}, bestaat_niet)


class TestIpBlock:
    def test_cidr_met_except(self):
        allow = policy(
            {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["169.254.169.254/32"]}}],
                        "ports": [{"port": 443, "protocol": "TCP"}],
                    }
                ],
            }
        )
        assert evaluate([allow], TENANT, External("93.184.216.34"), 443)
        assert not evaluate([allow], TENANT, External("169.254.169.254"), 443)

    def test_adresfamilies_lopen_niet_door_elkaar(self):
        """0.0.0.0/0 dekt geen IPv6-bestemming en ::/0 geen IPv4."""
        alleen_v4 = policy(
            {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}], "ports": [{"port": 443, "protocol": "TCP"}]}],
            }
        )
        assert evaluate([alleen_v4], TENANT, External("93.184.216.34"), 443)
        assert not evaluate([alleen_v4], TENANT, External("2606:2800:220:1:248:1893:25c8:1946"), 443)

    def test_een_catch_all_dekt_ook_een_pod_in_het_cluster(self):
        """0.0.0.0/0 zonder except opent ook bestemmingen binnen het cluster.

        Een CNI legt de CIDR tegen het echte bestemmingsadres, en elk pod-IP valt in
        een blok met prefixlengte 0. Dat is precies de valkuil van deze regel.
        """
        allow = policy(
            {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}],
            }
        )
        assert evaluate([allow], TENANT, PEER, 8000)

    def test_een_smaller_blok_matcht_een_pod_niet_en_zegt_waarom(self):
        """Zonder pod-IP kan de evaluator een smal blok niet tegen een pod leggen."""
        allow = policy(
            {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [{"to": [{"ipBlock": {"cidr": "145.21.161.201/32"}}]}],
            }
        )
        verdict = evaluate([allow], TENANT, PEER, 8000)
        assert not verdict
        assert "niet te beoordelen" in verdict.reason
        assert "145.21.161.201/32" in verdict.reason

    def test_een_catch_all_met_except_matcht_een_pod_niet(self):
        """Met een except hangt het van het pod-IP af, en dat kent dit model niet."""
        allow = policy(
            {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8"]}}]}],
            }
        )
        verdict = evaluate([allow], TENANT, PEER, 8000)
        assert not verdict
        assert "niet te beoordelen" in verdict.reason


class TestPoorten:
    def test_een_poort_zonder_protocol_is_tcp(self):
        allow = policy(
            {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"port": 8000}]}],
            }
        )
        assert evaluate([allow], PEER, TENANT, 8000, "TCP")
        assert not evaluate([allow], PEER, TENANT, 8000, "UDP")

    def test_een_regel_zonder_ports_dekt_elke_poort(self):
        allow = policy(
            {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"podSelector": {"matchLabels": {"deployment": "api"}}}]}],
            }
        )
        assert evaluate([allow], PEER, TENANT, 1)
        assert evaluate([allow], PEER, TENANT, 65535)

    def test_endport_dekt_een_reeks(self):
        allow = policy(
            {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"port": 8000, "endPort": 8010, "protocol": "TCP"}]}],
            }
        )
        assert evaluate([allow], PEER, TENANT, 8005)
        assert not evaluate([allow], PEER, TENANT, 8011)


class TestBeideKanten:
    def test_een_verbinding_moet_langs_egress_en_ingress(self):
        """De bron mag eruit, maar het doel laat niets binnen."""
        egress_open = policy(
            {"podSelector": {"matchLabels": {"deployment": "web"}}, "policyTypes": ["Egress"], "egress": [{}]},
            name="egress-open",
        )
        ingress_dicht = policy(
            {"podSelector": {"matchLabels": {"app": "minio"}}, "policyTypes": ["Ingress"], "ingress": []},
            name="ingress-dicht",
            namespace="rig-prd-operations",
        )
        assert not evaluate([egress_open, ingress_dicht], TENANT, OTHER_NAMESPACE, 9000)

    def test_een_doel_buiten_het_cluster_heeft_geen_ingress_kant(self):
        egress_open = policy(
            {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}], "ports": [{"port": 443}]}],
            }
        )
        assert evaluate([egress_open], TENANT, External("93.184.216.34"), 443)

    def test_een_bron_buiten_het_cluster_heeft_geen_egress_kant(self):
        ingress_open = policy(
            {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"ipBlock": {"cidr": "93.184.216.0/24"}}], "ports": [{"port": 443}]}],
            }
        )
        assert evaluate([ingress_open], External("93.184.216.34"), TENANT, 443)
