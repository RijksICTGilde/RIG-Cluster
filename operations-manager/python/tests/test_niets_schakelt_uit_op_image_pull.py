"""Eén plek zet een component uit, en die plek raakt een image-pull-fout niet aan.

Uitschakelen betekent ``replicas: 0``, en dat haalt juist de pod weg die de pull opnieuw
zou doen. Een disable lost zichzelf ook niet op: hij wacht op een uitrol. Daarom is de
klasse opgeruimd in plaats van de foutmelding verbeterd (RC-243), nadat dezelfde string in
zeven weken drie keer verkeerd was gelezen.

Deze toets bewaakt de VORM waarin dat vastligt, want het beslispunt zelf zit midden in
``_process_from_git`` en in de sanitize-lus en is niet als functie aan te roepen. Wat hier
vastligt: hoeveel schrijvers er zijn, en dat de naam van de verwijderde helper niet
terugkomt.
"""

import pathlib
import re

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


def test_er_is_precies_een_plek_die_een_component_uitzet() -> None:
    """Drie modules roepen de schrijver aan; twee daarvan alleen om te HERACTIVEREN.

    De sanitize is de enige die nog uitzet, en die slaat een component met een
    image-pull-event helemaal over (zie test_sanitize.py). Komt er een tweede
    uitschakelaar bij, dan hoort daar een reden bij en wordt deze lijst bijgewerkt.
    """
    aanzetters = set()
    uitzetters = set()
    for pad in sorted(OPI.rglob("*.py")):
        inhoud = pad.read_text(encoding="utf-8")
        naam = str(pad.relative_to(OPI))
        for aanroep in re.findall(rf"{SCHRIJVER}\((.*?)\)", inhoud, flags=re.DOTALL):
            if naam == "handlers/project_file_handler.py":
                continue  # de definitie en haar eigen docstring
            if re.search(r",\s*True\s*,", aanroep):
                uitzetters.add(naam)
            elif re.search(r",\s*False\s*,", aanroep):
                aanzetters.add(naam)

    assert uitzetters == {"api/resource_router.py"}
    assert aanzetters == {
        "services/resource_tuning_service.py",
        "services/catalog/deployment_health/__init__.py",
    }
