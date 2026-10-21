"""Of de lader elke NetworkPolicy in de repo kent.

Een nieuwe policy die hier niet opduikt valt stil buiten de matrix, en dan is een
groene matrix geen uitspraak over het beleid dat echt draait.
"""

from __future__ import annotations

from tests.netpol.policies import (
    PLATFORM_POLICY_SOURCES,
    REPO_ROOT,
    TEMPLATE_SOURCES,
    declares_network_policy,
    discovered_policy_files,
    discovered_template_files,
    platform_policies,
)


def test_de_lader_kent_elk_networkpolicy_bestand():
    onbekend = discovered_policy_files() - set(PLATFORM_POLICY_SOURCES)
    assert not onbekend, (
        "deze bestanden declareren een NetworkPolicy maar staan niet in "
        f"PLATFORM_POLICY_SOURCES: {sorted(onbekend)}. Voeg ze toe met de namespace "
        "waarin productie ze krijgt, of met None als geen Application ze uitrolt."
    )


def test_elke_bekende_bron_bestaat_nog():
    verdwenen = [p for p in PLATFORM_POLICY_SOURCES if not (REPO_ROOT / p).exists()]
    assert not verdwenen, f"deze bronnen staan in de lader maar niet meer in de repo: {verdwenen}"


def test_de_lader_kent_elke_netpol_template():
    gevonden = discovered_template_files()
    onbekend = gevonden - set(TEMPLATE_SOURCES)
    assert not onbekend, (
        "deze templates renderen een NetworkPolicy maar staan niet in TEMPLATE_SOURCES: "
        f"{sorted(onbekend)}. Zet erbij of de matrix hem rendert, en zo niet waarom niet."
    )
    verdwenen = set(TEMPLATE_SOURCES) - gevonden
    assert not verdwenen, f"deze templates staan in de lader maar niet meer in de repo: {sorted(verdwenen)}"


def test_welke_templates_de_matrix_rendert():
    gerenderd = {naam for naam, in_matrix in TEMPLATE_SOURCES.items() if in_matrix}
    assert gerenderd == {
        "tenant-baseline-network-policy.yaml.jinja",
        "network-policy.yaml.jinja",
    }


def test_de_uitgerolde_policies_hebben_een_namespace():
    for policy in platform_policies():
        assert policy["metadata"].get("namespace"), f"{policy['metadata']['name']} heeft geen namespace"


def test_een_appproject_met_networkpolicy_in_de_whitelist_is_geen_policy():
    appproject = """
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: zad-platform
spec:
  namespaceResourceWhitelist:
    - group: networking.k8s.io
      kind: NetworkPolicy
"""

    assert not declares_network_policy(appproject)
    assert not [p for p in discovered_policy_files() if "appproject" in p]
