"""Het deployment-verzoek wijst naar de keuzelijsten die het nodig heeft.

DE MELDING (zad-cli)

"``base_domain`` en ``domain-format`` staan half in twee schema's. De ``x-choices-source``
zit op de publish-on-web-config, de ``enum`` op het deployment-verzoek, en ``deployment
create`` heeft ze allebei nodig."

WAT ER WAS

``annotate_config_choices`` plakt ``x-choices-source`` uitsluitend op de PUT-route van een
service-config, dus ``UpsertDeploymentRequest`` -- het model waar ``deployment create``
naartoe post -- kreeg er niets van. ``domain_format`` had daar wel zijn ``enum``, maar
``base_domain`` had noch een enum noch een verwijzing: alleen de zin "Must be a
cluster-supported domain", die bovendien niet klopte. Een eigen domein IS een geldige
waarde, dat is de hele reden dat ``custom-domain-certificates`` bestaat.

DE KEUZE

De verzameling wordt niet herhaald -- dat is precies de fout die twee schema's uit elkaar
laat lopen. De beschrijving zegt waar de lijst staat, in alle drie de verzoekmodellen die
deze velden dragen, uit één constante zodat ze niet opnieuw kunnen splitsen. Het
mechanisme dat ``x-choices-source`` ook op een handgeschreven verzoekmodel zet, is
vervolgwerk en een beslissing van de eigenaar.
"""

from __future__ import annotations

from typing import Any

import pytest

CLUSTERS_ENDPOINT = "/api/v2/projects/{project_name}/clusters"

# Alleen dit model komt in het OpenAPI-document terecht; de twee andere verzoekmodellen met
# dezelfde velden hangen aan routes die niet in de spec staan. Ze delen wel dezelfde
# constanten, zodat de teksten niet opnieuw uit elkaar kunnen lopen.
MODELLEN_MET_DOMEINVELDEN = ["UpsertDeploymentRequest"]


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    from opi.server import app

    return app.openapi()


def _veld(spec: dict[str, Any], model: str, veld: str) -> dict[str, Any]:
    return spec["components"]["schemas"][model]["properties"][veld]


class TestBaseDomain:
    @pytest.mark.parametrize("model", MODELLEN_MET_DOMEINVELDEN)
    def test_wijst_naar_het_endpoint_met_de_domeinen(self, spec: dict[str, Any], model: str) -> None:
        beschrijving = _veld(spec, model, "base_domain")["description"]

        assert CLUSTERS_ENDPOINT in beschrijving

    @pytest.mark.parametrize("model", MODELLEN_MET_DOMEINVELDEN)
    def test_zegt_dat_een_eigen_domein_mag(self, spec: dict[str, Any], model: str) -> None:
        """De oude tekst zei het omgekeerde en weersprak daarmee het configmodel."""
        beschrijving = _veld(spec, model, "base_domain")["description"]

        assert "domain of your own" in beschrijving
        assert "Must be a cluster-supported domain" not in beschrijving

    @pytest.mark.parametrize("model", MODELLEN_MET_DOMEINVELDEN)
    def test_noemt_de_goedkeuring_die_op_een_keuze_volgt(self, spec: dict[str, Any], model: str) -> None:
        beschrijving = _veld(spec, model, "base_domain")["description"]

        assert "approval" in beschrijving
        assert "default-domain" in beschrijving

    def test_blijft_een_open_veld_zonder_enum(self, spec: dict[str, Any]) -> None:
        """Een enum hier zou een eigen domein weigeren; de lijst hoort achter het endpoint."""
        veld = _veld(spec, "UpsertDeploymentRequest", "base_domain")

        assert "enum" not in str(veld.get("anyOf", veld))


class TestDomainFormat:
    @pytest.mark.parametrize("model", MODELLEN_MET_DOMEINVELDEN)
    def test_wijst_naar_de_config_met_dezelfde_keuzes(self, spec: dict[str, Any], model: str) -> None:
        beschrijving = _veld(spec, model, "domain_format")["description"]

        assert "publish-on-web" in beschrijving

    def test_houdt_zijn_eigen_gesloten_verzameling(self, spec: dict[str, Any]) -> None:
        """De enum is hier wel op zijn plaats: die verzameling is echt gesloten."""
        veld = _veld(spec, "UpsertDeploymentRequest", "domain_format")
        enum_waarden = [w for tak in veld["anyOf"] for w in tak.get("enum", [])]

        assert "component-deployment-project" in enum_waarden
