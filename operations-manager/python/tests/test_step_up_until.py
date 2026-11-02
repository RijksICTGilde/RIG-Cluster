"""Tot wanneer een verse step-up geldt, voor het accountmenu (28-10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from opi.core.step_up import STEP_UP_SESSION_KEY, STEP_UP_WINDOW, has_fresh_step_up, step_up_until
from opi.core.template_helpers import format_dutch_date


def _verzoek(stempel: object) -> SimpleNamespace:
    return SimpleNamespace(session={STEP_UP_SESSION_KEY: stempel} if stempel is not None else {})


def test_een_verse_stempel_geldt_tot_het_einde_van_het_venster() -> None:
    gegeven = datetime.now(UTC) - timedelta(minutes=1)

    assert step_up_until(_verzoek(gegeven.isoformat())) == gegeven + STEP_UP_WINDOW  # type: ignore[arg-type]
    assert has_fresh_step_up(_verzoek(gegeven.isoformat())) is True  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "stempel",
    [None, "geen-datum", 12345, (datetime.now(UTC) - STEP_UP_WINDOW - timedelta(seconds=1)).isoformat()],
    ids=["geen-stempel", "onleesbaar", "geen-tekst", "verlopen"],
)
def test_zonder_verse_stempel_is_er_geen_einde(stempel: object) -> None:
    assert step_up_until(_verzoek(stempel)) is None  # type: ignore[arg-type]
    assert has_fresh_step_up(_verzoek(stempel)) is False  # type: ignore[arg-type]


def test_het_filter_toont_de_tijd_in_amsterdam() -> None:
    assert format_dutch_date(datetime(2026, 10, 28, 12, 32, tzinfo=UTC), only_time=True) == "13:32"
