"""Wat vóór de sprong naar Keycloak 26.x in de uitrol moet staan (RC-411).

Deze toetsen lezen MANIFESTEN en geen Python, net als `test_keycloak_eigen_image.py`, en ze
lenen daar hun render-hulp. Wat hier fout kan gaan, gaat stil fout: een rolling update die
twee pods op hetzelfde schema zet, of een optie die als geregeld leest terwijl hij niets doet.
"""

import shutil

import pytest
from tests.test_keycloak_eigen_image import (
    REPO_ROOT,
    _basis_container,
    _basis_deployment,
    _env_namen,
    _rendered_container,
    _rendered_deployment,
)

#: Elke omgeving die de basis uitrolt. De strategie staat in de basis en nergens in een
#: overlay, dus dit meet wat kustomize oplevert en niet wat de basis belooft.
OMGEVINGEN = ("odcn", "odcn-staging", "sandboxed-local")

#: De vijf variabelen die RC-411 weghaalde, met per stuk de reden dat ze dood zijn.
DODE_OMGEVINGSVARIABELEN = (
    # Geen Keycloak-optie en niets in de repo leest ze.
    "ORGANIZATION",
    "KEYCLOAK_ADMIN_IMPORT_ENABLED",
    "KEYCLOAK_ADMIN_EXPORT_ENABLED",
    # Een optie van hostname v1, en 25 draait standaard v2. Gemeten op de sandbox: Keycloak
    # noemt hem in zijn deprecation-lijst, maar -1 is de documentatiestandaard en de
    # overlays zetten KC_HOSTNAME als volledige URL, waar v2 de poort uit haalt.
    "KC_HOSTNAME_PORT",
    # Een WildFly-restant: de Quarkus-distributie kent de variabele niet.
    "KC_PROXY_ADDRESS_FORWARDING",
    # Een bouwoptie in 26 (spi-<spi>-enabled), dus naast --optimized stopt Keycloak 26.8.1
    # met exit 2 op "build time options differ". Hij deed ook niets: onze endpoints hangen
    # aan realm-restapi-extension, niet aan admin-realm-restapi-extension.
    "KC_SPI_ADMIN_REALM_RESTAPI_EXTENSION_ENABLED",
)

#: De namen waarmee 25 de opstartbeheerder zette. In 26 doen ze niets meer.
OPSTARTBEHEERDER_25 = ("KEYCLOAK_ADMIN", "KEYCLOAK_ADMIN_PASSWORD")

#: De namen van 26, met dezelfde sleutels uit `keycloak-admin-credentials`.
OPSTARTBEHEERDER_26 = ("KC_BOOTSTRAP_ADMIN_USERNAME", "KC_BOOTSTRAP_ADMIN_PASSWORD")


def _kustomize_of_skip() -> None:
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize ontbreekt")


class TestDeUitrolMigreertNooitMetTweePods:
    """Upstream staat een rolling update bij een minor- of major-upgrade niet toe.

    Het schema mag pas migreren als alle oude pods uit zijn. Bij RollingUpdate (de standaard
    van Kubernetes, dus wat er zonder deze regel geldt) migreert de nieuwe pod terwijl de
    oude nog op het oude schema schrijft.
    """

    def test_de_basis_vervangt_in_plaats_van_te_rollen(self) -> None:
        assert _basis_deployment()["spec"]["strategy"] == {"type": "Recreate"}

    @pytest.mark.parametrize("omgeving", OMGEVINGEN)
    def test_geen_overlay_zet_de_rolling_update_terug(self, omgeving: str) -> None:
        _kustomize_of_skip()
        strategy = _rendered_deployment(REPO_ROOT, omgeving)["spec"].get("strategy")
        assert strategy == {"type": "Recreate"}, (omgeving, strategy)


class TestGeenDodeOptieInDeDeployment:
    """Een optie die niets doet leest als geregeld, en dat is erger dan een ontbrekende regel."""

    @pytest.mark.parametrize("naam", DODE_OMGEVINGSVARIABELEN)
    def test_de_basis_noemt_hem_niet(self, naam: str) -> None:
        assert naam not in _env_namen(_basis_container()), naam

    @pytest.mark.parametrize("naam", DODE_OMGEVINGSVARIABELEN)
    @pytest.mark.parametrize("omgeving", OMGEVINGEN)
    def test_geen_overlay_zet_hem_terug(self, omgeving: str, naam: str) -> None:
        _kustomize_of_skip()
        assert naam not in _env_namen(_rendered_container(REPO_ROOT, omgeving)), (omgeving, naam)


class TestDeOpstartbeheerderHeetZoalsKeycloak26HemKent:
    """`KEYCLOAK_ADMIN` en `KEYCLOAK_ADMIN_PASSWORD` doen in 26 niets meer.

    Ze werken alleen op een LEGE database, dus een bestaand cluster merkt er niets van; een
    verse sandbox zou zonder beheerder opkomen en dat valt pas op bij de eerste inlog.
    """

    @pytest.mark.parametrize("naam", OPSTARTBEHEERDER_25)
    def test_de_basis_noemt_de_namen_van_25_niet_meer(self, naam: str) -> None:
        assert naam not in _env_namen(_basis_container()), naam

    @pytest.mark.parametrize("naam", OPSTARTBEHEERDER_26)
    def test_de_basis_noemt_de_namen_van_26(self, naam: str) -> None:
        assert naam in _env_namen(_basis_container()), naam

    @pytest.mark.parametrize("naam", OPSTARTBEHEERDER_25)
    @pytest.mark.parametrize("omgeving", OMGEVINGEN)
    def test_geen_overlay_zet_de_oude_namen_terug(self, omgeving: str, naam: str) -> None:
        _kustomize_of_skip()
        assert naam not in _env_namen(_rendered_container(REPO_ROOT, omgeving)), (omgeving, naam)
