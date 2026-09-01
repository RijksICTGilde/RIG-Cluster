"""Wat een aanroeper te zien krijgt als er iets misgaat.

Een fout heeft twee lezers met tegengestelde behoeften. De beheerder wil de
volledige uitzondering: het IP-adres, de poort, het pad, de stacktrace. De
gebruiker mag die juist niet zien - dat is de netwerkindeling van het platform -
en heeft er ook niets aan. Wat hij nodig heeft is of hij moet wachten, iets moet
corrigeren of iemand moet bellen.

Dit bestand bedient allebei, met een kenmerk ertussen: de melding op het scherm
noemt het kenmerk uit :mod:`opi.core.flow_id`, en datzelfde kenmerk staat op elke
logregel van hetzelfde verzoek. Zo vindt een beheerder met een grep de volledige
fout terug bij een melding die niets weggeeft.

De pagina's zijn bewust zelfstandig: ze moeten ook renderen als het thema, de
sjablonenmap of de sessie juist het probleem is.
"""

from __future__ import annotations

import html
from http import HTTPStatus
from typing import TYPE_CHECKING

from opi.core.flow_id import get_flow_id

if TYPE_CHECKING:
    import logging

    from starlette.requests import Request

#: Wat een 5xx zegt als de plek die hem opgooide zelf niets te melden had. Geen
#: "er ging iets mis" en verder niets: de zin moet zeggen wat je eraan kunt doen.
GENERIEKE_FOUTTEKST = "Er ging iets mis bij het verwerken van je verzoek. Probeer het over een minuut opnieuw."

_PAGE = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titel} - ZAD</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; display: grid; place-items: center;
         min-height: 100vh; color: #154273; background: #fff; }}
  main {{ text-align: center; padding: 2rem; max-width: 34rem; }}
  h1 {{ font-size: 2rem; margin: 0 0 .5rem; }}
  p {{ color: #4a4a4a; margin: 0 0 1.5rem; }}
  code {{ font-family: ui-monospace, monospace; background: #f3f3f3; border-radius: 3px;
         padding: .1em .3em; }}
  a {{ color: #154273; }}
</style>
</head>
<body>
<main>
  <h1>{kop}</h1>
  <p>{tekst}</p>{kenmerkregel}
  <a href="/dashboard">Naar het dashboard</a>
</main>
</body>
</html>
"""

#: De 404-pagina.
NOT_FOUND_PAGE = _PAGE.format(
    titel="Pagina niet gevonden",
    kop="Deze pagina bestaat niet",
    tekst="De link klopt niet meer, of de pagina is verplaatst.",
    kenmerkregel="",
)


def statusomschrijving(status_code: int) -> str:
    """De HTTP-omschrijving bij een status, of de code zelf als hij onbekend is.

    ``HTTPStatus(599)`` werpt een ValueError, en een uitzondering in de foutafhandeling
    is het ene ding dat hier nooit mag gebeuren.
    """
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return str(status_code)


def kenmerk_nu() -> str:
    """Het kenmerk van het verzoek dat nu loopt, of een lege string.

    Voor een plek die geen ``request`` in handen heeft -- een hulpfunctie die een
    fragment vult -- maar wel binnen hetzelfde verzoek draait.
    """
    huidig = get_flow_id()
    return "" if huidig == "-" else huidig


def kenmerk_van(request: Request) -> str:
    """Het kenmerk van dit verzoek, of een lege string als er geen is.

    Eerst uit de scope, dan uit de contextvar. De handler voor een onafgevangen
    uitzondering draait in ``ServerErrorMiddleware``, buiten de middleware die de
    contextvar zet; de scope reist wel mee naar buiten.
    """
    uit_scope = request.scope.get("state", {}).get("flow_id")
    if isinstance(uit_scope, str) and uit_scope not in ("", "-"):
        return uit_scope
    return kenmerk_nu()


def server_error_page(tekst: str, kenmerk: str) -> str:
    """De 5xx-pagina: wat er misging, wat je eraan kunt doen, en het kenmerk."""
    kenmerkregel = ""
    if kenmerk:
        kenmerkregel = f"\n  <p>Blijft het misgaan, meld dan dit kenmerk:<br><code>{html.escape(kenmerk)}</code></p>"
    return _PAGE.format(
        titel="Er ging iets mis",
        kop="Er ging iets mis",
        tekst=html.escape(tekst),
        kenmerkregel=kenmerkregel,
    )


def met_kenmerk(tekst: str, kenmerk: str) -> str:
    """De melding plus de zin die een gebruiker aan een beheerder kan doorgeven.

    Ook voor de JSON-envelop: een client die alleen ``detail`` afdrukt houdt zo iets
    in handen waarmee de fout terug te vinden is.
    """
    if not kenmerk:
        return tekst
    return f"{tekst} Blijft het misgaan, meld dan kenmerk {kenmerk}."


def log_render_failure(log: logging.Logger, wat: str, exc: BaseException) -> None:
    """Zet een mislukte render volledig in de log, met regelnummer en bronregel.

    Die twee stonden tot nu toe in het antwoord aan de gebruiker, waar ze niets
    oplossen en wel het sjabloonpad prijsgeven. Ze horen hier: de beheerder leest de
    log, en het kenmerk op deze regel is hetzelfde als op het scherm.
    """
    log.exception("Renderen van %s mislukt: %s", wat, _sjabloondetail(exc))


def _sjabloondetail(exc: BaseException) -> str:
    """De uitzondering plus het regelnummer en de bronregel die Jinja2 meegeeft."""
    detail = str(exc)
    lineno = getattr(exc, "lineno", None)
    if not isinstance(lineno, int):
        return detail
    detail = f"regel {lineno}: {detail}"
    source = getattr(exc, "source", None)
    if isinstance(source, str):
        regels = source.splitlines()
        if 0 <= lineno - 1 < len(regels):
            detail = f"{detail}\nbron: {regels[lineno - 1].strip()}"
    return detail
