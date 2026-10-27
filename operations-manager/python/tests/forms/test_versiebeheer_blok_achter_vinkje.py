"""Het blok "Oude versies" staat achter het vinkje "Versiebeheer op de bucket" (zowel bij
minio als bij seaweedfs).

De grens geldt alleen voor een bucket met versiebeheer, dus wie het vinkje niet zet ziet
het blok niet. De poort is ``Fieldset.depends_on`` op de enable-versioning-sleutel; live
hertekenen doet ``wizard.js`` op ``[data-rerender]``, dus het vinkje draagt dat attribuut.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opi.forms.layout import Fieldset
from opi.forms.visualizers.bridge import evaluate_show_when
from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.catalog.minio import MinioStorageService
from opi.services.catalog.minio.visualizers import MINIO_ENABLE_VERSIONING
from opi.services.catalog.seaweedfs import SeaweedfsStorageService
from opi.services.catalog.seaweedfs.visualizers import SEAWEEDFS_ENABLE_VERSIONING
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from opi.forms.visualizers.sections import FormSection


def _sectie(service_type: ServiceType) -> FormSection:
    return get_service(service_type).config_form_section(ConfigLayer.PROJECT)


def _veldset(sectie: FormSection) -> Fieldset:
    veldset = next(element for element in sectie.layout or [] if isinstance(element, Fieldset))
    assert veldset.legend == "Oude versies"
    return veldset


@pytest.mark.parametrize(
    ("service_type", "klasse"),
    [
        (ServiceType.MINIO_STORAGE, MinioStorageService),
        (ServiceType.SEAWEEDFS_STORAGE, SeaweedfsStorageService),
    ],
    ids=lambda waarde: waarde.value if isinstance(waarde, ServiceType) else getattr(waarde, "__name__", ""),
)
def test_het_blok_staat_achter_het_vinkje(service_type: ServiceType, klasse: type) -> None:
    """De layout-poort verwijst naar de eigen enable-versioning-sleutel van de dienst."""
    assert isinstance(get_service(service_type), klasse), "fixture en dienst lopen uit elkaar"
    veldset = _veldset(_sectie(service_type))
    verwacht_pad = config_path(ConfigLayer.PROJECT, service_type, "config", "enable-versioning")
    assert veldset.depends_on == verwacht_pad, "het blok hangt aan een andere of geen sleutel"
    # show_when=None betekent waarheidswaarde: aan staat het blok er, uit of afwezig niet.
    assert veldset.show_when is None
    assert evaluate_show_when(True, veldset.show_when)
    assert not evaluate_show_when(None, veldset.show_when)
    assert not evaluate_show_when(False, veldset.show_when)


@pytest.mark.parametrize(
    "visualizer",
    [MINIO_ENABLE_VERSIONING, SEAWEEDFS_ENABLE_VERSIONING],
    ids=lambda vis: vis.editable.yaml_path.split("/")[1],
)
def test_het_vinkje_tekent_live_om(visualizer) -> None:
    """Zonder data-rerender laat de toggle de gebruiker in een verouderde stand kijken."""
    assert (visualizer.attributes or {}).get("data-rerender") == "true"


def _velden_van(sectie: FormSection, yaml_data: dict) -> dict:
    """De velden van de sectie zoals de renderer ze opbouwt; de interne aanroep is hier
    de wijze: de layout-poort en de veldsleutel moeten elkaar vinden (zie de test)."""
    from opi.forms.renderer import FormRenderer
    from opi.forms.widgets.lotc import LOTCWidgetAdapter

    renderer = FormRenderer(widget_adapter=LOTCWidgetAdapter())
    return renderer._build_fields_from_editables(  # type: ignore[attr-defined]
        editables=sectie.editables, yaml_data=yaml_data, errors=None, edit_mode=False
    )


@pytest.mark.parametrize("service_type", [ServiceType.MINIO_STORAGE, ServiceType.SEAWEEDFS_STORAGE])
def test_de_poort_vindt_de_sleutel_in_de_velden(service_type: ServiceType) -> None:
    """Een poort die zijn sleutel niet vindt is ALTIJD dicht, ook met versiebeheer aan.

    Die stand zou stilletjes "het blok is er nooit" betekenen; deze toets bewijst dat de
    sleutel die de layout noemt ook echt een veld van de sectie is.
    """
    from opi.forms.layout import layout_field_names

    sectie = _sectie(service_type)
    veldset = _veldset(sectie)
    yaml_data = {"services": [{"name": "x"}], service_type.value + "/dummy": {}}
    velden = _velden_van(sectie, yaml_data)
    assert veldset.depends_on in velden, (
        f"de poort zoekt {veldset.depends_on} maar de sectie kent {sorted(velden)}; "
        "blijft dat zo, dan is het blok onzichtbaar ook als het vinkje aan staat"
    )
    # en de velden die de layout noemt, staan er ook (de sweep in
    # test_service_config_layout_coverage dekt de rest).
    assert set(layout_field_names(sectie.layout)).issubset(set(velden))


@pytest.mark.parametrize("service_type", [ServiceType.MINIO_STORAGE, ServiceType.SEAWEEDFS_STORAGE])
def test_de_poort_rendert_precies_als_het_vinkje_aanstaat(service_type: ServiceType) -> None:
    """An de renderer zelf: uit is een lege streng, aan is het blok er.

    Dit is de stand die op de pagina uitkomt na elke hertekening; de testdekkende naam
    is de layout-poort plus de veldsleutel (gesorteerd in de vorige test).
    """
    from opi.forms.renderer import FormRenderer
    from opi.forms.widgets.lotc import LOTCWidgetAdapter

    sectie = _sectie(service_type)
    veldset = _veldset(sectie)
    renderer = FormRenderer(widget_adapter=LOTCWidgetAdapter())

    uit = {"services": [service_type.value]}
    aan = {"services": [{service_type.value: {"config": {"enable-versioning": True}}}]}
    velden_uit = _velden_van(sectie, uit)
    velden_aan = _velden_van(sectie, aan)

    assert renderer._render_layout_element(veldset, velden_uit, uit) == ""  # type: ignore[attr-defined]
    assert "Oude versies" in renderer._render_layout_element(veldset, velden_aan, aan)  # type: ignore[attr-defined]
