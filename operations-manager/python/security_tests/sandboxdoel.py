"""Het runbestand voor het tweede doel: een geclaimde sandbox in het kind-cluster.

De wegwerp-sandbox zet zichzelf op en schrijft daarna zijn runbestand. Een sandbox in het
cluster bestaat al, dus hier wordt alleen het runbestand geschreven, met het token dat
``task security-proof:sandbox`` in de pod heeft gezet. De drie sloten van ``doel.py`` blijven
staan: het doel komt uit dit runbestand, de host moet op het sandboxdomein staan, en de
instantie moet het token terugkaatsen op ``/health``.

Wat er in het runbestand aan herkenningen komt, bepaalt wat er te meten valt:

* ``ZAD_PROEF_SESSIE_SECRET`` plus ``ZAD_PROEF_ADMIN``: hiermee wordt een sessiekoekje voor de
  beheerder van de sandbox gemaakt. Sinds RC-390 hoort daar een RIJ bij: deze zet een
  server-side sessie in de database van het doel (``sandbox_api.make_session_cookie``) en
  ondertekent alleen het id. Zonder deze twee gaan de proeven die een sessie nodig hebben
  over in NIET GEMETEN.
* ``ZAD_PROEF_SLEUTELS``: JSON van projectnaam naar API-sleutel. Zonder deze zijn er geen
  proefprojecten op dit doel, en slaan de proeven die een projectsleutel vragen zich over.
"""

from __future__ import annotations

import json
import os
import sys
import time

from tests.e2e.helpers.sandbox_api import make_session_cookie

from security_tests.doel import AANROEPLOG, RUNBESTAND, SANDBOX, OnveiligDoel, huidig_doel, soort_doel

#: De sandbox van dit project, zoals workflow/sandbox.md hem noemt.
STANDAARDDOEL = "https://zad.sandbox.rijksapp.dev"


def _sessies() -> dict[str, str]:
    """De ondertekende sessies voor dit doel, of niets als het geheim niet is meegegeven.

    Elke sessie is een rij in de database van het doel plus het ondertekende id ervan.
    """
    secret = os.environ.get("ZAD_PROEF_SESSIE_SECRET", "")
    admin = os.environ.get("ZAD_PROEF_ADMIN", "")
    if not secret or not admin:
        return {}
    gebruiker = {"sub": admin, "email": admin, "name": "Proef ZAD-admin"}
    return {"zad-admin": make_session_cookie({"user": gebruiker}, secret=secret)}


def schrijf_runbestand(url: str, token: str) -> None:
    """Zet het doel in het runbestand, met wat er aan herkenningen beschikbaar is."""
    RUNBESTAND.parent.mkdir(parents=True, exist_ok=True)
    RUNBESTAND.write_text(
        json.dumps(
            {
                "url": url.rstrip("/"),
                "token": token,
                # Geen eigen proces: deze sandbox draait in het cluster en wordt niet door
                # dit harnas afgebroken. ``:down`` raakt hem dus niet.
                "pid": 0,
                "sleutels": json.loads(os.environ.get("ZAD_PROEF_SLEUTELS", "{}")),
                "sessies": _sessies(),
                "aanroeplog": str(AANROEPLOG),
                "gestart": time.time(),
            },
            indent=2,
        )
    )


def main(argv: list[str] | None = None) -> int:
    """Schrijf het runbestand voor een sandboxdoel en verifieer het meteen."""
    argumenten = sys.argv[1:] if argv is None else argv
    url = argumenten[0] if argumenten else STANDAARDDOEL
    token = os.environ.get("ZAD_PROEF_TOKEN", "")
    if not token:
        print("GEWEIGERD: ZAD_PROEF_TOKEN is leeg; zonder token is er geen doel", file=sys.stderr)
        return 2
    if soort_doel(url) != SANDBOX:
        print(f"GEWEIGERD: {url!r} is geen sandboxdoel", file=sys.stderr)
        return 2
    schrijf_runbestand(url, token)
    try:
        doel = huidig_doel()
    except OnveiligDoel as fout:
        RUNBESTAND.unlink(missing_ok=True)
        print(f"GEWEIGERD: {fout}", file=sys.stderr)
        return 2
    print(f"OK: {doel.url} is het sandboxdoel van deze run ({len(doel.sleutels)} proefprojecten)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
