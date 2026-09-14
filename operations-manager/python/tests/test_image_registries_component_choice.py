"""De registrykeuze staat bij de image, en die keuze IS de selectie (RC-187).

Drie richtingen, en ze horen alle drie te gelden voor het formulier en voor de API:

- een niet-publieke waarde materialiseert de dienstvermelding op dat component;
- de standaardwaarde (publiek) schrijft niets en haalt een bestaande vermelding weg;
- een formulier waarin niemand iets koos voegt niets toe.

Die derde is de val waar ``instructions/services.md`` voor waarschuwt: het pad-filter
materialiseert een dienst als bijwerking, dus een default wordt stilletjes een selectie.
Hier willen we dat WEL, maar alleen in de ene richting.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.forms.editables.editable import Editable
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.layout import COMPONENT_IMAGE_SLOT
from opi.forms.visualizers.providers import FilteredServiceOptionsProvider
from opi.forms.visualizers.wizard_sections import COMPONENTS_SECTION
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.image_registries.config_model import ComponentRegistryConfig
from opi.services.catalog.image_registries.editables import (
    COMPONENT_REGISTRY_EDITABLE,
    DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE,
)
from opi.services.registry import get_service
from opi.services.services import ServiceAdapter, service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType
from pydantic import ValidationError

_SVC = ServiceType.IMAGE_REGISTRIES.value
_REGISTRY = {"name": "code-overheid", "upstream": "code.overheid.nl/team"}
_COMPONENT_PATH = "components[0]/services{image-registries}/config/registry"


def _project(component_services: list[Any] | None = None) -> dict[str, Any]:
    return {
        "name": "demo",
        "services": [{"name": _SVC, "config": {"registries": [_REGISTRY]}}],
        "components": [
            {
                "name": "web",
                "image": "code.overheid.nl/team/app:1",
                "ports": {"inbound": [8080]},
                "path": [{"match": "/"}],
                "services": component_services if component_services is not None else [],
            }
        ],
    }


def _submission(registry: str | None) -> dict[str, Any]:
    """Wat het componentformulier post: de dienstenlijst zonder deze dienst (hij heeft
    geen vinkje meer) en de keuze onder de virtuele configroot."""
    component: dict[str, Any] = {
        "name": "web",
        "image": "code.overheid.nl/team/app:1",
        "ports": {"inbound": [8080]},
        "path": [{"match": "/"}],
        "services": [],
    }
    if registry is not None:
        component["_services-config"] = [{"name": _SVC, "config": {"registry": registry}}]
    return {"components": [component]}


async def _save(project: dict[str, Any], submitted: dict[str, Any]) -> list[Any]:
    processor = EditableFormProcessor()
    result, errors = await processor.process_json_submission(
        submitted, COMPONENTS_SECTION.editables, project, edit_mode=True
    )
    assert errors == {}, errors
    return result["components"][0].get("services", [])


def _chose(services: list[Any]) -> str | None:
    for entry in services:
        if service_entry_name(entry) == _SVC:
            config = service_entry_config(entry)
            return config.get("registry") if isinstance(config, dict) else None
    return None


class TestDeDrieRichtingenDoorHetFormulier:
    @pytest.mark.asyncio
    async def test_een_registry_kiezen_materialiseert_de_dienstvermelding(self) -> None:
        services = await _save(_project(), _submission("code-overheid"))
        assert _chose(services) == "code-overheid"

    @pytest.mark.asyncio
    async def test_publiek_kiezen_haalt_de_vermelding_weg(self) -> None:
        project = _project([{"name": _SVC, "config": {"registry": "code-overheid"}}])
        services = await _save(project, _submission(""))
        assert [service_entry_name(entry) for entry in services] == []

    @pytest.mark.asyncio
    async def test_niemand_koos_iets_en_er_komt_niets_bij(self) -> None:
        """De standaardwaarde is geen selectie: afwezig BETEKENT publiek."""
        services = await _save(_project(), _submission(""))
        assert services == []

    @pytest.mark.asyncio
    async def test_een_gekozen_registry_overleeft_een_opslag_die_hem_teruggeeft(self) -> None:
        """Het veld staat er altijd, dus een gewone opslag stuurt de huidige waarde mee."""
        project = _project([{"name": _SVC, "config": {"registry": "code-overheid"}}])
        services = await _save(project, _submission("code-overheid"))
        assert _chose(services) == "code-overheid"


class TestDeGrendelLos:
    """De weghaal-kant gemeten op de schrijfpoort zelf, zodat hij niet meelift op het
    feit dat de dienstenlijst bij een volledige inzending toch al opnieuw wordt gezet."""

    def test_de_lege_waarde_haalt_de_hele_vermelding_weg(self) -> None:
        data = _project([{"name": _SVC, "config": {"registry": "code-overheid"}}])
        EditableFormProcessor._write_field(COMPONENT_REGISTRY_EDITABLE, _COMPONENT_PATH, "", data)
        assert data["components"][0]["services"] == []

    def test_de_legacy_vorm_van_de_vermelding_gaat_net_zo_goed_weg(self) -> None:
        data = _project([{_SVC: {"config": {"registry": "code-overheid"}}}])
        EditableFormProcessor._write_field(COMPONENT_REGISTRY_EDITABLE, _COMPONENT_PATH, "", data)
        assert data["components"][0]["services"] == []

    def test_een_dienst_zonder_die_verklaring_houdt_zijn_lege_vermelding(self) -> None:
        """De tegenproef: normaal MARKEERT een leeg blok dat het component de dienst
        aanvinkte, en dat blijft zo. Alleen wie zegt dat zijn config de selectie is,
        verliest de vermelding met zijn laatste waarde mee."""
        data = _project([{"name": "metrics-scraper", "config": {"port": 9090}}])
        editable = Editable(yaml_path="components[*]/services{metrics-scraper}/config/port", remove_when_none=True)
        EditableFormProcessor._write_field(editable, "components[0]/services{metrics-scraper}/config/port", "", data)
        assert [service_entry_name(entry) for entry in data["components"][0]["services"]] == ["metrics-scraper"]

    def test_een_vermelding_met_meer_dan_deze_sleutel_blijft_staan(self) -> None:
        """Alleen een vermelding die niets anders meer draagt gaat weg."""
        data = _project([{"name": _SVC, "config": {"registry": "code-overheid", "iets-anders": 1}}])
        EditableFormProcessor._write_field(COMPONENT_REGISTRY_EDITABLE, _COMPONENT_PATH, "", data)
        assert [service_entry_name(entry) for entry in data["components"][0]["services"]] == [_SVC]


class TestGeenTweedeKnop:
    def test_de_dienst_staat_niet_in_de_vinkjes_van_het_componentformulier(self) -> None:
        namen = [
            option["value"]
            for option in FilteredServiceOptionsProvider(project_services=[_SVC, "metrics-scraper"]).get_options()
        ]
        assert namen == ["metrics-scraper"]

    def test_de_verklaring_staat_op_de_dienst_en_nergens_anders(self) -> None:
        assert get_service(ServiceType.IMAGE_REGISTRIES).component_selection_follows_config is True
        assert get_service(ServiceType.METRICS_SCRAPER).component_selection_follows_config is False


class TestDezelfdeRegelViaDeApi:
    def test_de_config_wissen_haalt_de_vermelding_weg(self) -> None:
        """DELETE op de componentconfig. Een vermelding die alleen nog zijn naam draagt
        zou hier niets zeggen: niet publiek, niet gekozen, niets."""
        project = _project([{"name": _SVC, "config": {"registry": "code-overheid"}}])
        removed = ServiceAdapter.remove_service_config(project, _SVC, ConfigLayer.COMPONENT, component_name="web")
        assert removed is True
        assert project["components"][0]["services"] == []

    def test_een_andere_dienst_wordt_nog_steeds_teruggezet_naar_een_kale_naam(self) -> None:
        project = _project([{"name": "metrics-scraper", "config": {"port": 9090}}])
        removed = ServiceAdapter.remove_service_config(
            project, "metrics-scraper", ConfigLayer.COMPONENT, component_name="web"
        )
        assert removed is True
        assert project["components"][0]["services"] == ["metrics-scraper"]

    def test_op_projectniveau_blijft_de_selectie_een_eigen_beslissing(self) -> None:
        """Daar vinkt de afnemer de dienst zelf aan en configureert hij hem in een eigen
        blok; wissen haalt de config weg en niet het project van de dienst af."""
        project = _project()
        removed = ServiceAdapter.remove_service_config(project, _SVC, ConfigLayer.PROJECT)
        assert removed is True
        assert project["services"] == [_SVC]

    def test_een_lege_registrynaam_is_geen_waarde_aan_de_api_deur(self) -> None:
        with pytest.raises(ValidationError):
            ComponentRegistryConfig(registry="")


class TestHetVeldStaatBijDeImage:
    def test_de_layoutknoop_noemt_het_slot_achter_image(self) -> None:
        nodes = get_service(ServiceType.IMAGE_REGISTRIES).config_component_layout()
        assert [node.slot for node in nodes] == [COMPONENT_IMAGE_SLOT]

    def test_het_veld_wacht_niet_meer_op_een_vinkje(self) -> None:
        """``depends_on``/``show_when`` op de dienstenlijst zou betekenen dat het veld
        wacht op de lijst die het zelf zet."""
        assert COMPONENT_REGISTRY_EDITABLE.depends_on is None
        assert COMPONENT_REGISTRY_EDITABLE.show_when is None
        assert COMPONENT_REGISTRY_EDITABLE.required is False


def test_de_dienst_draagt_nog_steeds_config_op_het_componentniveau() -> None:
    """Het veld verhuisde van een eigen fieldset naar het slot; de laag blijft."""
    layers = get_service(ServiceType.IMAGE_REGISTRIES).config_layers()
    assert ConfigLayer.COMPONENT in layers
    assert ConfigLayer.DEPLOYMENT_COMPONENT in layers


def test_het_projectniveau_verandert_niet() -> None:
    """Daar blijft het: dienst aanvinken, configureren in een eigen blok."""
    project = _project()
    before = copy.deepcopy(project["services"])
    section = get_service(ServiceType.IMAGE_REGISTRIES).config_form_section(ConfigLayer.PROJECT)
    assert section is not None
    assert section.visible(project) is True
    assert project["services"] == before


class TestDezelfdeRegelOpEenDeploymentComponent:
    """Daar zit de override, en zonder dit veld was die alleen met de hand te zetten.

    ``services`` is op dat niveau een DICT keyed op dienstnaam, dus de vermelding is een
    sleutel in plaats van een lijstitem -- de weghaal-kant loopt er langs het gewone
    opruimen van leeg geworden ouders.
    """

    def _deployment_project(self, registry: str | None) -> dict[str, Any]:
        component: dict[str, Any] = {"reference": "web", "image": "code.overheid.nl/team/app:1"}
        if registry is not None:
            component["services"] = {_SVC: {"config": {"registry": registry}}}
        return {
            "name": "demo",
            "services": [{"name": _SVC, "config": {"registries": [_REGISTRY]}}],
            "deployments": [{"name": "productie", "components": [component]}],
        }

    def test_een_registry_kiezen_zet_de_override(self) -> None:
        data = self._deployment_project(None)
        path = "deployments[0]/components[0]/services/image-registries/config/registry"
        EditableFormProcessor._write_field(DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE, path, "code-overheid", data)
        component = data["deployments"][0]["components"][0]
        assert component["services"][_SVC]["config"]["registry"] == "code-overheid"

    def test_de_lege_waarde_haalt_de_override_weg(self) -> None:
        data = self._deployment_project("code-overheid")
        path = "deployments[0]/components[0]/services/image-registries/config/registry"
        EditableFormProcessor._write_field(DEPLOYMENT_COMPONENT_REGISTRY_EDITABLE, path, "", data)
        assert "services" not in data["deployments"][0]["components"][0]
