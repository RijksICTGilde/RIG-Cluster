"""De uitsluitlijst van emergency-restore-allow-all.

Elke waarde in die lijst haalt een workload uit de tijdelijke allow-all. Staat daar een
workload zonder eigen policy in, dan verliest hij in een keer al zijn verkeer; dat is
precies wat de onderbreking van 10 juni 2026 veroorzaakte.
"""

from __future__ import annotations

from typing import Any

from tests.netpol.evaluator import matches_selector
from tests.netpol.matrix import ALLOW_ALL_NAME
from tests.netpol.policies import OPS_NAMESPACE, platform_policies


def _allow_all() -> dict[str, Any]:
    for policy in platform_policies():
        if policy["metadata"]["name"] == ALLOW_ALL_NAME:
            return policy
    raise AssertionError(f"{ALLOW_ALL_NAME} staat niet meer in de lader")


def _uitgesloten_labels() -> list[dict[str, str]]:
    """Per uitgesloten workload de labels die hem herkennen."""
    selector = _allow_all()["spec"]["podSelector"]
    return [
        {expressie["key"]: waarde}
        for expressie in selector.get("matchExpressions") or []
        if expressie["operator"] == "NotIn"
        for waarde in expressie["values"]
    ]


def test_de_allow_all_sluit_workloads_uit():
    assert _uitgesloten_labels(), (
        "de allow-all selecteert nog elke pod; de uitsluitlijst is de weg naar het verwijderen van dit bestand"
    )


def test_elke_uitgesloten_workload_heeft_een_eigen_policy():
    eigen = [p for p in platform_policies() if p["metadata"]["name"] != ALLOW_ALL_NAME]
    for labels in _uitgesloten_labels():
        selecterend = [
            p["metadata"]["name"]
            for p in eigen
            if p["metadata"]["namespace"] == OPS_NAMESPACE
            and matches_selector(labels, p["spec"].get("podSelector") or {})
        ]
        assert selecterend, (
            f"{labels} staat in de uitsluitlijst van {ALLOW_ALL_NAME}, maar geen policy in "
            f"git selecteert zo'n pod in {OPS_NAMESPACE}. Zet de eigen policy eerst neer."
        )


def test_een_onbekende_pod_valt_nog_onder_de_allow_all():
    """NotIn matcht ook een pod zonder het label, en dat is de bedoeling."""
    assert matches_selector({}, _allow_all()["spec"]["podSelector"])
