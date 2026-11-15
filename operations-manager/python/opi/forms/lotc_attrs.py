"""The attribute bundle of a form field, as the dict LOTC spreads onto a component.

A widget carries two bundles of attributes that are only known at render time:
``field.htmx_attrs`` (hx-get, hx-target, hx-trigger, ...) and ``field.attributes`` (loose
data-, aria- and HTML attributes). They cannot be written out as attribute TEXT inside a
component tag, because LOTC reads the braces as an attribute name; it offers
``:attrs="<dict>"`` for this, which merges a flat dict onto the input field, and this module
builds that dict.

Both bundles are already dicts, so this is a merge and not a conversion. The real work is the
skip list: besides HTML attributes ``field.attributes`` also carries field-definition settings
(which converter, which validator, how many rows), and those must not reach the HTML.
"""

from typing import Any


def attr_escape(value: object) -> str:
    """Escape a value for use inside a double-quoted HTML attribute.

    Unlike Jinja2's built-in ``|e`` filter this does NOT escape a single quote. A web
    component reads an attribute value as plain text, so ``&#39;`` would end up on the screen
    literally, and our attributes always sit between double quotes.

    Here and not with the widgets because the template environment registers it as a filter:
    importing from ``opi.forms.widgets`` would point that environment back at itself through
    the package's ``__init__``.
    """
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


#: Keys in field.attributes that describe the field definition and are not an HTML attribute.
#: The same separation as the extra_attrs macro in templates/widgets/_macros.html.j2, and a
#: test lays the two side by side, so a change there belongs here too.
NON_HTML_ATTRIBUTES = frozenset(
    {
        "rows",
        "min",
        "max",
        "step",
        "accept",
        "multiple",
        "min_items",
        "max_items",
        "add_label",
        "remove_label",
        "options_provider",
        "converter",
        "validator",
        "kv_format",
        "kv_mask_values",
        "icon",
        "icon_color",
    }
)


def field_attrs(field: Any, handled: list[str] | None = None) -> dict[str, Any]:  # noqa: ANN401  # a project-file value
    """Every loose attribute of a field as the flat dict ``:attrs`` wants.

    ``handled`` names the keys this widget already sets as an explicit attribute and that must
    therefore stay out of the bundle; by default the non-HTML keys.

    A ``None`` or empty value is not filtered out, because LOTC drops it itself and an empty
    string is meaningful for some aria attributes.
    """
    skip = NON_HTML_ATTRIBUTES if handled is None else frozenset(handled)
    merged: dict[str, Any] = dict(getattr(field, "htmx_attrs", None) or {})
    merged.update({key: value for key, value in (getattr(field, "attributes", None) or {}).items() if key not in skip})
    return merged
