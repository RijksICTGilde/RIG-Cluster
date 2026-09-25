"""De issuer komt uit het domein, ook als niemand hem in het bestand zette.

Het projectbestand van ``mzs-3ik`` had voor deployment ``site-admin`` een goedgekeurd
``base-domain: rijks.app`` en geen ``issuer``, want de vier schrijfacties liepen via de
configure-service-taak: die weg draait geen enkele formuliergenerator, en geen dienst vult
dit veld in. Zonder issuer zet de ingress-template geen cert-manager-annotatie en geen
``secretName``, dus cert-manager maakte niets aan en de router serveerde zijn eigen
wildcard op ``moza-site.rijks.app``. Er faalde nergens iets: de manifests waren geldig en
ArgoCD stond groen.

Daarom leidt de manifestgeneratie de issuer nu zelf af. Deze test legt dat vast, en de
bronwacht onderaan legt vast dat niemand buiten de dienst het veld nog rauw leest -- dat is
precies de vorm waarin dit mankement terugkomt: een nieuwe leesplek die het ontbreken van
het veld voor "geen certificaat nodig" aanziet.
"""

from __future__ import annotations

import pathlib
import re

import opi
import pytest
from opi.services.catalog.publish_on_web.issuer import derive_issuer, effective_issuer

_CLUSTER = "odcn-production"

#: Het projectblok dat ``rijks.app`` met subdomein ``moza-site`` toegekend heeft gekregen,
#: zoals het na de goedkeuring van 20 september in ``mzs-3ik.yaml`` staat.
_APPROVED_DOMAINS = {
    "allowed-domains": [{"domain": "rijks.app", "status": "approved"}],
    "allowed-subdomains": [
        {"domain": "rijks.app", "subdomains": [{"name": "moza-site", "status": "approved"}]}
    ],
}


def _deployment(**config: object) -> dict[str, object]:
    """Een deployment met het publish-on-web-configblok waar de velden horen."""
    return {"name": "site-admin", "services": [{"reference": "publish-on-web", "config": dict(config)}]}


class TestDeAfgeleideIssuer:
    def test_een_goedgekeurd_platformdomein_zonder_issuer_krijgt_letsencrypt(self):
        """Het geval mzs-3ik/site-admin: goedgekeurd domein, veld afwezig."""
        project_data = {"domains": _APPROVED_DOMAINS}
        deployment = _deployment(**{"base-domain": "rijks.app", "subdomain": "moza-site"})

        assert effective_issuer(project_data, deployment, _CLUSTER) == "letsencrypt"

    def test_een_opgeslagen_issuer_wint(self):
        """Het veld blijft een override, ook als de afleiding iets anders zou zeggen."""
        project_data = {"domains": _APPROVED_DOMAINS}
        deployment = _deployment(
            **{"base-domain": "rijks.app", "subdomain": "moza-site", "issuer": "eigen-issuer"}
        )

        assert effective_issuer(project_data, deployment, _CLUSTER) == "eigen-issuer"

    def test_zonder_eigen_domein_geen_issuer(self):
        """Het clusteradres is gedekt door het platformcertificaat."""
        deployment = _deployment(**{"domain-format": "component-deployment-project"})

        assert effective_issuer({}, deployment, _CLUSTER) is None

    def test_een_niet_goedgekeurd_domein_krijgt_geen_issuer(self):
        """De hostnaam valt dan terug op het clusteradres.

        Een issuer voor een domein dat niemand aan dit project toekende zou cert-manager op
        een uitdaging zetten voor een naam die de ingress niet bedient. Dat is de stand van
        ``hwmaw-ovh``: een basisdomein in het bestand, geen goedkeuring.
        """
        deployment = _deployment(**{"base-domain": "rijks.app", "subdomain": "moza-site"})

        assert effective_issuer({}, deployment, _CLUSTER) is None

    def test_een_niet_goedgekeurd_subdomein_krijgt_geen_issuer(self):
        """``rijks.app`` heeft restricted subdomains, dus de naam telt ook mee."""
        project_data = {"domains": {"allowed-domains": [{"domain": "rijks.app", "status": "approved"}]}}
        deployment = _deployment(**{"base-domain": "rijks.app", "subdomain": "niet-gevraagd"})

        assert effective_issuer(project_data, deployment, _CLUSTER) is None

    def test_een_eigen_domein_volgt_het_projectblok(self):
        """Een domein dat het cluster niet aanbiedt mag zijn eigen issuer noemen."""
        project_data = {
            "domains": {
                "allowed-domains": [{"domain": "mijn-app.nl", "issuer": "custom-issuer", "status": "approved"}]
            }
        }
        deployment = _deployment(**{"base-domain": "mijn-app.nl"})

        assert effective_issuer(project_data, deployment, _CLUSTER) == "custom-issuer"

    def test_de_afleiding_negeert_de_goedkeuring(self):
        """``derive_issuer`` antwoordt over het domein, niet over de toekenning.

        De portal schrijft het veld op het moment dat de gebruiker het domein kiest, dus
        vóór de goedkeuring. Zou de afleiding daar op de goedkeuring wachten, dan bleef het
        veld leeg en kwam er na de goedkeuring niets dat hem opnieuw berekent.
        """
        deployment = _deployment(**{"base-domain": "rijks.app", "subdomain": "moza-site"})

        assert derive_issuer({}, deployment, _CLUSTER) == "letsencrypt"


# ---------------------------------------------------------------------------
# Bronwacht: één weg naar de issuer
# ---------------------------------------------------------------------------

_OPI_ROOT = pathlib.Path(opi.__file__).parent
#: De dienst zelf IS de autoriteit: ``issuer.py`` leest het opgeslagen veld.
_OWNER = _OPI_ROOT / "services" / "catalog" / "publish_on_web"

_RAW_READ = re.compile(r"get_domain_setting\([^)]*DomainSetting\.ISSUER")


def _raw_issuer_reads() -> list[str]:
    found: list[str] = []
    for path in sorted(_OPI_ROOT.rglob("*.py")):
        if path.is_relative_to(_OWNER):
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if _RAW_READ.search(line):
                found.append(f"{path.relative_to(_OPI_ROOT)}:{number}: {line.strip()}")
    return found


def test_niemand_buiten_de_dienst_leest_het_issuer_veld_rauw():
    """Elke leesplek gaat via ``effective_issuer``, dus ook zonder veld in het bestand."""
    violations = _raw_issuer_reads()
    assert not violations, "Lees de issuer via effective_issuer():\n" + "\n".join(violations)


@pytest.mark.parametrize("helper", [derive_issuer, effective_issuer])
def test_de_helpers_staan_in_het_dienstpakket(helper):
    """Anders ontstaat er een tweede afleiding die van de eerste af kan drijven."""
    assert helper.__module__ == "opi.services.catalog.publish_on_web.issuer"
