"""Een verborgen veld hoort niet in de samenvatting van een wizardstap.

DE MELDING

Op de stap Bijlagen stond onder het kopje "Bijlagen" een rauwe Python-dump van de hele
dienstenlijst, inclusief de AGE-blokken van het realm-wachtwoord en de gedeelde OTP.
Versleuteld, dus geen leesbaar geheim, maar het hoort niet op het scherm van een
samenvatting en het maakt die samenvatting onleesbaar.

DE OORZAAK

De bijlagensectie draagt een VERBORGEN veld met ``yaml_path="services"`` mee, zodat het
uploadscherm weet welke diensten aanstaan zonder de dienstenlijst bij het opslaan te
herschrijven (zie ``AttachmentsService.config_form_section``). De samenvatting liep over
alle velden van de sectie en formatteerde ook dat veld.

DE REGEL

Een verborgen veld heeft de gebruiker nooit ingevuld, dus het hoort nooit in een
samenvatting. Dat geldt voor elke drager, ook de volgende die iemand toevoegt, en daarom
staat de uitzondering op het widgettype en niet op de naam van dit ene veld.
"""

from __future__ import annotations

from typing import Any

from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.visualizers.sections import FormSection
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.web.router_wizard import _build_section_summary

AGE_BLOK = "-----BEGIN AGE ENCRYPTED FILE-----\nYWdlLWVuY3J5cHRpb24ub3JnL3Yx\n-----END AGE ENCRYPTED FILE-----"


def _sectie_met_drager() -> FormSection:
    """Een sectie zoals Bijlagen: een verborgen drager plus een gewoon veld."""
    drager = EditableVisualizer(
        editable=Editable(yaml_path="services"),
        widget=WidgetType.HIDDEN,
        label="",
        readonly=True,
    )
    zichtbaar = EditableVisualizer(
        editable=Editable(yaml_path="display-name"),
        widget=WidgetType.TEXT,
        label="Naam",
    )
    return FormSection(section_id="attachments", title="Bijlagen", editables=[drager, zichtbaar])


def _gegevens() -> dict[str, Any]:
    return {
        "display-name": "Mijn project",
        "services": [
            "publish-on-web",
            {"name": "keycloak", "config": {"realms": [{"realm": "demo", "password": AGE_BLOK}]}},
        ],
    }


def test_de_verborgen_drager_staat_niet_in_de_samenvatting() -> None:
    html = _build_section_summary(_sectie_met_drager(), _gegevens())

    assert "keycloak" not in html
    assert "publish-on-web" not in html


def test_geen_versleuteld_blok_op_het_scherm() -> None:
    """De reden dat dit meer is dan een schoonheidsfoutje."""
    html = _build_section_summary(_sectie_met_drager(), _gegevens())

    assert "BEGIN AGE ENCRYPTED FILE" not in html


def test_het_zichtbare_veld_blijft_wel_staan() -> None:
    """De uitzondering mag niet de halve samenvatting meenemen."""
    html = _build_section_summary(_sectie_met_drager(), _gegevens())

    assert "Mijn project" in html
