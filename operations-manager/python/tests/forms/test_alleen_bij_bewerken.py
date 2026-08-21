"""Een veld dat alleen bij BEWERKEN hoort te staan, staat niet in de aanmaakwizard.

Aanleiding: "Markeer voor verwijdering" stond op de schemastap van de aanmaakwizard. Dat
veld stopt het beheer van een schema dat er AL is en laat de data in de database staan.
Terwijl je een schema aan het verzinnen bent slaat die vraag nergens op; je laat het schema
gewoon weg. Zo ook gemeld.

De vlag heet ``alleen_bij_bewerken`` en de poort staat in ``should_render_editable``.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.visualizers.bridge import should_render_editable
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.postgresql_database.visualizers import SCHEMA_MARKED, SCHEMA_POSTFIX


def _veld(*, alleen_bij_bewerken: bool) -> EditableVisualizer:
    return EditableVisualizer(
        editable=Editable(yaml_path="ergens/iets"),
        widget=WidgetType.CHECKBOX,
        label="proef",
        alleen_bij_bewerken=alleen_bij_bewerken,
    )


class TestDeVlag:
    def test_een_gemarkeerd_veld_valt_weg_bij_aanmaken(self) -> None:
        assert should_render_editable(_veld(alleen_bij_bewerken=True), {}, edit_mode=False) is False

    def test_een_gemarkeerd_veld_staat_er_bij_bewerken(self) -> None:
        assert should_render_editable(_veld(alleen_bij_bewerken=True), {}, edit_mode=True) is True

    def test_zonder_de_vlag_verandert_er_niets(self) -> None:
        veld = _veld(alleen_bij_bewerken=False)
        assert should_render_editable(veld, {}, edit_mode=False) is True
        assert should_render_editable(veld, {}, edit_mode=True) is True

    def test_de_vlag_staat_standaard_uit(self) -> None:
        """Anders zou elk veld dat hem niet noemt uit de aanmaakwizard vallen."""
        assert _veld(alleen_bij_bewerken=False).alleen_bij_bewerken is False


class TestHetSchemaveld:
    def test_markeer_voor_verwijdering_staat_niet_in_de_aanmaakwizard(self) -> None:
        assert SCHEMA_MARKED.alleen_bij_bewerken is True
        assert should_render_editable(SCHEMA_MARKED, {}, edit_mode=False) is False

    def test_maar_wel_bij_het_bewerken_van_een_bestaand_schema(self) -> None:
        assert should_render_editable(SCHEMA_MARKED, {}, edit_mode=True) is True

    def test_de_andere_schemavelden_staan_er_gewoon(self) -> None:
        """De poort mag niet de hele reeks meenemen; je moet wel een schema kunnen invullen."""
        assert should_render_editable(SCHEMA_POSTFIX, {}, edit_mode=False) is True
