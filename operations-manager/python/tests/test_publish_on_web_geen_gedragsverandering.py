"""Verhuizen is geen gedragsverandering: dezelfde hostnamen, uit beide opslagvormen.

Het plan van RC-60 zegt het met zoveel woorden: elke gerenderde hostnaam, elk ingress en elk
certificaat hoort na afloop byte-identiek te zijn, en een groene testsuite is daar geen
bewijs van. Deze poort meet het waar het te meten valt zonder cluster: de code die de
hostnamen samenstelt krijgt hetzelfde project twee keer aangeboden -- een keer met de
instellingen in de wortel van de deployment (de vorm van voor v2.7) en een keer eronder de
dienst -- en wat eruit komt moet gelijk zijn.

Dat is precies wat de verhuizing kan breken en waar geen enkele andere test naar kijkt: elke
lezer die nog rechtstreeks in de wortel keek, krijgt na de migratie stil ``None`` terug. Geen
foutmelding, geen kapotte test -- een deployment die op het verkeerde adres publiceert.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.catalog.publish_on_web.urls import public_urls_for_deployment
from opi.utils.naming import HostnameFormat, get_component_ingress_map

CLUSTER = "local"
PROJECT = "webadres"

#: De vormen waarin een deployment zijn webadres kan dragen, met dezelfde waarden. Meer dan
#: twee, want een productiebestand kan elk van de drie dienstingangen dragen: de v2.4-migratie
#: maakt ``{reference: ...}``, ``ensure_domains_config`` schrijft ``{name: ...}``, en oudere
#: bestanden hebben de geneste vorm.
_SETTINGS = {
    "base-domain": "rijksapp.nl",
    "subdomain": "wies",
    "domain-format": "component-deployment-subdomain",
    "issuer": "letsencrypt",
    "root-component": "frontend",
}


def _deployment(shape: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "productie",
        "cluster": CLUSTER,
        "namespace": PROJECT,
        "components": [{"reference": "frontend", "image": "nginx:1.25"}],
    }
    if shape == "root":
        return {**base, **_SETTINGS}
    if shape == "reference-record":
        return {**base, "services": [{"reference": "publish-on-web", "config": dict(_SETTINGS)}]}
    if shape == "name-record":
        return {**base, "services": [{"name": "publish-on-web", "config": dict(_SETTINGS)}]}
    if shape == "legacy-nested":
        return {**base, "services": [{"publish-on-web": {"config": dict(_SETTINGS)}}]}
    raise AssertionError(f"onbekende vorm: {shape}")


def _project(shape: str) -> dict[str, Any]:
    return {
        "name": PROJECT,
        "clusters": [CLUSTER],
        # Het domein moet GOEDGEKEURD zijn, anders valt elke vorm terug op het clusteradres
        # en zijn vier identieke uitkomsten geen bewijs van iets: dan meet de test dat de
        # terugval werkt, niet dat de instellingen worden gelezen.
        "services": [
            {
                "reference": "publish-on-web",
                "config": {
                    "domains": {
                        "allowed-domains": [{"domain": "rijksapp.nl", "status": "approved"}],
                        "allowed-subdomains": [
                            {"domain": "rijksapp.nl", "subdomains": [{"name": "wies", "status": "approved"}]}
                        ],
                    }
                },
            }
        ],
        "components": [
            {
                "name": "frontend",
                "type": "deployment",
                "ports": {"inbound": [8080], "outbound": [443]},
                "services": ["publish-on-web"],
            }
        ],
        "deployments": [_deployment(shape)],
    }


_SHAPES = ["root", "reference-record", "name-record", "legacy-nested"]


def _hostnames(shape: str) -> dict[str, str]:
    """De ingressmap van het component, opgebouwd zoals project_manager dat doet."""
    project = _project(shape)
    deployment = project["deployments"][0]
    return get_component_ingress_map(
        component_name="frontend",
        deployment_name=deployment["name"],
        project_name=PROJECT,
        ingress_postfix=".cluster.local",
        subdomain=get_domain_setting(deployment, DomainSetting.SUBDOMAIN),
        base_domain=get_domain_setting(deployment, DomainSetting.BASE_DOMAIN),
        hostname_format=HostnameFormat.from_domain_mode(get_domain_setting(deployment, DomainSetting.DOMAIN_MODE)),
        domain_format=get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT),
        project_data=project,
        cluster=CLUSTER,
    )


class TestDezelfdeHostnamenUitElkeOpslagvorm:
    def test_de_instellingen_bepalen_de_uitkomst_echt(self) -> None:
        """De poort van de poort: zonder dit meet de vergelijking hieronder niets.

        Vier identieke uitkomsten zeggen alleen iets als de instellingen de uitkomst ECHT
        bepalen. Zolang het domein niet is goedgekeurd valt elke vorm terug op het
        clusteradres en zijn ze identiek ook als geen enkele lezer de instellingen ziet --
        precies de fout die deze test moet vangen. Daarom eerst: de hostnaam draagt het
        gekozen subdomein en basisdomein.
        """
        hostnames = _hostnames("root")
        assert hostnames, "de oude vorm levert geen enkele hostnaam op; de meting zegt dan niets"
        assert any("wies.rijksapp.nl" in host for host in hostnames.values()), (
            f"de hostnaam volgt de instellingen niet, dus de vergelijking is leeg: {hostnames}"
        )

    @pytest.mark.parametrize("shape", [s for s in _SHAPES if s != "root"])
    def test_elke_dienstvorm_geeft_dezelfde_hostnamen_als_de_wortel(self, shape: str) -> None:
        assert _hostnames(shape) == _hostnames("root")

    @pytest.mark.parametrize("shape", _SHAPES)
    def test_de_publieke_urls_zijn_gelijk(self, shape: str) -> None:
        # De tweede plek waar dezelfde velden een adres opbouwen: de dienst-eigen
        # URL-opbouw achter de detailpagina en de API.
        from opi.handlers.project_file_handler import ProjectFileHandler

        handler = ProjectFileHandler()
        project = _project(shape)
        links = public_urls_for_deployment(project, project["deployments"][0], PROJECT, handler)
        root = _project("root")
        expected = public_urls_for_deployment(root, root["deployments"][0], PROJECT, handler)
        assert links == expected
        assert links, "geen enkele publieke URL opgebouwd; de vergelijking zegt dan niets"
