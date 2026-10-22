"""De flowmatrix tegen de policies zoals ze in git staan.

Deze test zegt niets over wat het beleid zou moeten zijn: hij legt vast wat het nu
toelaat. Verandert er een policy, dan verandert hier een regel mee, en die diff is het
gesprek over de wijziging.
"""

from __future__ import annotations

import copy

import pytest
from tests.netpol.evaluator import ONBEOORDEELBAAR, evaluate
from tests.netpol.matrix import (
    TENANT_NAMESPACE,
    TENANT_PROJECT,
    evaluate_flow,
    load_matrix,
    odcn_policies,
    odcn_policies_zonder_allow_all,
)
from tests.netpol.policies import tenant_baseline

ENDPOINTS, FLOWS = load_matrix()


@pytest.fixture(scope="module")
def policies():
    return odcn_policies()


@pytest.fixture(scope="module")
def policies_zonder_allow_all():
    return odcn_policies_zonder_allow_all()


@pytest.mark.parametrize("flow", FLOWS, ids=lambda f: f"{f.van}->{f.naar}:{f.poort}")
def test_de_matrix_beschrijft_het_huidige_beleid(flow, policies):
    verdict = evaluate_flow(flow, ENDPOINTS, policies)
    gemeten = "open" if verdict.allowed else "dicht"
    assert gemeten == flow.verwacht, (
        f"{flow.van} -> {flow.naar} op poort {flow.poort} is {gemeten}, "
        f"de matrix verwacht {flow.verwacht} ({flow.waarom}). Reden: {verdict.reason}"
    )


def test_geen_matrixregel_rust_op_een_onbeoordeelbaar_verdict(policies):
    """Een verdict met "niet te beoordelen" komt van een ipBlock die de evaluator niet
    tegen een pod-doel kan leggen (zie evaluator.py `_unjudged_peer`): een `dicht` met
    die reden erbij is geen bevestigde weigering, maar een grens van het model. Rust een
    matrixregel hierop, dan hoort die bij `task netpol:meet` en niet hier."""
    for flow in FLOWS:
        verdict = evaluate_flow(flow, ENDPOINTS, policies)
        assert ONBEOORDEELBAAR not in verdict.reason, (
            f"{flow.van} -> {flow.naar}:{flow.poort} rust op een onbeoordeelbaar ipBlock: {verdict.reason}"
        )


@pytest.mark.parametrize("flow", FLOWS, ids=lambda f: f"{f.van}->{f.naar}:{f.poort}")
def test_de_matrix_beschrijft_het_beleid_zonder_de_allow_all(flow, policies_zonder_allow_all):
    """De eindtoestand: wat er geldt als de uitsluitlijst elke workload bevat.

    Deze test is de toets voor een nieuwe per-component policy. Faalt hij op een regel
    die open hoort te zijn, dan mist die workload een regel en zou hij breken zodra hij
    buiten de allow-all valt.
    """
    verdict = evaluate_flow(flow, ENDPOINTS, policies_zonder_allow_all)
    gemeten = "open" if verdict.allowed else "dicht"
    assert gemeten == flow.zonder_allow_all, (
        f"zonder emergency-restore-allow-all is {flow.van} -> {flow.naar} op poort "
        f"{flow.poort} {gemeten}, de matrix verwacht {flow.zonder_allow_all} "
        f"({flow.waarom}). Reden: {verdict.reason}"
    )


def test_de_allow_all_maakt_verschil():
    """Anders zegt de tweede kolom niets: er moet minstens een regel omslaan."""
    assert any(f.verwacht != f.zonder_allow_all for f in FLOWS)


def test_de_matrix_dekt_beide_adresfamilies():
    """De internetregel moet op IPv4 en IPv6 getoetst zijn: het cluster is dual-stack."""
    doelen = {flow.naar for flow in FLOWS}
    assert {"internet-v4", "internet-v6"} <= doelen


def _internetregel(policy):
    for rule in policy["spec"]["egress"]:
        if any("ipBlock" in (peer or {}) for peer in rule.get("to") or []):
            return rule
    raise AssertionError("de tenant-baseline heeft geen egress-regel met een ipBlock")


