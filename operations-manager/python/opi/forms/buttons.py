"""The button rule: which variants and which sizes a ``<c-button>`` may carry.

The component itself does not TRIP over a word it does not know: a variant outside its
table renders without a style class and an unknown size without a size class, both without
a message. So the vocabularies are written down here.

The variants are the two sources that agree: ``lotc_nldd/renderers.py``
(``_BUTTON_VARIANT_MAP``, the theme the portal runs in) and
``lord_of_the_components/templates/components/button.html.j2`` (the bare build line).

The sizes are a choice, written down in ``features/knopmaten.md``:

* ``sm``: buttons in a repeated, dense context: a row in a table, a card in a list, the
  actions next to a single item.
* the default (``md``): everything else, the main action of a page, the buttons under a form,
  the buttons in a dialog. Those you do NOT write down: ``md`` is the component's default, and
  a size that can be written two ways ends up being written two ways.

``xs`` is not in it.

Guarded by ``tests/test_lotc_knopmaten.py``, which reads the templates itself.
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

    For the places where the variant comes out of PYTHON and only becomes a ``type``
    attribute in the template: the template guard can read nothing there, so such a value
    would otherwise only show up as a bare button on the screen.
    """
    if kind not in BUTTON_VARIANTS:
        raise ValueError(
            f"{owner} kiest knopvariant '{kind}', en die bestaat niet. "
            f"Kies er een uit: {', '.join(sorted(BUTTON_VARIANTS))}"
        )
