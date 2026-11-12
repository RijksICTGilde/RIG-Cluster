"""Which icons the NLDD theme really SUPPLIES.

Not read from ``icons.json``: that list is the intended vocabulary, and the bundle the
browser loads is what actually gets drawn. Those two diverge, and a name that exists only
on paper renders as nothing, without an error.

Which names are missing changes PER VERSION, and in both directions, so a name chosen
because it was once measured is not enough: measure it again on a bump. The names are
therefore read from the SUPPLIED files, the same place the browser reads them, and never
copied out by hand.

Two kinds of name count: the icons themselves, which sit in the bundle as
``["name", "<svg ...>"]``, and the friendly names NLDD forwards itself (``search`` ->
``magnifier``), which render fine and so belong to the vocabulary.

The alias regex is deliberately wide: from the minified bundle it also picks up a handful
of keys that are not icon names. That makes the set slightly too large and never too
small, and too large is the safe side here: this set WARNS, and the real gate is
``tests/test_lotc_icon_mapping.py``, which measures on the rendered markup.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from lord_of_the_components.assets import manifest_urls

#: An icon in the bundle: ["name", "<svg ...".
_ICON = re.compile(r'\["([a-z0-9][a-z0-9-]{1,50})",[\'"]<svg')

#: A forward in the bundle: name:"existing-icon" or "name":"existing-icon".
_ALIAS = re.compile(r'(?:"([a-z0-9-]+)"|([a-z][a-z0-9]*)):"([a-z0-9-]+)"')


def _dist_directory() -> Path | None:
    """The dist directory of lotc_nldd, wherever it is installed."""
    try:
        import lotc_nldd
    except ImportError:
        return None

    if lotc_nldd.__file__ is None:
        return None

    # From the manifest and not as a fixed path: the bundle sits in a delivery directory
    # with the LOTC version in its name (v<date>-<time>-NLDD<version>/nldd/dist). A fixed
    # path pointed at an empty spot after that change, and the icon gate then passed
    # everything silently.
    package = Path(lotc_nldd.__file__).parent
    _, js_urls = manifest_urls(package / "assets.json", "")
    bundle = next((url for url in js_urls if url.endswith("/nldd/dist/nldd.js")), None)
    if bundle is None:
        return None
    directory = (package / "static" / "lotc" / bundle.lstrip("/")).parent
    return directory if directory.is_dir() else None


@cache
def nldd_icon_names() -> frozenset[str]:
    """Every icon name the supplied NLDD bundle really draws.

    Empty when the theme is not installed. That is deliberate: the callers use this set to
    WARN, and an environment without the theme should not complain about every icon. The
    test that uses it as a gate then skips itself.
    """
    directory = _dist_directory()
    if directory is None:
        return frozenset()

    icons: set[str] = set()
    sources: list[str] = []
    for file in sorted(directory.glob("*.js")):
        content = file.read_text(encoding="utf-8", errors="ignore")
        sources.append(content)
        icons |= {m.group(1) for m in _ICON.finditer(content)}

    aliases: set[str] = set()
    for content in sources:
        for m in _ALIAS.finditer(content):
            name = m.group(1) or m.group(2)
            if m.group(3) in icons:
                aliases.add(name)

    return frozenset(icons | aliases)