class TestDeMatrixMerktEenWijziging:
    """Zonder deze test zegt een groene matrix niets: hij moet rood kunnen worden."""

    def test_poort_25_openzetten_maakt_de_matrix_rood(self, policies):
        gemuteerd = copy.deepcopy(policies)
        for policy in gemuteerd:
            if policy["metadata"]["name"].endswith("tenant-baseline-network-policy"):
                _internetregel(policy)["ports"].append({"protocol": "TCP", "port": 25})

        dichte_smtp = [
            f for f in FLOWS if f.van.startswith("tenant") and f.naar.startswith("internet") and f.poort == 25
        ]
        assert dichte_smtp, "de matrix moet een dichte SMTP-regel houden"
        for flow in dichte_smtp:
            assert evaluate_flow(flow, ENDPOINTS, gemuteerd).allowed, (
                f"{flow.van} -> {flow.naar}:25 hoort open te gaan zodra de internetregel poort 25 dekt"
            )

    def test_de_internetregel_weghalen_sluit_uitgaand_verkeer(self, policies):
        gemuteerd = copy.deepcopy(policies)
        for policy in gemuteerd:
            if policy["metadata"]["name"].endswith("tenant-baseline-network-policy"):
                spec = policy["spec"]
                spec["egress"] = [r for r in spec["egress"] if r is not _internetregel(policy)]

        open_internet = [
            f for f in FLOWS if f.van.startswith("tenant") and f.naar.startswith("internet") and f.verwacht == "open"
        ]
        assert open_internet, "de matrix moet een open internetregel van een tenant houden"
        for flow in open_internet:
            assert not evaluate_flow(flow, ENDPOINTS, gemuteerd).allowed


class TestDeExceptSluitHetClusterUit:
    """Wat de internetregel doet zodra een cluster zijn pod- en service-CIDR noemt.

    De matrix hierboven kan dit niet vastleggen: zodra de internetregel een except
    draagt, legt een CNI die tegen het echte bestemmingsadres, en dit model kent geen
    pod-IP. Deze test rendert de baseline met een waarde per adresfamilie en toont het
    verschil: een pod in een andere namespace valt buiten de regel, een extern adres
    niet. De gemeten variant (task netpol:meet op Calico) is het bewijs; dit is de
    berekening.
    """

    @staticmethod
    def _policies_met_cidrs() -> list:
        baseline = tenant_baseline(
            namespace=TENANT_NAMESPACE,
            deployment_name="web",
            project_name=TENANT_PROJECT,
            # Beide adresfamilies: een except op alleen IPv4 laat ::/0 catch-all, en
            # daarmee blijft elke pod bereikbaar via zijn IPv6-adres.
            cluster_cidrs={
                "ipv4": ["10.128.0.0/14", "172.30.0.0/16"],
                "ipv6": ["fd01::/48", "fd02::/112"],
            },
        )
        return [baseline]

    def test_een_pod_in_een_andere_namespace_valt_buiten_de_internetregel(self):
        verdict = evaluate(
            self._policies_met_cidrs(),
            ENDPOINTS["tenant-app"],
            ENDPOINTS["tenant-helm-pod"],
            443,
        )
        assert not verdict.allowed, verdict.reason

    def test_de_router_blijft_bereikbaar(self):
        """Het OIDC-pad loopt via de router, en die valt binnen de except.

        De baseline heeft daarom een eigen egressregel naar de ingress-controller; zonder
        die regel gaat elke verbinding via een publieke hostnaam dicht.
        """
        verdict = evaluate(
            self._policies_met_cidrs(),
            ENDPOINTS["tenant-app"],
            ENDPOINTS["router"],
            443,
        )
        assert verdict.allowed, verdict.reason

    def test_een_extern_adres_blijft_bereikbaar(self):
        verdict = evaluate(
            self._policies_met_cidrs(),
            ENDPOINTS["tenant-app"],
            ENDPOINTS["internet-v4"],
            443,
        )
        assert verdict.allowed, verdict.reason
