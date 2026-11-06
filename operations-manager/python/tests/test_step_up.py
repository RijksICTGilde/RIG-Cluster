"""Wanneer een handeling een verse step-up vraagt, en hoe vers die is (RC-332).

Dekt ``opi/core/step_up.py``: het venster dat niet meeschuift (besluit 8.5), de vier
soorten uit :class:`StepUpKind`, en wat een opslag daarvan afleidt
(:func:`save_requires_step_up`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun import freeze_time
from opi.core.project_environment import Omgeving
from opi.core.step_up import (
    STEP_UP_SESSION_KEY,
    STEP_UP_WINDOW,
    StepUpAction,
    StepUpKind,
    has_fresh_step_up,
    record_step_up,
    requires_production_step_up,
    save_requires_step_up,
)

NU = "2026-10-18T10:00:00+00:00"


def _request(session: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(session=session if session is not None else {})


# ---------------------------------------------------------------------------
# has_fresh_step_up / record_step_up: het venster
# ---------------------------------------------------------------------------


def test_geen_stempel_in_de_sessie_is_niet_vers() -> None:
    assert has_fresh_step_up(_request()) is False


@freeze_time(NU)
def test_record_step_up_zet_de_stempel_in_de_sessie() -> None:
    request = _request()
    record_step_up(request)
    assert request.session[STEP_UP_SESSION_KEY] == datetime.now(UTC).isoformat()
    assert has_fresh_step_up(request) is True


def test_net_voor_het_einde_van_het_venster_nog_vers() -> None:
    with freeze_time(NU):
        request = _request()
        record_step_up(request)

    with freeze_time(datetime.fromisoformat(NU) + STEP_UP_WINDOW - timedelta(minutes=1)):
        assert has_fresh_step_up(request) is True


def test_na_het_venster_niet_meer_vers() -> None:
    with freeze_time(NU):
        request = _request()
        record_step_up(request)

    with freeze_time(datetime.fromisoformat(NU) + STEP_UP_WINDOW + timedelta(minutes=1)):
        assert has_fresh_step_up(request) is False


def test_een_tweede_handeling_binnen_het_venster_verlengt_het_niet() -> None:
    """Besluit 8.5: het venster schuift niet mee. Alleen ``record_step_up`` herzet de stempel."""
    with freeze_time(NU):
        request = _request()
        record_step_up(request)

    # Een volgende handeling binnen het venster leest alleen, ze schrijft niets.
    with freeze_time(datetime.fromisoformat(NU) + STEP_UP_WINDOW / 2):
        assert has_fresh_step_up(request) is True

    # Na de OORSPRONKELIJKE code is het venster dicht, ook al was er halverwege een
    # geslaagde handeling.
    with freeze_time(datetime.fromisoformat(NU) + STEP_UP_WINDOW + timedelta(minutes=1)):
        assert has_fresh_step_up(request) is False


def test_onleesbare_stempel_is_niet_vers(caplog: pytest.LogCaptureFixture) -> None:
    request = _request({STEP_UP_SESSION_KEY: "geen-datum"})
    with caplog.at_level("WARNING", logger="opi.core.step_up"):
        assert has_fresh_step_up(request) is False
    assert any("Onleesbare step-up" in r.getMessage() for r in caplog.records)


def test_stempel_van_het_verkeerde_type_is_niet_vers() -> None:
    assert has_fresh_step_up(_request({STEP_UP_SESSION_KEY: 12345})) is False


# ---------------------------------------------------------------------------
# requires_production_step_up: de drie soorten
# ---------------------------------------------------------------------------


def _project(deployments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"name": "demo", "deployments": deployments or []}


def test_deployment_met_productiemarkering() -> None:
    project = _project([{"name": "prod", "environment": "productie"}])
    assert requires_production_step_up(StepUpKind.DEPLOYMENT, project, "prod") is True


def test_deployment_zonder_markering_is_ontwikkeling() -> None:
    project = _project([{"name": "d1"}])
    assert requires_production_step_up(StepUpKind.DEPLOYMENT, project, "d1") is False


def test_andere_deployment_in_hetzelfde_project_blijft_buiten_schot() -> None:
    project = _project([{"name": "prod", "environment": "productie"}, {"name": "acc"}])
    assert requires_production_step_up(StepUpKind.DEPLOYMENT, project, "acc") is False


def test_onbekende_deployment_naam_vraagt_geen_step_up() -> None:
    project = _project([{"name": "prod", "environment": "productie"}])
    assert requires_production_step_up(StepUpKind.DEPLOYMENT, project, "nope") is False


def test_component_gebruikt_door_een_productiedeployment() -> None:
    project = _project([{"name": "prod", "environment": "productie", "components": [{"reference": "web"}]}])
    assert requires_production_step_up(StepUpKind.COMPONENT, project, "web") is True


def test_component_alleen_gebruikt_buiten_productie() -> None:
    project = _project([{"name": "acc", "environment": "acceptatie", "components": [{"reference": "web"}]}])
    assert requires_production_step_up(StepUpKind.COMPONENT, project, "web") is False


def test_component_door_niemand_gebruikt() -> None:
    project = _project([{"name": "prod", "environment": "productie"}])
    assert requires_production_step_up(StepUpKind.COMPONENT, project, "ongebruikt") is False


@pytest.mark.parametrize(
    ("before", "after", "verwacht"),
    [
        (Omgeving.PRODUCTIE, Omgeving.ACCEPTATIE, True),
        (Omgeving.ACCEPTATIE, Omgeving.PRODUCTIE, True),
        # Between the other stages is writing a deployment, not setting the marking (ffa2bd448).
        (Omgeving.ACCEPTATIE, Omgeving.TEST, False),
        (Omgeving.PRODUCTIE, Omgeving.PRODUCTIE, False),
    ],
)
def test_environment_kind_leest_elke_markeringswijziging(before: Omgeving, after: Omgeving, verwacht: bool) -> None:
    assert requires_production_step_up(StepUpKind.ENVIRONMENT, {}, change=({"d1": before}, {"d1": after})) is verwacht


def test_environment_kind_zonder_change_vraagt_geen_step_up() -> None:
    assert requires_production_step_up(StepUpKind.ENVIRONMENT, {}) is False


def test_environment_kind_laat_een_verdwenen_deployment_aan_de_verwijderregel() -> None:
    """Een deployment die uit de lijst valt heeft zijn EIGEN regel.

    ``save_requires_step_up`` leest het verwijderen eerst, met een eigen tekst; hem hier
    ook als markeringswijziging tellen zou het verwijderen van een productiedeployment
    'de OTAP-markering zetten' laten heten.
    """
    change = ({"d1": Omgeving.PRODUCTIE}, {})
    assert requires_production_step_up(StepUpKind.ENVIRONMENT, {}, change=change) is False


# ---------------------------------------------------------------------------
# save_requires_step_up: wat een opslag van voor en na afleidt
# ---------------------------------------------------------------------------


def test_verwijderen_van_een_productiedeployment_vraagt_step_up() -> None:
    before = _project([{"name": "prod", "environment": "productie"}])
    assert save_requires_step_up(before, _project()) == "de deployment 'prod' te verwijderen"


def test_verwijderen_van_een_niet_productiedeployment_vraagt_niets() -> None:
    before = _project([{"name": "acc", "environment": "acceptatie"}])
    assert save_requires_step_up(before, _project()) is None


def test_verwijderen_van_een_component_dat_in_productie_draait() -> None:
    before = {
        "name": "demo",
        "deployments": [{"name": "prod", "environment": "productie", "components": [{"reference": "web"}]}],
        "components": [{"name": "web"}],
    }
    after = {"name": "demo", "deployments": before["deployments"], "components": []}
    assert save_requires_step_up(before, after) == "het component 'web' te verwijderen"


def test_markering_verlagen_vraagt_step_up() -> None:
    before = _project([{"name": "d1", "environment": "productie"}])
    after = _project([{"name": "d1", "environment": "acceptatie"}])
    assert save_requires_step_up(before, after) == StepUpAction.MARKING_SET.phrase


def test_geen_wijziging_vraagt_niets() -> None:
    project = _project([{"name": "prod", "environment": "productie"}])
    assert save_requires_step_up(project, dict(project)) is None


def test_markering_verhogen_naar_productie_vraagt_ook_step_up() -> None:
    """Ontwerp paragraaf 7: zetten en verlagen vragen hetzelfde."""
    before = _project([{"name": "d1", "environment": "acceptatie"}])
    after = _project([{"name": "d1", "environment": "productie"}])
    assert save_requires_step_up(before, after) == StepUpAction.MARKING_SET.phrase


def test_deployment_verwijderen_gaat_voor_de_markering_als_beide_spelen() -> None:
    """Bij meerdere redenen levert de functie de EERSTE relevante op, niet alles."""
    before = _project([{"name": "prod", "environment": "productie"}, {"name": "d2", "environment": "productie"}])
    after = _project([{"name": "d2", "environment": "acceptatie"}])
    assert save_requires_step_up(before, after) == "de deployment 'prod' te verwijderen"
