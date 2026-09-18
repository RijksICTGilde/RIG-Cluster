"""De domeinkeuze in het zelfbedieningsportaal komt uit het ``domains``-blok.

``get_cluster_base_domains_for_template`` pakt dat blok met ``.get("domains", {})``. Een
verkeerde sleutel geeft daar geen fout maar een lege lijst per cluster, en dan biedt het
portaal geen enkel domein aan terwijl de suite groen blijft. Daarom toetsen we hier de
domeinen zelf, en het label dat de twee lokale namen krijgen.
"""

from unittest.mock import patch

from opi.core.cluster_config import CLUSTER_CONFIG, get_supported_domain_names
from opi.web.router_self_service import get_cluster_base_domains_for_template


class TestClusterBaseDomainsForTemplate:
    def test_every_cluster_is_present(self) -> None:
        assert set(get_cluster_base_domains_for_template()) == set(CLUSTER_CONFIG)

    def test_production_offers_its_own_domains(self) -> None:
        waarden = [o["value"] for o in get_cluster_base_domains_for_template()["odcn-production"]]

        assert "rijks.app" in waarden
        assert waarden == get_supported_domain_names("odcn-production")

    def test_local_offers_its_own_domains(self) -> None:
        waarden = [o["value"] for o in get_cluster_base_domains_for_template()["local"]]

        assert waarden == get_supported_domain_names("local")

    def test_label_equals_the_domain_by_default(self) -> None:
        for optie in get_cluster_base_domains_for_template()["odcn-production"]:
            assert optie["label"] == optie["value"]

    def test_the_two_local_names_say_they_are_local(self) -> None:
        labels = {o["value"]: o["label"] for o in get_cluster_base_domains_for_template()["local"]}

        assert labels["kind"] == "kind (lokaal)"
        assert labels["local"] == "local (lokaal)"

    def test_cluster_without_domains_block_gets_an_empty_list(self) -> None:
        """Een cluster dat geen eigen domeinen aanbiedt hoort erbij te staan, leeg."""
        with patch.dict(CLUSTER_CONFIG, {"cluster-zonder-domeinen": {"ingress_postfix": ".voorbeeld.nl"}}):
            resultaat = get_cluster_base_domains_for_template()

        assert resultaat["cluster-zonder-domeinen"] == []
