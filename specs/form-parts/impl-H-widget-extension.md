# Sub-part H: Widget Extension

**Layer:** 2 (no internal dependencies, but Layer 0 should be done)
**Files to modify:**
- `opi/forms/widgets/base.py`
- `opi/forms/widgets/roos.py`

**Files to create:**
- `tests/test_editables_widget.py`

**Root directory:** `/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/`

---

## Overview

Add the `display-card` widget type to the widget adapter system. This widget renders read-only status cards for encrypted fields, configuration status, and other display-only information.

## Changes to base.py

### 1. Add abstract method

Add after `render_service_cards` (around line 76):

```python
@abstractmethod
def render_display_card(self, field: "FormField") -> str:
    """Render a read-only display card for status/encrypted fields."""
```

### 2. Add to dispatch dict

In the `render_field()` method, add to the `render_methods` dict (around line 226):

```python
render_methods = {
    "text": self.render_text,
    "textarea": self.render_textarea,
    "select": self.render_select,
    "checkbox": self.render_checkbox,
    "checkbox_group": self.render_checkbox_group,
    "radio": self.render_radio,
    "number": self.render_number,
    "date": self.render_date,
    "datetime": self.render_date,
    "file": self.render_file,
    "hidden": self.render_hidden,
    "password": self.render_text,
    "service_cards": self.render_service_cards,
    "display_card": self.render_display_card,   # <-- ADD THIS
}
```

**Note:** The dispatch normalizes hyphens to underscores, so `display-card` in the editable becomes `display_card` in the dispatch dict.

---

## Changes to roos.py

Add the concrete `render_display_card` method to `ROOSWidgetAdapter`.

```python
def render_display_card(self, field: "FormField") -> str:
    """
    Render a read-only display card using ROOS c-card component.

    Used for encrypted fields, configuration status, and other
    display-only information. The field.value should already be
    a display string (via converter.view()).

    Icon and color can be customized via field.attributes:
    - attributes["icon"] — ROOS icon name (default: "sleutel")
    - attributes["icon_color"] — ROOS color (default: "blauw")
    """
    icon = field.attributes.get("icon", "sleutel")
    icon_color = field.attributes.get("icon_color", "blauw")

    value_html = ""
    if field.value:
        value_str = self.escape_html(str(field.value))
        # Detect status messages from EncryptedDisplayConverter
        if value_str in ("Versleuteld opgeslagen", "Geconfigureerd"):
            value_html = f'<c-tag type="success" size="sm">{value_str}</c-tag>'
        elif value_str == "Niet geconfigureerd":
            value_html = f'<c-tag type="warning" size="sm">{value_str}</c-tag>'
        else:
            # Plain display value (e.g., truncated key)
            value_html = (
                f'<span class="rvo-text--sm rvo-text--subtle">{value_str}</span>'
            )

    description_html = ""
    if field.description:
        description_html = self._render_helper_text(field.description)

    return (
        f'<c-card padding="md" outline>\n'
        f'    <c-layout-flow gap="xs">\n'
        f'        <div class="rvo-display-field__header">\n'
        f'            <c-icon icon="{self.escape_html(icon)}" '
        f'size="md" color="{self.escape_html(icon_color)}" />\n'
        f'            <span class="utrecht-form-label">'
        f'{self.escape_html(field.label)}</span>\n'
        f"        </div>\n"
        f"        {value_html}\n"
        f"        {description_html}\n"
        f"    </c-layout-flow>\n"
        f"</c-card>"
    )
```

---

## Tests: test_editables_widget.py

```python
from opi.forms.field import FormField
from opi.forms.widgets.roos import ROOSWidgetAdapter


class TestRenderDisplayCard:
    def _make_field(self, **kwargs) -> FormField:
        """Helper to create a FormField with display_card defaults."""
        defaults = {
            "name": "test",
            "path": "test",
            "schema_type": str,
            "widget_type": "display_card",
            "label": "Test Label",
        }
        defaults.update(kwargs)
        return FormField(**defaults)

    def test_renders_card_html(self):
        field = self._make_field(value="Some value")
        adapter = ROOSWidgetAdapter()
        html = adapter.render_display_card(field)
        assert "<c-card" in html
        assert "outline" in html

    def test_encrypted_status_renders_success_tag(self):
        field = self._make_field(value="Versleuteld opgeslagen")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert '<c-tag type="success"' in html
        assert "Versleuteld opgeslagen" in html

    def test_configured_status_renders_success_tag(self):
        field = self._make_field(value="Geconfigureerd")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert '<c-tag type="success"' in html

    def test_not_configured_renders_warning_tag(self):
        field = self._make_field(value="Niet geconfigureerd")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert '<c-tag type="warning"' in html

    def test_plain_value_renders_span(self):
        field = self._make_field(value="age1ufgl52y9y2aum...")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert "rvo-text--subtle" in html
        assert "age1ufgl52y9y2aum..." in html

    def test_no_value_renders_empty(self):
        field = self._make_field(value=None)
        html = ROOSWidgetAdapter().render_display_card(field)
        assert "<c-card" in html
        # No value html, just the card structure

    def test_custom_icon(self):
        field = self._make_field(
            value="test", attributes={"icon": "schild", "icon_color": "groen"}
        )
        html = ROOSWidgetAdapter().render_display_card(field)
        assert 'icon="schild"' in html
        assert 'color="groen"' in html

    def test_default_icon(self):
        field = self._make_field(value="test")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert 'icon="sleutel"' in html
        assert 'color="blauw"' in html

    def test_label_rendered(self):
        field = self._make_field(label="API Sleutel", value="test")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert "API Sleutel" in html

    def test_description_rendered(self):
        field = self._make_field(value="test", description="Help text here")
        html = ROOSWidgetAdapter().render_display_card(field)
        assert "Help text here" in html

    def test_html_escaping(self):
        field = self._make_field(value='<script>alert("xss")</script>')
        html = ROOSWidgetAdapter().render_display_card(field)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestRenderFieldDispatch:
    def test_display_card_dispatches(self):
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

    def test_display_card_with_hyphen_dispatches(self):
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
```

## Code Style

- Follow existing ROOSWidgetAdapter patterns (same f-string style, same helper usage)
- Use `self.escape_html()` for all user-provided values
- Use `self._render_helper_text()` for descriptions
- Run `ruff check --fix && ruff format` after implementation
- Run `pyright` for type checking
