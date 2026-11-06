"""The step-up state of an API credential, on a real database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opi.core.db import session_scope
from opi.core.step_up import STEP_UP_WINDOW
from opi.services.api_step_up_service import ApiStepUpService
from opi.services.persistence.api_step_ups import ApiStepUp
from sqlalchemy import select, update

EMAIL = "iemand@rijksoverheid.nl"
SID = "sid:sessie-1"

#: conftest patches ``is_fresh`` for every test; this is the method itself.
_is_fresh = ApiStepUpService.is_fresh

pytestmark = pytest.mark.asyncio


async def _age(credential: str, seconds_past_the_window: float) -> None:
    moment = datetime.now(UTC) - STEP_UP_WINDOW - timedelta(seconds=seconds_past_the_window)
    async with session_scope() as session:
        await session.execute(update(ApiStepUp).where(ApiStepUp.credential == credential).values(stepped_up_at=moment))


async def test_a_recorded_step_up_holds_for_this_credential_only(orm_db: Any) -> None:
    service = ApiStepUpService()
    until = await service.record(SID, EMAIL)

    assert until > datetime.now(UTC)
    assert await _is_fresh(service, SID, EMAIL) is True
    assert await _is_fresh(service, "sid:een-andere-sessie", EMAIL) is False
    assert await _is_fresh(service, SID, "ander@rijksoverheid.nl") is False


async def test_the_window_does_not_slide_along_with_reading(orm_db: Any) -> None:
    service = ApiStepUpService()
    await service.record(SID, EMAIL)
    await _is_fresh(service, SID, EMAIL)
    await _age(SID, 1)

    assert await _is_fresh(service, SID, EMAIL) is False


async def test_a_new_code_starts_a_new_window(orm_db: Any) -> None:
    service = ApiStepUpService()
    await service.record(SID, EMAIL)
    await _age(SID, 1)

    await service.record(SID, EMAIL)

    assert await _is_fresh(service, SID, EMAIL) is True


async def test_ending_closes_it(orm_db: Any) -> None:
    service = ApiStepUpService()
    await service.record(SID, EMAIL)

    await service.clear(SID)

    assert await _is_fresh(service, SID, EMAIL) is False


async def test_a_new_record_clears_expired_rows_of_others(orm_db: Any) -> None:
    service = ApiStepUpService()
    await service.record("sid:oud", EMAIL)
    await _age("sid:oud", 1)

    await service.record(SID, EMAIL)

    async with session_scope() as session:
        credentials = set((await session.execute(select(ApiStepUp.credential))).scalars())
    assert credentials == {SID}
