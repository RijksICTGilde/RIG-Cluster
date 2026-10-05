"""Eén plek zet een component uit, en die plek raakt een image-pull-fout niet aan.

Uitschakelen betekent ``replicas: 0``, en dat haalt juist de pod weg die de pull opnieuw
zou doen. Een disable lost zichzelf ook niet op: hij wacht op een uitrol. Daarom is de
klasse opgeruimd in plaats van de foutmelding verbeterd (RC-243), nadat dezelfde string in
zeven weken drie keer verkeerd was gelezen.

Deze toets bewaakt de VORM waarin dat vastligt, want het beslispunt zelf zit midden in
``process_project_from_git`` en in de sanitize-lus en is niet als functie aan te roepen. Wat hier
vastligt: hoeveel schrijvers er zijn, en dat de naam van de verwijderde helper niet
terugkomt.
"""

import ast
import pathlib
from inspect import signature

import opi
from opi.handlers.project_file_handler import ProjectFileHandler

OPI = pathlib.Path(opi.__file__).parent

#: De schrijver uit de klasse zelf opgehaald en niet als tekst herhaald: wordt de methode
#: hernoemd, dan vindt de zoektocht hieronder niets en slaagt deze toets leeg. Dat is
#: precies wat er met een zelfverzonnen naam gebeurt.
SCHRIJVER = ProjectFileHandler.set_deployment_component_disabled.__name__

#: De helper die met RC-243 is verdwenen. Deze naam valt uit niets af te leiden, dus staat
#: hij hier als tekst - met de kanarie hieronder ernaast, zodat een zoektocht die niets
#: meer kan vinden niet als "schoon" leest.
VERDWENEN_HELPER = "disable_components_for_image_pull"
KANARIE = "image_is_confirmed_absent"


def _modules_met(tekst: str) -> set[str]:
    gevonden = set()
    for pad in OPI.rglob("*.py"):
        if tekst in pad.read_text(encoding="utf-8"):
            gevonden.add(str(pad.relative_to(OPI)))
    return gevonden


def test_de_zoektocht_vindt_werkelijk_iets() -> None:
    # De kanarie: een naam die er wél is. Zonder dit zou een kapotte zoektocht elke
    # bewering hieronder gratis laten slagen.
    assert _modules_met(KANARIE), "de zoektocht vindt niets, dus bewijst hieronder niets"
    assert _modules_met(SCHRIJVER)


def test_de_verwijderde_helper_komt_niet_terug() -> None:
    assert _modules_met(VERDWENEN_HELPER) == set()


def _aanroepers() -> tuple[set[str], set[str]]:
    """Per module: zet hij uit, of alleen aan.

    Over de AST en niet over een grep op ``, True,``. Die zag een aanroep met
    ``disabled=True`` helemaal niet staan: met een tweede uitschakelaar in die vorm erbij
    bleef de grendel groen, en dat is precies wat hij hoort te vangen. De AST leest het
    argument op zijn plek in de signatuur, dus beide vormen tellen, en een waarde die hier
    niet te lezen is valt op in plaats van weg. Dezelfde keuze als in
    ``tests/test_schrijvers_inventaris.py``, om een verwante reden: een grep telt ook een
    naam in een docstring mee.
    """
    aanzetters: set[str] = set()
    uitzetters: set[str] = set()
    for pad in sorted(OPI.rglob("*.py")):
        naam = str(pad.relative_to(OPI))
        if naam == "handlers/project_file_handler.py":
            continue  # de definitie zelf
        for knoop in ast.walk(ast.parse(pad.read_text(encoding="utf-8"))):
            if not isinstance(knoop, ast.Call) or not isinstance(knoop.func, ast.Attribute):
                continue
            if knoop.func.attr != SCHRIJVER:
                continue
            stand = _stand(knoop)
            if stand is True:
                uitzetters.add(naam)
            elif stand is False:
                aanzetters.add(naam)
            else:
                raise AssertionError(
                    f"{naam} zet 'disabled' op iets dat hier niet te lezen is "
                    f"({ast.unparse(knoop)}); zet die aanroep in deze grendel"
                )
    return aanzetters, uitzetters


def _stand(aanroep: ast.Call) -> bool | None:
    """De waarde van het ``disabled``-argument, positioneel of met trefwoord."""
    plek = list(signature(ProjectFileHandler.set_deployment_component_disabled).parameters).index("disabled") - 1
    knoop: ast.expr | None = None
    if len(aanroep.args) > plek:
        knoop = aanroep.args[plek]
    for trefwoord in aanroep.keywords:
        if trefwoord.arg == "disabled":
            knoop = trefwoord.value
    if isinstance(knoop, ast.Constant) and isinstance(knoop.value, bool):
        return knoop.value
    return None


def test_er_is_precies_een_plek_die_een_component_uitzet() -> None:
    """Drie modules roepen de schrijver aan; twee daarvan alleen om te HERACTIVEREN.

    De sanitize is de enige die nog uitzet, en die slaat een component met een
    image-pull-event helemaal over (zie test_sanitize.py). Komt er een tweede
    uitschakelaar bij, dan hoort daar een reden bij en wordt deze lijst bijgewerkt.
    """
    aanzetters, uitzetters = _aanroepers()

    assert uitzetters == {"api/resource_router.py"}
    assert aanzetters == {
        "services/resource_tuning_service.py",
        "services/catalog/deployment_health/__init__.py",
    }
