"""Zonder ``base-domain`` draait een deployment op het clusterdomein, ook voor de issuer.

``base-domain`` is niet verplicht. Ontbreekt het, dan valt de hostnaam terug op het
domein van het cluster; ``resolve_effective_base_domain`` doet dat en wordt daar al voor
gebruikt. De issuer-generatie in ``project_manager`` deed dat niet: die las het rauwe
``base-domain`` en sloeg alles over zodra het ontbrak.

Gevolg op fundament-poc: een project op de clusterstandaard kreeg geen Issuer en geen
certificaat, en nginx serveerde zijn eigen "Kubernetes Ingress Controller Fake
Certificate". Op clusters waar iets anders het certificaat al leverde viel dat niet op.

Rood (rauw base-domain): fundament-poc levert (None, None) en er wordt niets gegenereerd.
Groen: het clusterdomein en de issuer die daarbij in cluster_config staat.
"""

import pytest
from opi.manager.project_manager import resolve_domain_and_issuer


def _deployment(**extra: object) -> dict:
    dep: dict = {
        "name": "productie",
        "services": [{"reference": "publish-on-web", "config": {"domain-format": "component-deployment-project"}}],
    }
    dep.update(extra)
    return dep


def test_zonder_base_domain_valt_terug_op_het_cluster() -> None:
    """Het geval dat stukging: geen base-domain, wel een issuer uit de clusterconfig."""
    domein, issuer = resolve_domain_and_issuer(_deployment(), "fundament-poc")
    assert domein == "fundament-poc.rijksapp.dev"
    assert issuer == "letsencrypt"


def test_eigen_domein_wint_van_de_fallback() -> None:
    domein, issuer = resolve_domain_and_issuer(
        _deployment(**{"base-domain": "eigen.example.nl", "issuer": "letsencrypt-staging"}),
        "fundament-poc",
    )
    assert domein == "eigen.example.nl"
    assert issuer == "letsencrypt-staging"


def test_eigen_domein_zonder_issuer_krijgt_die_uit_de_clusterconfig() -> None:
    """Een domein dat het cluster kent levert zijn eigen issuer, ook zonder issuer in het bestand."""
    domein, issuer = resolve_domain_and_issuer(
        _deployment(**{"base-domain": "fundament-poc.rijksapp.dev"}), "fundament-poc"
    )
    assert domein == "fundament-poc.rijksapp.dev"
    assert issuer == "letsencrypt"


@pytest.mark.parametrize("cluster", ["odcn-production"])
def test_een_cluster_zonder_issuer_voor_zijn_eigen_domein_verandert_niet(cluster: str) -> None:
    """De fallback zet niets aan wat niet geconfigureerd is.

    Het ingress-domein van odcn staat niet in nice_url.supported_domains, dus daar komt
    geen issuer uit en verandert er niets aan het bestaande gedrag. Dat is de reden dat
    deze wijziging veilig is voor productie.
    """
    _, issuer = resolve_domain_and_issuer(_deployment(), cluster)
    assert issuer is None
