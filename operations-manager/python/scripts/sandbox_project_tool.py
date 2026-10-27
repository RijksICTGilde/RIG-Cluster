#!/usr/bin/env python3
"""Snelle, HTTP-only hulp om sandboxprojecten te drijven, zonder browser.

Dit gereedschap maakt dezelfde server-side sessie die de e2e-suite gebruikt (een rij in
de rig-db plus het ondertekende id, RC-390) en praat plain HTTP, dus het antwoordt in
seconden. Gebruik het om een project een API-token te
geven, het te verwijderen via de Operations Manager, of een dienstconfig te zetten.

HOE HET AAN EEN BEWIJSMIDDEL KOMT. Tot schemaversie 2.12 schraapte dit script
``config.api-key`` van de detailpagina. Dat veld bestaat niet meer: het bewijsmiddel van
een project is een TOKEN van het service account ``project-api`` en daarvan staat alleen
de hash in het projectbestand, dus er is niets te lezen. De weg is er een UITGEVEN, over
het tabblad Team, met de sessiecookie die dit script al tekende. Dat doet
``tests.e2e.helpers.sandbox_api.geef_projecttoken``, een plek, zodat de e2e-suite en dit
script dezelfde weg lopen.

Alles gaat langs de API/UI van de Operations Manager -- geen kubectl, dus een verwijdering
loopt de echte OPI-teardown.

Omgeving (de defaults passen op de sandbox):
    ZAD_BASE_URL   default https://zad.sandbox.rijksapp.dev
    ZAD_SECRET_KEY of E2E_SECRET_KEY: de SECRET_KEY van het cluster, want daarmee wordt de
                   sessiecookie ondertekend. `eval "$(sandbox-gegevens --env)"` zet hem
    ZAD_USER_EMAIL default admin@sandbox.rijksapp.dev  (moet beheerder van het project zijn)

Voorbeelden:
    uv run python scripts/sandbox_project_tool.py token   myproject
    uv run python scripts/sandbox_project_tool.py delete  myproject
    uv run python scripts/sandbox_project_tool.py set-config myproject postgresql-database project \
        '{"scope":"project","storage":"256Mi","instances":1}'
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# Python zet bij een aanroep met een BESTANDSNAAM de map van het SCRIPT op ``sys.path`` en
# niet de projectwortel, en dan is ``tests`` niet te importeren.
PYTHON_ROOT = Path(__file__).resolve().parents[1]
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from tests.e2e.helpers.sandbox_api import STEP_UP_SESSION_KEY, geef_projecttoken, make_session_cookie  # noqa: E402

BASE = os.environ.get("ZAD_BASE_URL", "https://zad.sandbox.rijksapp.dev").rstrip("/")
# Dezelfde variabele die de e2e-suite leest, zodat `eval "$(sandbox-gegevens --env)"`
# genoeg is: de ondertekening moet met de SECRET_KEY van het cluster kloppen en die staat
# niet vast. De oude vaste waarde paste niet meer op de sandbox, en dan loopt elk verzoek
# naar de inlogpagina.
SECRET = os.environ.get("ZAD_SECRET_KEY") or os.environ.get("E2E_SECRET_KEY", "")
USER_EMAIL = os.environ.get("ZAD_USER_EMAIL", "admin@sandbox.rijksapp.dev")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _session_value() -> str:
    """De ondertekende sessiewaarde: een rij in de rig-db van het cluster plus het
    ondertekende id ervan (server-side sessies, RC-390).

    De step-upstempel hoort erbij: een token uitgeven vraagt een verse (RC-387) en dit
    gereedschap heeft geen browser om een code in te typen. Het maakt zijn sessie al aan
    met de SECRET_KEY van het cluster, dus de stempel geeft geen bevoegdheid die er niet
    was."""
    if not SECRET:
        raise SystemExit('geen SECRET_KEY: zet ZAD_SECRET_KEY, of doe eval "$(sandbox-gegevens --env)"')
    user = {"sub": "sandbox-tool", "email": USER_EMAIL, "name": "Sandbox Tool"}
    sessie = {"user": user, STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()}
    return make_session_cookie(sessie, secret=SECRET)


def _request(
    path: str, *, method: str = "GET", api_key: str | None = None, body: dict | None = None
) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    else:
        headers["Cookie"] = "session=" + _session_value()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)  # noqa: S310 (fixed https base)
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=120) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get_api_key(project: str) -> str:
    """Geef dit project een nieuw API-token en geef de waarde terug.

    Een dag geldig: dit is gereedschap voor een handeling nu, en een token dat langer
    blijft staan dan de handeling duurt is een bewijsmiddel dat niemand opruimt. Intrekken
    hoeft dus niet, aflopen is genoeg.
    """
    try:
        return geef_projecttoken(BASE, project, sessie_cookie=_session_value(), verify_ssl=False, dagen=1)
    except AssertionError as fout:
        raise SystemExit(f"kon geen API-token uitgeven voor '{project}': {fout}") from fout


def delete_project(project: str) -> None:
    key = get_api_key(project)
    status, text = _request(
        f"/api/projects/{project}", method="DELETE", api_key=key, body={"confirmDeletion": True, "force": True}
    )
    print(f"DELETE {project} -> {status} {text[:200]}")


def set_config(project: str, service: str, target: str, config_json: str) -> None:
    key = get_api_key(project)
    body = json.loads(config_json)
    status, text = _request(
        f"/api/v2/projects/{project}/services/{service}/config/{target}", method="PUT", api_key=key, body=body
    )
    print(f"PUT {service}/{target} on {project} -> {status} {text[:200]}")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        # Expliciet 0: wie om de uitleg vraagt heeft gekregen wat hij vroeg, en een
        # meetbank die op --help een foutcode geeft leest als een stuk gereedschap dat
        # niet start (tests/test_e2e_helpers.py::TestDeMeetbankscripts).
        print(__doc__)
        return 0
    cmd = argv[1]
    if cmd in ("token", "api-key") and len(argv) == 3:
        print(get_api_key(argv[2]))
    elif cmd == "delete" and len(argv) == 3:
        delete_project(argv[2])
    elif cmd == "set-config" and len(argv) == 6:
        set_config(argv[2], argv[3], argv[4], argv[5])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
