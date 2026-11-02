"""Voorbeelden waarop de eigen semgrep-regels moeten vuren, en voorbeelden waarop niet.

Dit bestand draait nooit: het is het bewijs dat aanvalsproef/semgrep-zad.yaml iets vindt.
Een regel die niets meer vangt is een gate die groen staat zonder te kijken.

De job sast-semgrep in .github/workflows/security.yml kopieert dit bestand tijdelijk naar
opi/web/ (de regels hebben een paths-filter) en eist dat beide regel-id's eruit komen.
Het ligt buiten operations-manager/python, dus ruff en pyright zien het niet, en dat is de
bedoeling: de aanroep met shell=True hoort hier juist te staan.
"""

import subprocess

from fastapi import APIRouter, Request
from opi.core.auth_decorators import requires_sso

router = APIRouter()


# ruleid: zad-webroute-zonder-sso
@router.get("/geheim")
async def geheime_pagina(request: Request) -> str:
    return "geheim"


# ok: zad-webroute-zonder-sso
@router.get("/wel-beschermd")
@requires_sso
async def beschermde_pagina(request: Request) -> str:
    return "ok"


# ok: zad-webroute-zonder-sso
@router.get("/open-met-reden")
async def open_pagina(request: Request) -> str:
    """BEWUST ZONDER ``@requires_sso``: een publieke uitlegpagina."""
    return "open"


# ruleid: zad-subprocess-via-een-shell
# ruleid: zad-subprocess-buiten-connector
def roep_via_een_shell(naam: str) -> None:
    subprocess.run(f"kubectl get pod {naam}", shell=True, check=False)


# ok: zad-subprocess-via-een-shell
# ruleid: zad-subprocess-buiten-connector
def roep_met_een_argumentenlijst(naam: str) -> None:
    subprocess.run(["kubectl", "get", "pod", naam], check=False)
