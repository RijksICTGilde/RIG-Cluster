"""Tests for the display_card widget extension (Sub-part H)."""

from __future__ import annotations

from opi.forms.field import FormField
from opi.forms.widgets.lotc import LOTCWidgetAdapter


class TestRenderDisplayCard:
    def _make_field(self, **kwargs: object) -> FormField:
        """Helper to create a FormField with display_card defaults."""
        defaults: dict[str, object] = {
            "name": "test",
            "path": "test",
            "schema_type": str,
            "widget_type": "display_card",
            "label": "Test Label",
        }
        defaults.update(kwargs)
        return FormField(**defaults)  # type: ignore[arg-type]

    def test_renders_card_html(self) -> None:
        field = self._make_field(value="Some value")
        adapter = LOTCWidgetAdapter()
        html = adapter.render_display_card(field)
        assert 'data-lotc-component="card"' in html

    def test_encrypted_status_renders_success_tag(self) -> None:
        field = self._make_field(value="Versleuteld opgeslagen")
        html = LOTCWidgetAdapter().render_display_card(field)
        assert 'data-lotc-component="tag"' in html
        assert 'color="success"' in html
        # De TEKST hoort in de tag te staan. Zonder deze regel bleef hij weg: het
        # omgezette sjabloon liet het label van de tag vallen en toonde een lege chip.
        assert 'text="Versleuteld opgeslagen"' in html

    def test_configured_status_renders_success_tag(self) -> None:
        field = self._make_field(value="Geconfigureerd")
        html = LOTCWidgetAdapter().render_display_card(field)
        assert 'color="success"' in html
        assert 'text="Geconfigureerd"' in html

    def test_not_configured_renders_warning_tag(self) -> None:
        field = self._make_field(value="Niet geconfigureerd")
        html = LOTCWidgetAdapter().render_display_card(field)
        assert 'color="warning"' in html
        assert 'text="Niet geconfigureerd"' in html

    def test_plain_value_renders_span(self) -> None:
        field = self._make_field(value="age1ufgl52y9y2aum...")
        html = LOTCWidgetAdapter().render_display_card(field)
        assert "<span>age1ufgl52y9y2aum...</span>" in html

    def test_no_value_renders_empty(self) -> None:
        field = self._make_field(value=None)
        html = LOTCWidgetAdapter().render_display_card(field)
        assert 'data-lotc-component="card"' in html
        # No value html, just the card structure

    def test_custom_icon(self) -> None:
        field = self._make_field(value="test", attributes={"icon": "schild", "icon_color": "groen"})
        html = LOTCWidgetAdapter().render_display_card(field)
        assert 'name="schild"' in html
        assert 'color="groen"' in html

    def test_default_icon(self) -> None:
        field = self._make_field(value="test")
        html = LOTCWidgetAdapter().render_display_card(field)
        # De naam wordt VERTAALD voordat hij bij <nldd-icon> aankomt: "sleutel" is onze
        # eigen ROOS-naam en NLDD kent hem niet. Deze test eiste eerst de onvertaalde
        # naam, en legde daarmee vast wat RC-94 juist repareerde - twaalf plekken gaven
        # een naam uit gegevens rechtstreeks door en tekenden een lege plek.
        assert 'name="lock-closed"' in html
        assert 'name="sleutel"' not in html
        assert 'color="hemelblauw"' in html

    def test_label_rendered(self) -> None:
        field = self._make_field(label="API Sleutel", value="test")
        html = LOTCWidgetAdapter().render_display_card(field)
        assert "API Sleutel" in html

    def test_description_rendered(self) -> None:
        field = self._make_field(value="test", description="Help text here")
        html = LOTCWidgetAdapter().render_display_card(field)
        assert "Help text here" in html

    def test_html_escaping(self) -> None:
        field = self._make_field(value='<script>alert("xss")</script>')
        html = LOTCWidgetAdapter().render_display_card(field)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestRenderFieldDispatch:
    def test_display_card_dispatches(self) -> None:
        """render_field() with widget_type='display_card' calls render_display_card."""
        field = FormField(
            name="test",
            path="test",
            schema_type=str,
            widget_type="display_card",
            label="Test",
            value="Versleuteld opgeslagen",
        )
        html = LOTCWidgetAdapter().render_field(field)
        assert 'data-lotc-component="card"' in html

    def test_display_card_with_hyphen_dispatches(self) -> None:
        """Widget type 'display-card' (with hyphen) also dispatches correctly."""
        field = FormField(
            name="test",
            path="test",
            schema_type=str,
            widget_type="display-card",
            label="Test",
            value="test",
        )
        html = LOTCWidgetAdapter().render_field(field)
        assert 'data-lotc-component="card"' in html


class TestRenderKeyValueEditor:
    def _make_field(self, **kwargs: object) -> FormField:
        defaults: dict[str, object] = {
            "name": "test",
            "path": "test/aliases",
            "schema_type": str,
            "widget_type": "key_value",
            "label": "Aliassen",
            "attributes": {"kv_format": "env"},
        }
        defaults.update(kwargs)
        return FormField(**defaults)  # type: ignore[arg-type]

    def test_renders_editor_html(self) -> None:
        field = self._make_field(value="KEY=value")
        html = LOTCWidgetAdapter().render_key_value_editor(field)
        assert "kv-editor" in html
        assert "kv-toggle" in html

    def test_renders_env_toggle_active(self) -> None:
        field = self._make_field(value="")
        html = LOTCWidgetAdapter().render_key_value_editor(field)
        assert "kvToggleFormat" in html
        assert ">ENV</button>" in html
        assert ">YAML</button>" in html

    def test_renders_textarea_with_value(self) -> None:
        field = self._make_field(value="API_KEY=secret")
        html = LOTCWidgetAdapter().render_key_value_editor(field)
        assert "API_KEY=secret" in html

    def test_dispatches_via_render_field(self) -> None:
        field = self._make_field(value="X=1")
        html = LOTCWidgetAdapter().render_field(field)
        assert "kv-editor" in html

    def test_yaml_format_active(self) -> None:
        field = self._make_field(value="KEY: value", attributes={"kv_format": "yaml"})
        html = LOTCWidgetAdapter().render_key_value_editor(field)
        assert 'data-format="yaml"' in html
