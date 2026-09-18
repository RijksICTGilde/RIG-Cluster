"""De domeinkeuze in het zelfbedieningsportaal komt uit het ``domains``-blok.

``get_cluster_base_domains_for_template`` pakt dat blok met ``.get("domains", {})``: bij een
verkeerde sleutel biedt het portaal geen enkel domein meer aan, zonder fout en met een groene
suite. Daarom toetsen we hier de domeinen zelf.
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
        with patch.dict(CLUSTER_CONFIG, {"cluster-zonder-domeinen": {"ingress_postfix": ".voorbeeld.nl"}}):
            resultaat = get_cluster_base_domains_for_template()

        assert resultaat["cluster-zonder-domeinen"] == []
