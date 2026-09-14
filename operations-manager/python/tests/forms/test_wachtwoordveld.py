"""Het wachtwoordveld: een geheim dat niet als gewone tekst op het scherm staat.

``WidgetType`` kende geen PASSWORD, dus elk geheim in een formulier (het token van een eigen
registry als eerste) stond leesbaar in beeld. De widget is generiek: een tekstveld met
``type="password"``, en verder gedraagt hij zich als tekst -- ook in de brug, waar een
tekstveld ``converter.read()`` krijgt. Zou hij daar ``view()`` krijgen, dan toonde het veld
de menselijke weergave van het opgeslagen geheim en schreef een ongewijzigde opslag die
weergave als NIEUW geheim terug.
"""

from __future__ import annotations

import re
from typing import Any

from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.field import FormField
from opi.forms.visualizers.bridge import editable_to_form_field
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.forms.widgets.lotc import LOTCWidgetAdapter


def _veld(widget_type: str) -> FormField:
    return FormField(name="token", path="a/token", schema_type=str, widget_type=widget_type, label="Token", value="x")


def _invoertype(html: str) -> str | None:
    tag = re.search(r"<nldd-text-field\b[^>]*>", html)
    assert tag is not None, f"geen <nldd-text-field> gerenderd:\n{html}"
    gevonden = re.search(r'\btype="([^"]*)"', tag.group(0))
    return gevonden.group(1) if gevonden else None


def test_een_wachtwoordveld_rendert_afgeschermd() -> None:
    assert _invoertype(LOTCWidgetAdapter().render_field(_veld(WidgetType.PASSWORD.value))) == "password"


def test_een_tekstveld_blijft_tekst() -> None:
    """De tegenproef: de afscherming hoort bij de widget, niet bij het sjabloon."""
    assert _invoertype(LOTCWidgetAdapter().render_field(_veld(WidgetType.TEXT.value))) == "text"


class _Omkeerder:
    """Een converter die laat zien of de brug ``read()`` of ``view()`` aanroept."""

    def read(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return f"read:{value}"

    def view(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return f"view:{value}"

    def write(self, value: Any, context_data: dict[str, Any] | None = None) -> Any:
        return value


def test_de_brug_leest_een_wachtwoordveld_als_bewerkbare_waarde() -> None:
    visualizer = EditableVisualizer(
        editable=Editable(yaml_path="token", converter=_Omkeerder()),  # type: ignore[arg-type]
        widget=WidgetType.PASSWORD,
        label="Token",
    )
    assert editable_to_form_field(visualizer, {"token": "opgeslagen"}).value == "read:opgeslagen"


def test_het_registrytoken_staat_op_het_wachtwoordveld() -> None:
    from opi.services.catalog.image_registries.visualizers import REGISTRY_PASSWORD

    assert REGISTRY_PASSWORD.widget is WidgetType.PASSWORD
