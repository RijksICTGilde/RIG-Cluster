"""Which icons the NLDD theme really SUPPLIES, and how our own names map onto them.

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

import logging
import re
from functools import cache
from pathlib import Path

from lord_of_the_components.assets import manifest_urls

logger = logging.getLogger(__name__)

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


# Our icons carry Dutch ROOS names; NLDD has its own English vocabulary. LOTC translates a
# handful itself (home -> house) and passes the rest through unchanged, after which NLDD
# does not recognise them and nothing appears.
#
# Only names that EXIST ON THE NLDD SIDE belong on the right: a guess would render the same
# empty icon as no translation, but invisibly instead of measurably. What is missing here is
# a real gap and is reported as such by tests/test_lotc_icon_mapping.py.
ROOS_TO_NLDD_ICONS = {
    "applicatie": "rectangle-stack",
    "beveiligingsscan": "shield-check-mark",
    "bewerken": "pencil-on-square",
    "envelop": "envelope",
    "groep-3-personen": "person-2",
    "kalender": "calendar",
    "computercode": "chevron-left-forward-slash-chevron-right",
    "database": "database",
    # The deltas are little triangles; caret is the NLDD triangle.
    "delta-naar-links": "caret-left",
    "delta-naar-rechts": "caret-right",
    "delta-omlaag": "caret-down",
    # square-arrow-down, not square-and-arrow-down: which of the two names the bundle DRAWS
    # changes per version, and the other renders an empty svg element. Measured in the
    # bundle, not read from the icon list.
    "downloaden": "square-arrow-down",
    # Logging out.
    "uitgang": "arrow-right-out-bucket",
    "document-blanco": "file-text",
    "externe-link": "link",
    "foutmelding": "exclamation-triangle",
    "grafiek": "chart-x-y-axis-line",
    "informatie-op-internet": "globe",
    "instellingen": "gear",
    # netwerk has no icon of its own; link is the closest that carries the idea of a
    # connection.
    "netwerk": "link",
    "klok": "timer",
    "kruis": "dismiss",
    # NLDD has no laptop, but it does have a desk with a screen.
    "laptop": "desk-with-screen",
    # folder-stack MAY NOT APPEAR HERE, however well it fits: LOTC rewrites that name in
    # icons.json to ``folder-on-folder``, which is not in the bundle, so the icon renders
    # empty. The gate measures the rendered ``name=`` (tests/test_lotc_icon_mapping.py).
    "map": "folder",
    # NLDD has no rocket; cylinder-split is the icon a deployment carries everywhere here
    # (see "server" below), so the wizard step gets the same image as its subject.
    "raket": "cylinder-split",
    # "Job uitvoeren" (postgres_pages.py): setting something in motion.
    "uitvoering": "play",
    "wolk": "cloud",
    "pijl-naar-rechts": "chevron-right",
    "publicatie": "file-text",
    "puzzel": "puzzle-piece",
    "refresh": "arrow-2-counter-clockwise",
    "schild-met-vinkje-erop": "shield-check-mark",
    "server": "cylinder-split",
    "sleutel": "lock-closed",
    # NLDD has no stethoscope. The heart carries the same meaning here: health-check and
    # deployment-health are both about the health of a deployment.
    "stethoscoop": "heart",
    "terug": "arrow-u-turn-backward",
    "user": "person",
    "verwijderen": "trash",
    "vinkje": "check-mark-circle",
    "vraagteken": "question-mark-circle",
    "wachtend-persoon": "person",
    "waarschuwing": "exclamation-triangle",
    "wereldbol": "globe",
    "zandloper": "timer",
    "zoek": "search",
    # No literal counterpart in NLDD, but one that SAYS the same: sleep mode (a moon) and
    # the help page about resources (a meter).
    "uit-aanknop": "moon",
    "weegschaal": "score-meter",
}


def to_nldd_icon(roos_icon: str) -> str:
    """Translate a ROOS icon name into the NLDD vocabulary.

    Unknown names pass through unchanged, because LOTC knows a number of them itself and a
    name NLDD does know has to be able to get through, but they no longer pass SILENTLY: an
    icon that never arrives still holds its space in the button and nobody hears about it.

    The hard gate is tests/test_lotc_icon_mapping.py, which walks every icon name in every
    template and every service definition. This log line is the safety net under it, for
    names that only come into being at runtime (a service that takes its icon from data).
    """
    translated = ROOS_TO_NLDD_ICONS.get(roos_icon, roos_icon)
    known = nldd_icon_names()
    if roos_icon and known and translated not in known:
        logger.warning(
            "Iconnaam %r levert geen icoon op (na vertaling: %r). Hij rendeert als een lege "
            "plek zonder foutmelding. Kies een naam die NLDD levert of leg de afbeelding in "
            "ROOS_TO_NLDD_ICONS.",
            roos_icon,
            translated,
        )
    return translated
