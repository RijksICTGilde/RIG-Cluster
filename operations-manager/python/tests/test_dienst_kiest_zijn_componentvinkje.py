"""Een dienst zegt zelf of je hem per component aanvinkt (RC-213).

De componentkeuze toonde bijna alles wat het project had aangezet. Ze stelde een vraag,
``component_selection_follows_config``, en die dekt precies een geval: image-registries.
Voor de rest gold "staat het in de projectlijst, dan staat het in de lijst", en dat gaf
vinkjes die aantoonbaar niets deden: sleep-mode bepaalt met ``match:`` zelf welke
deployments slapen, een uitnodiging geldt voor het Keycloak-realm van het project, en de
componentkeuze van cross-domain-access zit in de regel zelf (``to.component``).

De grendel hieronder is het punt van de hele wijziging. Zonder hem staat dit over een half
jaar opnieuw scheef, want de standaard is "wel een vinkje" en niemand komt er langs.
"""

from __future__ import annotations

import pytest
from opi.forms.visualizers.providers import FilteredServiceOptionsProvider
from opi.manager import project_manager
from opi.manager.project_manager import collect_manifest_contributions
from opi.services.catalog.base import ConfigLayer, ManifestContext, ManifestContribution, offers_component_checkbox
from opi.services.registry import SERVICES
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ServiceType

#: De lagen waarop configuratie PER COMPONENT staat. Draagt een dienst hier iets, dan is
#: er per component wel degelijk iets te kiezen.
_COMPONENT_LAYERS = (ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT_COMPONENT)

#: De diensten die vandaag zeggen dat je ze niet per component aanvinkt. Uitgeschreven en
#: niet afgeleid: een lijst die zichzelf uit de registry haalt bewaakt niets.
NIET_PER_COMPONENT = {
    ServiceType.SLEEP_MODE,
    ServiceType.INVITE,
    ServiceType.CROSS_DOMAIN_ACCESS,
    ServiceType.DEPLOYMENT_HEALTH,
    ServiceType.RESOURCE_TUNING,
}


@pytest.mark.parametrize("service_type", list(SERVICES), ids=lambda s: s.value)
def test_de_declaratie_klopt_met_de_lagen(service_type: ServiceType) -> None:
    """Geen vinkje per component betekent ook geen configuratie per component.

    De twee horen bij elkaar: wie iets per component instelt, kiest hem daar ook per
    component. Vallen ze uit elkaar, dan is er een scherm dat vraagt om een instelling die
    het component nooit aanzette, of een vinkje dat nergens toe leidt.
    """
    service = SERVICES[service_type]
    draagt_componentconfig = any(layer in _COMPONENT_LAYERS for layer in service.config_layers())
    if not service.definition.selectable_per_component:
        assert not draagt_componentconfig, (
            f"{service_type.value} wordt niet per component gekozen maar draagt wel componentconfiguratie"
        )


@pytest.mark.parametrize("service_type", list(SERVICES), ids=lambda s: s.value)
def test_wie_componentconfiguratie_draagt_houdt_de_standaard(service_type: ServiceType) -> None:
    """De omkering van de regel hierboven, want alleen die vangt de fout in de andere
    richting: een dienst die ``selectable_per_component=False`` krijgt terwijl hij een
    instelling per component draagt."""
    service = SERVICES[service_type]
    if any(layer in _COMPONENT_LAYERS for layer in service.config_layers()):
        assert service.definition.selectable_per_component is True


@pytest.mark.parametrize("service_type", sorted(NIET_PER_COMPONENT, key=lambda s: s.value), ids=lambda s: s.value)
def test_de_vijf_zonder_vinkje_declareren_dat(service_type: ServiceType) -> None:
    assert SERVICES[service_type].definition.selectable_per_component is False


@pytest.mark.parametrize("service_type", list(SERVICES), ids=lambda s: s.value)
def test_de_rest_van_de_catalogus_houdt_de_standaard(service_type: ServiceType) -> None:
    """Pin de hele verzameling, niet alleen de vijf: zo wordt een zesde dienst die de
    waarde krijgt een bewuste wijziging in dit bestand en geen bijwerking."""
    if service_type not in NIET_PER_COMPONENT:
        assert SERVICES[service_type].definition.selectable_per_component is True


