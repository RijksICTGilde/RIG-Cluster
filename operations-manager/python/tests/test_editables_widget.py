"""Tests for the display_card widget extension (Sub-part H)."""

from __future__ import annotations

from opi.forms.field import FormField
from opi.forms.widgets.roos import ROOSWidgetAdapter


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
        adapter = ROOSWidgetAdapter()
        html = adapter.render_display_card(field)
        assert "<c-card" in html
        assert "outline" in html

    def test_encrypted_status_renders_success_tag(self) -> None:
        field = self._make_field(value="Versleuteld opgeslagen")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert '<c-tag type="success"' in html
        assert "Versleuteld opgeslagen" in html

    def test_configured_status_renders_success_tag(self) -> None:
        field = self._make_field(value="Geconfigureerd")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert '<c-tag type="success"' in html

    def test_not_configured_renders_warning_tag(self) -> None:
        field = self._make_field(value="Niet geconfigureerd")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert '<c-tag type="warning"' in html

    def test_plain_value_renders_span(self) -> None:
        field = self._make_field(value="age1ufgl52y9y2aum...")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert "rvo-text--subtle" in html
        assert "age1ufgl52y9y2aum..." in html

    def test_no_value_renders_empty(self) -> None:
        field = self._make_field(value=None)
        html = ROOSWidgetAdapter().render_display_card(field)
        assert "<c-card" in html
        # No value html, just the card structure

    def test_custom_icon(self) -> None:
        field = self._make_field(value="test", attributes={"icon": "schild", "icon_color": "groen"})
        html = ROOSWidgetAdapter().render_display_card(field)
        assert 'icon="schild"' in html
        assert 'color="groen"' in html

    def test_default_icon(self) -> None:
        field = self._make_field(value="test")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert 'icon="sleutel"' in html
        assert 'color="blauw"' in html

    def test_label_rendered(self) -> None:
        field = self._make_field(label="API Sleutel", value="test")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert "API Sleutel" in html

    def test_description_rendered(self) -> None:
        field = self._make_field(value="test", description="Help text here")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert "Help text here" in html

    def test_html_escaping(self) -> None:
        field = self._make_field(value='<script>alert("xss")</script>')
        html = ROOSWidgetAdapter().render_display_card(field)
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
        html = ROOSWidgetAdapter().render_field(field)
        assert "<c-card" in html

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
        html = ROOSWidgetAdapter().render_field(field)
        assert "<c-card" in html
