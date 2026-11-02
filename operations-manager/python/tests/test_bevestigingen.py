"""Guard: een bevestiging wordt nooit een systeemvenster.

``confirm()`` en ``alert()`` openen een venster van de BROWSER: het draagt de naam van de
host, het weet niets van de vormgeving, het bevriest de pagina tot je klikt, en het is niet
te stijlen en niet te testen. De regel in dit portaal is dat elke vraag en elke melding in
een dialoog van het thema valt (``<nldd-modal-dialog>``), ook een kleine.

Dat was zeven keer niet zo, elke keer in een eigen constructie: ``hx-confirm`` (dat htmx op
``window.confirm`` laat terugvallen), ``onsubmit="return confirm(...)"`` en een rechtstreekse
``window.confirm``. Deze poort houdt tegen dat er een achtste bij komt.

Gezocht wordt in de CODE en niet in de uitleg: commentaar gaat er eerst uit, zodat de
toelichting bij een omzetting mag vertellen wat er stond.

DE NAAM VAN EEN GEBRUIKER (RC-287)

Hier stond een poort op ``onsubmit`` met ``| tojson`` en ``| forceescape``. Die hoorde bij
de oude constructie: autoescape dekt een stringliteral in een ``on*``-attribuut niet, want
de HTML-parser decodeert ``&#39;`` terug tot een apostrof voordat de scriptparser de waarde
ziet, en dan breekt een naam als ``O'Brien`` uit de string. Een naam in ``data-bevestig``
is een gewone attribuutwaarde en geen code, dus daar beschermt autoescape wel. De meting
dat de naam letterlijk op het scherm komt staat in
``tests/e2e/test_user_admin.py::TestBevestigingMetVijandigeNaam``.
"""

import re

from opi.core.template_helpers import STATIC_DIR, TEMPLATES_DIR

#: Wat in de code niet voor mag komen, met de naam waaronder de melding hem noemt.
VERBODEN = {
    "confirm(": re.compile(r"\bconfirm\s*\("),
    "hx-confirm": re.compile(r"\bhx-confirm\b"),
    "alert(": re.compile(r"(?<![\w.-])alert\s*\("),
}

#: Bestanden die een ``alert(`` mogen houden, met de reden erbij. Een ``confirm`` is nooit
#: toegestaan: daar is een dialoog voor.
ALERT_TOEGESTAAN = {
    "static/js/wizard.js": (
        "de gedocumenteerde terugval voor als het sjabloon zijn eigen dialoog niet "
        "meebracht; een lelijke melding is daar beter dan een klik die stil niets doet"
    ),
    "test-template-variables.html.j2": "een testpagina voor sjabloonvariabelen, geen portaalpagina",
}

#: ``[^\n]*`` en niet ``.*``: met DOTALL erbij slikt een regelcommentaar de rest van het
#: bestand, en dan meet de poort niets meer.
JS_COMMENTAAR = re.compile(r"/\*.*?\*/|^[ \t]*//[^\n]*", re.DOTALL | re.MULTILINE)
JINJA_COMMENTAAR = re.compile(r"{#.*?#}", re.DOTALL)


def _bronnen() -> list[tuple[str, str]]:
    """(naam, code zonder commentaar) voor elke template en elk eigen scriptbestand.

    De gebundelde bibliotheken blijven buiten de meting: ``*.min.js`` is niet van ons, en
    ``static/swagger-ui/`` valt buiten ``static/js/``.
    """
    sjablonen = [
        (str(pad.relative_to(TEMPLATES_DIR)), JINJA_COMMENTAAR.sub("", pad.read_text()))
        for pad in sorted(TEMPLATES_DIR.rglob("*.j2"))
    ]
    scripts = [
        (f"static/js/{pad.name}", JS_COMMENTAAR.sub("", pad.read_text()))
        for pad in sorted((STATIC_DIR / "js").glob("*.js"))
        if not pad.name.endswith(".min.js")
    ]
    return sjablonen + scripts


def _overtredingen() -> list[str]:
    gevonden = []
    for naam, code in _bronnen():
        for patroon, regex in VERBODEN.items():
            if not regex.search(code):
                continue
            if patroon == "alert(" and naam in ALERT_TOEGESTAAN:
                continue
            gevonden.append(f"{naam}: {patroon}")
    return gevonden


def test_geen_systeemvenster_in_een_sjabloon_of_script() -> None:
    """Vragen en melden gaat door de dialoog van het thema."""
    overtredingen = _overtredingen()

    assert not overtredingen, (
        "Zet de vraag in <nldd-modal-dialog> via static/js/bevestiging.js: een formulier "
        "draagt data-bevestig, data-bevestig-uitleg en data-bevestig-knop, en de "
        "POST-bestemming blijft zijn eigen action/hx-post:\n  " + "\n  ".join(overtredingen)
    )


def test_de_uitzonderingen_zijn_nog_nodig() -> None:
    """Een uitzondering voor een bestand zonder ``alert(`` verbergt de regel weer."""
    code = dict(_bronnen())
    verlopen = [naam for naam in ALERT_TOEGESTAAN if naam not in code or not VERBODEN["alert("].search(code[naam])]

    assert not verlopen, "Deze uitzonderingen zijn niet meer nodig, haal ze weg:\n  " + "\n  ".join(verlopen)


#: De dialoog zelf, en het attribuut dat een formulier hem laat openen.
DIALOOG = "bg/_bevestiging.html.j2"
VERWIJZING = re.compile(r"{%-?\s*(?:include|extends)\s+[\"']([^\"']+)[\"']")


def _sjablonen() -> dict[str, str]:
    """Elke template op zijn pad onder templates_lotc, de ruwe tekst, commentaar en al.

    Een ``{# ... data-bevestig ... #}`` kan de poort daarmee op een uitleg laten vuren.
    Vandaag bestaat zo'n commentaar niet, en het weghalen heeft zijn eigen prijs: een
    uitgecommentarieerde include zou dan een pagina zonder dialoog laten slagen.
    """
    return {str(pad.relative_to(TEMPLATES_DIR)): pad.read_text() for pad in sorted(TEMPLATES_DIR.rglob("*.j2"))}


def _meegenomen(naam: str, sjablonen: dict[str, str], gezien: set[str] | None = None) -> set[str]:
    """Alles wat het renderen van ``naam`` meeneemt, include en extends door."""
    gezien = gezien if gezien is not None else set()
    for verwijzing in VERWIJZING.findall(sjablonen.get(naam, "")):
        if verwijzing in gezien:
            continue
        gezien.add(verwijzing)
        _meegenomen(verwijzing, sjablonen, gezien)
    return gezien


def test_wie_data_bevestig_draagt_krijgt_de_dialoog_mee() -> None:
    """Zonder de dialoog op de pagina post de knop zonder vraag.

    ``data-bevestig`` zit vaak in een fragment, dus de meting gaat over de pagina die hem
    insluit: die moet ``bg/_bevestiging.html.j2`` in zijn eigen boom hebben.
    """
    sjablonen = _sjablonen()
    bomen = {naam: _meegenomen(naam, sjablonen) for naam in sjablonen}
    pagina_s = [naam for naam, boom in bomen.items() if not any(naam in andere for andere in bomen.values())]

    zonder = [
        naam
        for naam in pagina_s
        if DIALOOG not in bomen[naam]
        and any("data-bevestig" in sjablonen.get(deel, "") for deel in bomen[naam] | {naam})
    ]

    assert not zonder, f'Sluit {{% include "{DIALOOG}" %}} in:\n  ' + "\n  ".join(zonder)
