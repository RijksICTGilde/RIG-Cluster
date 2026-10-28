"""De bewaarlus van het audittrail (RC-393) tegen een echte Postgres (``orm_db``).

Punt 6 van ``features/futures/gebeurtenissen-vastleggen-en-melden.md``: na 90 dagen gaan
``actor`` en ``origin`` op NULL, na een jaar gaat de rij weg.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from opi.core.actor import ActorKind, scheduler_actor
from opi.core.audit_retention_scheduler import AuditRetentionScheduler
from opi.core.db import session_scope
from opi.services import (
    audit,
    persistence,  # noqa: F401  -- registreert de ORM-modellen op Base.metadata
)
from opi.services.persistence.notification_events import NotificationEvent
from sqlalchemy import select


async def _leg_neer(dagen_oud: int, naam: str) -> None:
    async with session_scope() as db:
        db.add(
            NotificationEvent(
                source=audit.event_source(),
                type=audit.TYPE_PERFORMED,
                subject="demo",
                time=datetime.now(UTC) - timedelta(days=dagen_oud),
                category=audit.CATEGORY,
                severity=audit.SEVERITY_ALLOWED,
                cluster="local",
                project="demo",
                actor="ops@rijksoverheid.nl",
                actor_kind=ActorKind.USER.value,
                origin="10.0.0.7",
                summary=f"demo: {naam}",
                dedup_key=f"audit:{naam}:demo:allowed",
                data={"action": naam},
            )
        )


async def _rijen() -> dict[str, NotificationEvent]:
    async with session_scope() as db:
        rijen = (await db.execute(select(NotificationEvent))).scalars()
        return {rij.data["action"]: rij for rij in rijen}


async def test_een_ronde_pseudonimiseert_na_90_dagen_en_ruimt_na_een_jaar(orm_db: None) -> None:
    await _leg_neer(89, "jong")
    await _leg_neer(91, "oud")
    await _leg_neer(366, "stokoud")

    uitkomst = await audit.run_retention()

    rijen = await _rijen()
    assert rijen["jong"].actor == "ops@rijksoverheid.nl"
    assert rijen["jong"].origin == "10.0.0.7"

    assert rijen["oud"].actor is None
    assert rijen["oud"].origin is None
    assert rijen["oud"].actor_kind == ActorKind.USER.value, "de soort blijft staan"
    assert rijen["oud"].summary == "demo: oud"
    assert rijen["oud"].project == "demo"

    assert "stokoud" not in rijen
    assert uitkomst == {"pseudonymized": 1, "deleted": 1}


async def test_een_tweede_ronde_raakt_een_al_gepseudonimiseerde_rij_niet_meer(orm_db: None) -> None:
    """De sweep-index krimpt: een verwerkte rij valt uit het predicaat."""
    await _leg_neer(91, "oud")
    await audit.run_retention()

    assert await audit.run_retention() == {"pseudonymized": 0, "deleted": 0}


async def test_de_scheduler_draait_een_ronde(orm_db: None) -> None:
    await _leg_neer(91, "oud")

    assert await AuditRetentionScheduler().run_once() == {"pseudonymized": 1, "deleted": 0}


async def test_stoppen_zonder_gestart_te_zijn_is_veilig() -> None:
    await AuditRetentionScheduler().stop()


async def test_start_zet_de_scheduler_actor_en_stop_annuleert_de_lus() -> None:
    """Patroon van ``test_reconciliation_scheduler.py``: de achtergrondtaak start en stopt."""
    scheduler = AuditRetentionScheduler()

    with patch("opi.core.audit_retention_scheduler.set_actor") as mock_set_actor:
        await scheduler.start()
        await asyncio.sleep(0)
        assert scheduler._task is not None
        await scheduler.stop()

    mock_set_actor.assert_called_once_with(scheduler_actor("audit-retention"))
    assert scheduler._task is None