class TestDeAfgeleideVraag:
    """``offers_component_checkbox`` is de plek waar de twee feiten samenkomen."""

    def test_een_gewone_dienst_biedt_een_vinkje(self) -> None:
        assert offers_component_checkbox(SERVICES[ServiceType.KEYCLOAK]) is True

    def test_geen_keuze_per_component_haalt_het_vinkje_weg(self) -> None:
        assert offers_component_checkbox(SERVICES[ServiceType.SLEEP_MODE]) is False

    def test_een_keuze_die_in_het_configveld_zit_ook(self) -> None:
        """De andere reden, en die blijft apart bestaan: image-registries heeft wel
        degelijk een keuze per component, maar die zit in zijn eigen keuzeveld."""
        registries = SERVICES[ServiceType.IMAGE_REGISTRIES]
        assert registries.definition.selectable_per_component is True
        assert registries.component_selection_follows_config is True
        assert offers_component_checkbox(registries) is False


class TestDeComponentkeuze:
    """De lijst zoals een gebruiker hem op het componentformulier krijgt."""

    def _keuze(self, project_services: list[str]) -> list[str]:
        return [option["value"] for option in FilteredServiceOptionsProvider(project_services).get_options()]

    def test_de_drie_dode_vinkjes_staan_er_niet_meer_bij(self) -> None:
        """De regressie die dit alles aanleiding gaf, gemeten op een projectlijst van
        negen diensten. De drie stonden in de lijst en hun vinkje deed niets."""
        keuze = self._keuze(
            [
                "publish-on-web",
                "keycloak",
                "postgresql-database",
                "send-email",
                "sleep-mode",
                "invite",
                "cross-domain-access",
                "image-registries",
                "vlam",
            ]
        )
        assert keuze == [
            "publish-on-web",
            "keycloak",
            "postgresql-database",
            "send-email",
            "vlam",
        ]

    def test_een_dienst_die_het_project_niet_heeft_staat_er_sowieso_niet(self) -> None:
        assert self._keuze([]) == []


class TestWaarDeManifestbijdrageZijnSelectieLeest:
    """De tweede consument van de declaratie, naast de picker.

    Geen enkele dienst in de catalogus draagt vandaag ALLEBEI (geen vinkje per component
    en wel een manifestbijdrage), dus de tak is hier met een nepdienst gemeten. Zonder
    deze toets kan de tak stil verdwijnen, en dan draagt de eerstvolgende dienst die hem
    nodig heeft nergens meer bij: geen enkel component vinkt hem immers ooit aan.
    """

    class _Nep:
        """Zo veel van een dienst als ``collect_manifest_contributions`` aanraakt."""

        def __init__(self, *, selectable: bool) -> None:
            self.definition = ServiceDefinition(
                name="nep",
                description="nep",
                icon="wolk",
                color="grijs-600",
                selectable_per_component=selectable,
            )

        def manifest_activation_types(self) -> tuple[ServiceType, ...]:
            return (ServiceType.VLAM,)

        def contribute_manifest_context(self, ctx: ManifestContext) -> ManifestContribution:
            return ManifestContribution(env_vars={"NEP": "1"})

    def _ctx(self) -> ManifestContext:
        return ManifestContext(
            deployment_name="prod",
            project_data={},
            unique_name="prod-web",
            cluster="odcn-production",
            get_secret=lambda *args, **kwargs: None,
            component_def={"name": "web", "services": []},
        )

    def _bijdragen(self, *, selectable: bool, component: list[str], project: list[str]) -> list:
        nep = self._Nep(selectable=selectable)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(project_manager, "manifest_services", lambda: [nep])
            return collect_manifest_contributions(self._ctx(), component_services=component, project_services=project)

    def test_zonder_vinkje_per_component_telt_de_projectlijst(self) -> None:
        vlam = ServiceType.VLAM.value
        assert self._bijdragen(selectable=False, component=[], project=[vlam])
        assert not self._bijdragen(selectable=False, component=[vlam], project=[])

    def test_met_vinkje_per_component_telt_de_componentlijst(self) -> None:
        vlam = ServiceType.VLAM.value
        assert self._bijdragen(selectable=True, component=[vlam], project=[])
        assert not self._bijdragen(selectable=True, component=[], project=[vlam])
