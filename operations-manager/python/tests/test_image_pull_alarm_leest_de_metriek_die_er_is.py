"""Het alarm leest de metriek die OPI werkelijk uitgeeft, en de klassen die er zijn.

Sinds RC-243 schakelt OPI een component dat zijn image niet kan ophalen niet meer uit, en
hoort zo'n pod niet stil in ImagePullBackOff te blijven hangen. Dit alarm is daar een deel
van, en het hangt aan drie koppelingen die stil kunnen verschuiven, want niets anders legt
ze langs elkaar:

- de naam in ``expr`` tegen de naam die de collector uitgeeft. Hernoemt iemand de gauge,
  dan vuurt het alarm nooit meer: ``opi_image_pull_failing_pods > 0`` is op een niet
  bestaande serie niet rood maar leeg.
- de reden-klassen in de beschrijving tegen de constanten in de code. Daar hangt de
  handelingsinstructie aan (wie het oplost), dus een klasse die niet meer zo heet stuurt
  de lezer naar een label dat er niet is.
- de regel in de kustomization van zijn eigen overlay. Een resource die daar niet in staat
  rendert stil weg: geen fout, geen alarm.

En er is een tweede alarm, omdat de telling waar het eerste op afgaat ook nul is als er
niemand gekeken heeft: de momentopname begint leeg, dus een cluster waar de cluster-brede
pod-lezing geweigerd wordt meldt een schone nul. Dat alarm hangt aan dezelfde koppeling,
op een gauge die de collector pas sinds deze taak uitgeeft.

Wat hier NIET in staat, en wat hier ook niet te meten is: of de regels ergens geevalueerd
worden. Dat hangt aan een productiefeit en staat als open punt in
features/image-pull-backoff-detection.md en in de kop van de PrometheusRule zelf.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from opi.handlers.project_file_handler import IMAGE_PULL_ABSENT, IMAGE_PULL_CAPACITY, IMAGE_PULL_UNDIAGNOSED
from ruamel.yaml import YAML

_OVERLAY = (
    Path(__file__).parent.parent.parent.parent
    / "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production"
)
_REGEL = _OVERLAY / "prometheusrule-image-pull.yaml"


def _alarmen() -> dict[str, dict]:
    document = YAML(typ="safe").load(_REGEL.read_text(encoding="utf-8"))
    (groep,) = document["spec"]["groups"]
    return {regel["alert"]: regel for regel in groep["rules"]}


def _alarm() -> dict:
    return _alarmen()["ZadComponentKanImageNietOphalen"]


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


class TestHetAlarmOpDeObservatieZelf:
    """Zonder dit alarm is "we konden niet kijken" niet te onderscheiden van "niets aan de hand"."""

    def test_de_expr_leest_de_tijdstempel_die_de_collector_uitgeeft(self) -> None:
        alarm = _alarmen()["ZadImagePullObservatieOntbreekt"]

        assert "opi_image_pull_observed_timestamp" in _uitgegeven_metrieken(), (
            "de collector geeft de tijdstempel niet uit, dus dit alarm staat op een lege serie"
        )
        assert "opi_image_pull_observed_timestamp" in alarm["expr"]

    def test_de_expr_dekt_zowel_verouderd_als_nog_nooit_gelukt(self) -> None:
        """Het geval dat op productie het risico is, is NOG NOOIT gelukt, niet verouderd.

        Een leeftijdsdrempel dekt dat mee, omdat een tijdstempel die nooit gezet is op 0
        staat en de epoch ruim langer dan de drempel geleden is. Die eigenschap is niet
        vanzelfsprekend, dus hij staat hier: een herschrijving naar een vorm die alleen op
        VERANDERING kijkt (``changes()``, ``delta()``) laat juist het ene geval lopen
        waarvoor dit alarm bestaat.
        """
        expr = _alarmen()["ZadImagePullObservatieOntbreekt"]["expr"].strip()

        gevonden = re.fullmatch(r"time\(\) - (\w+) > (\d+)", expr)
        assert gevonden, f"de expr '{expr}' is geen leeftijdsdrempel op een tijdstempel"
        assert gevonden.group(1) == "opi_image_pull_observed_timestamp"

        drempel = int(gevonden.group(2))
        nu = time.time()
        assert nu - 0.0 > drempel, "de drempel is zo groot dat 'nog nooit gelukt' er niet onder valt"
        assert not nu - nu > drempel, "een ronde die net lukte mag niet alarmeren"

    def test_de_beschrijving_stuurt_naar_de_lezing_die_niet_nagemeten_is(self) -> None:
        """De waarschijnlijkste oorzaak op odcn-production staat in de melding zelf.

        Of een cluster-brede pod-lezing daar via Capsule Proxy mag is niet nagemeten
        (features/oom-pod-watch.md), dus wie dit alarm krijgt hoort meteen te weten dat
        dat de eerste vraag is.
        """
        beschrijving = _alarmen()["ZadImagePullObservatieOntbreekt"]["annotations"]["description"]

        assert "can-i" in beschrijving
        assert "opi_image_pull_failing_pods" in beschrijving
