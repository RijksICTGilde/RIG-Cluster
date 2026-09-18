"""De benoemde plek in de componentvorm.

Het formulier bepaalt WAAR een plek zit, de dienst bepaalt WAT erin komt. Een dienst die
geen plek noemt landt nog steeds onderaan, en dat is wat de bestaande diensten doen.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.forms.layout import COMPONENT_IMAGE_SLOT, Fieldset, layout_field_names
from opi.forms.visualizers import wizard_sections
from opi.forms.visualizers.wizard_sections import COMPONENTS_SECTION, _service_component_layouts
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType


class _StubService:
    """Een dienst met twee layoutknopen: een in het slot en een zonder plek."""

    config_component_order = 99

    def __init__(self) -> None:
        self.in_slot = Fieldset(legend="In het slot", slot=COMPONENT_IMAGE_SLOT, children=["a"])
        self.onderaan = Fieldset(legend="Onderaan", children=["b"])

    def config_component_layout(self) -> list[Any]:
        return [self.in_slot, self.onderaan]


@pytest.fixture
def alleen_de_stub(monkeypatch: pytest.MonkeyPatch) -> _StubService:
    """Vervang de catalogus door precies een dienst, zodat de meting over hem gaat."""
    stub = _StubService()
    monkeypatch.setattr(wizard_sections, "get_service", lambda _service_type: stub)
    return stub


class TestHetSlot:
    def test_een_knoop_met_een_slot_landt_in_dat_slot(self, alleen_de_stub: _StubService) -> None:
        assert _service_component_layouts(COMPONENT_IMAGE_SLOT) == [alleen_de_stub.in_slot] * len(ServiceType)

    def test_een_knoop_zonder_slot_blijft_onderaan(self, alleen_de_stub: _StubService) -> None:
        """``slot=None`` is het bestaande gedrag en verzamelt alleen de ongeplaatste knopen."""
        verzameld = _service_component_layouts()
        assert alleen_de_stub.onderaan in verzameld
        assert alleen_de_stub.in_slot not in verzameld

    def test_elke_knoop_hoort_bij_precies_een_plek(self, alleen_de_stub: _StubService) -> None:
        """Samen leveren de twee aanroepen elke knoop op, en geen enkele twee keer."""
        alles = _service_component_layouts() + _service_component_layouts(COMPONENT_IMAGE_SLOT)
        assert sorted(id(node) for node in alles) == sorted(
            id(node) for _ in ServiceType for node in alleen_de_stub.config_component_layout()
        )


class TestDeBestaandeDienstenStaanWaarZeStonden:
    def test_metrics_scraper_staat_nog_onderaan_en_niet_in_het_slot(self) -> None:
        """De dienst die geen plek noemt hoort bij de staart van het componentformulier."""
        nodes = get_service(ServiceType.METRICS_SCRAPER).config_component_layout()
        assert [node.slot for node in nodes] == [None]
        legends = [getattr(node, "legend", None) for node in _service_component_layouts()]
        assert "Prometheus metrics scraper configuratie" in legends

    def test_de_identificatie_fieldset_noemt_de_plek_achter_image(self) -> None:
        """De plek zit tussen ``image`` en ``command``, want waar een image vandaan komt
        is een eigenschap van dat veld."""
        sequence = COMPONENTS_SECTION.layout[0]
        identificatie = sequence.child_layout[0]
        assert identificatie.legend == "Identificatie"
        namen = [child for child in identificatie.children if isinstance(child, str)]
        assert namen == ["name", "image", "command"]

    def test_de_gesloten_knopen_tekenen_hun_velden_in_de_identificatie(self) -> None:
        """Wat er in het slot landt hoort ook echt bij die fieldset getekend te worden."""
        sequence = COMPONENTS_SECTION.layout[0]
        identificatie = sequence.child_layout[0]
        in_het_slot = _service_component_layouts(COMPONENT_IMAGE_SLOT)
        for node in in_het_slot:
            assert layout_field_names(node) <= layout_field_names(identificatie)
