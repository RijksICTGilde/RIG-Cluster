"""Het alarm leest de metriek die OPI werkelijk uitgeeft, en de klassen die er zijn.

Sinds RC-243 schakelt OPI een component dat zijn image niet kan ophalen niet meer uit. Dit
alarm is de tegenprestatie: zonder dit blijft zo'n pod stil in ImagePullBackOff hangen.
Daarmee hangt het aan drie koppelingen die stil kunnen verschuiven, want niets anders legt
ze langs elkaar:

- de naam in ``expr`` tegen de naam die de collector uitgeeft. Hernoemt iemand de gauge,
  dan vuurt het alarm nooit meer: ``opi_image_pull_failing_pods > 0`` is op een niet
  bestaande serie niet rood maar leeg.
- de reden-klassen in de beschrijving tegen de constanten in de code. Daar hangt de
  handelingsinstructie aan (wie het oplost), dus een klasse die niet meer zo heet stuurt
  de lezer naar een label dat er niet is.
- de regel in de kustomization van zijn eigen overlay. Een resource die daar niet in staat
  rendert stil weg: geen fout, geen alarm.
"""

from __future__ import annotations

from pathlib import Path

from opi.handlers.project_file_handler import IMAGE_PULL_ABSENT, IMAGE_PULL_CAPACITY, IMAGE_PULL_UNDIAGNOSED
from ruamel.yaml import YAML

_OVERLAY = (
    Path(__file__).parent.parent.parent.parent
    / "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production"
)
_REGEL = _OVERLAY / "prometheusrule-image-pull.yaml"


def _alarm() -> dict:
    document = YAML(typ="safe").load(_REGEL.read_text(encoding="utf-8"))
    (groep,) = document["spec"]["groups"]
    (alarm,) = groep["rules"]
    return alarm


def _uitgegeven_metrieken() -> set[str]:
    from opi.core.metrics import OPICollector

    return {familie.name for familie in OPICollector().collect()}


def test_de_expr_noemt_een_metriek_die_de_collector_uitgeeft() -> None:
    uitgegeven = _uitgegeven_metrieken()
    # De kanarie: de collector geeft werkelijk iets uit, anders slaagt de bewering erna
    # gratis op een lege verzameling.
    assert uitgegeven, "de collector geeft niets uit, dus deze grendel meet niets"

    expr = _alarm()["expr"]
    genoemd = {naam for naam in uitgegeven if naam in expr}

    assert genoemd, f"de expr '{expr}' noemt geen enkele metriek die OPI uitgeeft, dus dit alarm kan niet vuren"


def test_de_beschrijving_noemt_de_klassen_die_het_label_werkelijk_kan_hebben() -> None:
    beschrijving = _alarm()["annotations"]["description"]

    for klasse in (IMAGE_PULL_ABSENT, IMAGE_PULL_CAPACITY, IMAGE_PULL_UNDIAGNOSED):
        assert f"reason={klasse}" in beschrijving, (
            f"de beschrijving noemt reason={klasse} niet; dan mist de lezer juist de klasse die zegt wie het oplost"
        )
    # En de metriek waarin die labels staan, want zonder die naam is de instructie niet op
    # te volgen.
    assert "opi_image_pull_failing_pods_by_reason" in beschrijving


def test_de_regel_staat_in_de_kustomization_van_zijn_overlay() -> None:
    """Een resource die er niet in staat rendert stil weg: exit 0 en geen alarm."""
    kustomization = YAML(typ="safe").load((_OVERLAY / "kustomization.yaml").read_text(encoding="utf-8"))

    assert _REGEL.name in kustomization["resources"]
