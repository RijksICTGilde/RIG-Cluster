"""De OOM-query van de tuner kijkt over een bereik, maar niet verder dan nodig.

Waarom een bereik
-----------------
``kube_pod_container_status_last_terminated_reason`` wordt door kube-state-metrics alleen
geexporteerd zolang de gestopte pod BESTAAT. Een container die door de OOM-killer wordt
geveld herstart binnen dezelfde pod, dus daar ziet een momentopname hem nog. Zodra er een
uitrol overheen gaat is hij weg -- en dat is precies wat de tuner zelf veroorzaakt.
Gemeten tegen mimir-prd op 1 september 2026 voor rig-prd-dd-mco: de kale selector gaf
niets terug, ``max_over_time(...[24h])`` gaf de twee pods die die avond waren gestopt.

Waarom niet verder dan nodig
----------------------------
``has_oom_kills`` slaat de deadband over EN dwingt een verhoging van minstens
``current_limit x factor``, ongeacht het gemeten gebruik. Een OOM die de tuner gisteren al
beantwoord heeft zou dus elke ronde opnieuw een verhoging afdwingen. Nagerekend op de vorm
van dd-mco (verklaard 64Mi, override 192Mi, feitelijk gebruik 40Mi): 192 -> 384 -> 576 ->
864Mi, tot het groeiplafond. Dat is de escalatie van asses-k2n/pr-494 opnieuw.

Het venster loopt daarom tot de laatste OOM die de watcher al beantwoord heeft.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from opi.services.resource_tuning_service import _unanswered_oom_window_minutes

VOLLEDIG_VENSTER_UREN = 24


def _project(history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    component: dict[str, Any] = {"reference": "placeholder"}
    if history is not None:
        component["resources"] = {"history": history}
    return {
        "deployments": [
            {
                "name": "productie",
                "cluster": "odcn-production",
                "namespace": "dd-mco",
                "components": [component],
            }
        ]
    }


def _minuten_geleden(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def _venster(project: dict[str, Any]) -> int:
    return _unanswered_oom_window_minutes(project, "productie", "placeholder", VOLLEDIG_VENSTER_UREN)


def test_zonder_beantwoorde_oom_geldt_het_volle_venster() -> None:
    """Er is nog niets beantwoord, dus alles binnen het venster telt."""
    assert _venster(_project()) == VOLLEDIG_VENSTER_UREN * 60


def test_venster_loopt_tot_de_laatste_beantwoorde_oom() -> None:
    """De OOM van drie uur geleden is beantwoord; alleen wat daarna kwam telt nog."""
    project = _project([{"source": "oom-watcher", "timestamp": _minuten_geleden(180)}])

    assert 175 <= _venster(project) <= 181


def test_de_nieuwste_beantwoorde_oom_wint() -> None:
    """Twee tunes: de laatste bepaalt de grens, niet de oudste."""
    project = _project(
        [
            {"source": "oom-watcher", "timestamp": _minuten_geleden(30)},
            {"source": "oom-watcher", "timestamp": _minuten_geleden(600)},
        ]
    )

    assert 25 <= _venster(project) <= 31


def test_alleen_de_watcher_telt_als_antwoord() -> None:
    """Een nachtelijke verlaging beantwoordt geen OOM en mag het venster niet inkorten."""
    project = _project([{"source": "auto-tune", "timestamp": _minuten_geleden(30)}])

    assert _venster(project) == VOLLEDIG_VENSTER_UREN * 60


def test_onleesbare_tijdstempel_kort_het_venster_niet_in() -> None:
    """Faalt naar het volle venster: liever een keer te veel kijken dan een OOM missen."""
    project = _project([{"source": "oom-watcher", "timestamp": "gisteren"}])

    assert _venster(project) == VOLLEDIG_VENSTER_UREN * 60


def test_venster_is_nooit_nul() -> None:
    """Vlak na een tune. Een bereik van 0 is geen geldige PromQL-duur."""
    project = _project([{"source": "oom-watcher", "timestamp": datetime.now(UTC).isoformat()}])

    assert _venster(project) >= 1


def test_de_query_is_een_bereik_en_geen_momentopname() -> None:
    """De vorm van de query die de tuner bouwt, zoals hij in de broncode staat.

    Direct op de bron getoetst en niet via een dubbel: de query is een f-string diep in
    _analyze_component_resources, en juist de VORM ervan is wat hier stuk kan gaan.
    """
    import inspect

    from opi.services import resource_tuning_service

    bron = inspect.getsource(resource_tuning_service._analyze_component_resources)
    oom_blok = bron[bron.index("oom_query = ") : bron.index("oom_results = ")]

    assert "max_over_time(" in oom_blok
    assert re.search(r"\[\{oom_window_minutes\}m\]", oom_blok)
