"""Een handler vraagt niet nog eens wat de poort al besliste.

``enforce_route_action`` weegt de action van de route voordat de handler draait. Roept die
handler daarna in zijn eigen body ``authorize`` aan met DEZELFDE action, dan is dat een
tweede antwoord op dezelfde vraag, en schrijft de audit er sinds RC-397 een tweede rij
voor (``opi/middleware/audit.py``).

Een ANDERE action in de body blijft goed: ``submit_step`` draagt ``wizard.use`` op de route
en weegt daarnaast ``project.edit`` op een bestaand project, en dat zijn twee vragen.
"""

from __future__ import annotations

import ast
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "opi" / "web"


def _route_action(functie: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """De naam van het Action-lid uit ``@action(Action.X)`` op deze handler."""
    for versiering in functie.decorator_list:
        if not isinstance(versiering, ast.Call) or not versiering.args:
            continue
        naam = versiering.func
        if not (isinstance(naam, ast.Name) and naam.id == "action"):
            continue
        eerste = versiering.args[0]
        if isinstance(eerste, ast.Attribute) and isinstance(eerste.value, ast.Name) and eerste.value.id == "Action":
            return eerste.attr
    return None


def _authorize_actions(functie: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, int]]:
    """De Action-leden waarmee de body van deze handler ``authorize`` aanroept."""
    gevonden: list[tuple[str, int]] = []
    for knoop in ast.walk(functie):
        if not isinstance(knoop, ast.Call):
            continue
        aangeroepen = knoop.func
        if not (isinstance(aangeroepen, ast.Name) and aangeroepen.id == "authorize"):
            continue
        for sleutelwoord in knoop.keywords:
            if sleutelwoord.arg != "action":
                continue
            waarde = sleutelwoord.value
            if isinstance(waarde, ast.Attribute) and isinstance(waarde.value, ast.Name) and waarde.value.id == "Action":
                gevonden.append((waarde.attr, knoop.lineno))
    return gevonden


def test_geen_handler_herhaalt_de_action_van_zijn_eigen_route() -> None:
    dubbel: list[str] = []
    for bestand in sorted(WEB.rglob("*.py")):
        boom = ast.parse(bestand.read_text(encoding="utf-8"))
        for knoop in ast.walk(boom):
            if not isinstance(knoop, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            van_de_route = _route_action(knoop)
            if van_de_route is None:
                continue
            for in_de_body, regel in _authorize_actions(knoop):
                if in_de_body == van_de_route:
                    dubbel.append(f"{bestand.name}:{regel} {knoop.name} vraagt Action.{van_de_route} nog een keer")

    assert not dubbel, "De poort heeft dit al beslist; lees het project met authorized_project():\n" + "\n".join(dubbel)
