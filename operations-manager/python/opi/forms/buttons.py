"""The button rule: which variants and which sizes a ``<c-button>`` may carry.

The component trips over nothing: a variant outside its table renders without a style class
and an unknown size without a size class, both without a message. The variants here are what
``lotc_nldd/renderers.py`` (``_BUTTON_VARIANT_MAP``) and
``lord_of_the_components/templates/components/button.html.j2`` both know; the sizes are a
choice, written down in ``features/knopmaten.md``. ``tests/test_lotc_knopmaten.py`` reads
those templates and holds this file to them.
"""

from __future__ import annotations

#: Every ``type`` value the component hangs a class on.
BUTTON_VARIANTS = frozenset(
    {
        "primary",
        "secondary",
        "tertiary",
        "quaternary",
        "subtle",
        "warning",
        "warning-subtle",
    }
)

#: The size for a button in a dense, repeated context.
DENSE_BUTTON_SIZE = "sm"

#: Every ``size`` value a template may write down. Anything else is either the default (and
#: then you do not write it down) or a size the component does not know.
BUTTON_SIZES = frozenset({DENSE_BUTTON_SIZE})


def check_button_variant(kind: str, owner: str) -> None:
    """Trip over a button variant the component does not know.

    For a variant that comes out of Python and only becomes a ``type`` attribute in the
    template: the template guard cannot read it there.
    """
    if kind not in BUTTON_VARIANTS:
        raise ValueError(
            f"{owner} kiest knopvariant '{kind}', en die bestaat niet. "
            f"Kies er een uit: {', '.join(sorted(BUTTON_VARIANTS))}"
        )
