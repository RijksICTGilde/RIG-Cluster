"""Dates shown to a user go through ``dutch_date``, not through string slicing.

Three templates used to render ``entry.date[:10]``. That bypasses the one place that
knows about time zones, and it is wrong on its own: an ISO timestamp is UTC, so slicing
the first ten characters shows the UTC date. At 23:30 UTC that is yesterday here.

    format_dutch_date("2026-08-05T23:30:00+00:00")  ->  6 augustus 2026
    "2026-08-05T23:30:00+00:00"[:10]                ->  2026-08-05

The filter converts to Europe/Amsterdam and writes the month in Dutch, so it is both
correct and consistent with the rest of the interface.
"""

from __future__ import annotations

import pathlib
import re

import opi
from opi.core.template_helpers import format_dutch_date

_TEMPLATES = pathlib.Path(opi.__file__).parent / "templates_lotc"


def test_the_filter_converts_to_our_own_timezone() -> None:
    """The case that made slicing wrong: late UTC is already the next day here."""
    assert format_dutch_date("2026-08-05T23:30:00+00:00", include_time=False) == "6 augustus 2026"


#: Elke Jinja-uitdrukking waarin een ``[:N]`` staat: ``{{ ... }}`` of ``{% ... %}``.
_UITDRUKKING = re.compile(r"\{[{%](?:(?!\{[{%])[\s\S])*?\[:\s*\d+\s*\][\s\S]*?[%}]\}")

#: De afkappingen in sjablonen die GEEN tijdstempel zijn, met wat ze afkappen. Een nieuwe
#: regel hier hoort een bewuste keuze te zijn: alles wat niet op deze lijst staat, valt.
#:
#: Deze test werkte eerst andersom - hij zocht een slice met "date", "time", "sync" of
#: "_at" in de naam ervoor. Daarmee ontglipten hem precies de twee die dit onderzoek
#: opleverde: ``(item.gestart or "")[:16]`` in de takentabel (de naam is Nederlands EN het
#: teken voor de haak is een sluithaakje, dus ook ``\\w+`` matchte niet) en
#: ``project.last_deployed[:16]`` op het dashboard. Een lijst van wat WEL mag, kan die
#: fout niet maken.
_GEEN_TIJDSTEMPEL = {
    # de eerste drie teamleden als avatar
    ("projects-overview/_tabel.html.j2", "{% for user in (project.users | sort(attribute='email'))[:3] %}"),
    # de initialen uit een e-mailadres
    ("projects-overview/_tabel.html.j2", "{{ user.email[:2]|upper }}"),
    # de eerste vier diensten
    ("projects-overview/_tabel.html.j2", "{% for service in project.services[:4] %}"),
    # een lange foutmelding per component, met "..." erachter
    ("bg/_argocd-deployment-card.html.j2", "{{ reden[:120] }}"),
    # een lange ArgoCD-melding, met "..." erachter
    ("bg/_argocd-deployment-card.html.j2", "{{ status.operation_message[:100] }}"),
    # de eerste vijf afwijkende resources, met een telling erboven
    ("bg/_argocd-deployment-card.html.j2", "{% for deviation in deviations[:5] %}"),
}


def test_no_template_slices_a_timestamp() -> None:
    """Geen enkel sjabloon kapt iets af, op de vijf plekken na die geen tijd tonen.

    The first version of this guard looked for ``date[:10]`` literally and missed
    ``status.last_sync[:19]|replace('T', ' ')`` on the deployment card, which rendered
    the sync time in UTC with "UTC" written after it. Same defect, two characters
    different. The second version matched the slice but still guessed from the name
    before it, and missed the two Dutch-named ones in the tasks table and on the
    dashboard. Daarom telt deze versie ze allemaal en toetst hij tegen een lijst.
    """
    gevonden = [
        (str(path.relative_to(_TEMPLATES)), match.strip())
        for path in sorted(_TEMPLATES.rglob("*.j2"))
        for match in _UITDRUKKING.findall(path.read_text(encoding="utf-8"))
    ]
    assert set(gevonden) == _GEEN_TIJDSTEMPEL, (
        "afkappingen erbij: als het een tijdstip is hoort het via dutch_date, anders hoort het in "
        f"_GEEN_TIJDSTEMPEL. Erbij: {sorted(set(gevonden) - _GEEN_TIJDSTEMPEL)}. "
        f"Verdwenen: {sorted(_GEEN_TIJDSTEMPEL - set(gevonden))}"
    )


def test_the_three_converted_templates_use_the_filter() -> None:
    """De drie plekken die een tijdstip tonen, doen dat via het filter.

    Het gaat om de plek waar de markup staat, niet om het bestand: toen
    admin/approvals.html.j2 werd opgedeeld in deeltemplates verhuisde de datum mee naar
    admin/approvals/_aanvragen.html.j2. Daarom wijst deze test naar het bestand dat de
    datum nu toont. Om dezelfde reden staat de goedkeuringsmelding hier nu als
    bg/_patterns.html.j2: die melding hoort ook naast de PUBLIEKE LINKS te staan, dus de
    markup is naar de gedeelde macro ``approval_alerts`` verhuisd.
    """
    for name in (
        "wizard/partials/approval_items.html.j2",
        "admin/approvals/_aanvragen.html.j2",
        "bg/_patterns.html.j2",
    ):
        assert "dutch_date" in (_TEMPLATES / name).read_text(encoding="utf-8"), name


def test_de_twee_laatst_omgezette_sjablonen_gebruiken_het_filter() -> None:
    """De takentabel en de tegel op het dashboard, omgezet in RC-133.

    Dat er geen afkapping meer staat bewijst nog niet dat de tijd nu WEL getoond wordt;
    weglaten zou die test ook halen. Wat de takentabel op het scherm zet staat in
    tests/test_takentabel_tijdstippen.py.
    """
    for name in ("bg/_tasks.html.j2", "dashboard/_projecten.html.j2"):
        assert "dutch_date" in (_TEMPLATES / name).read_text(encoding="utf-8"), name
