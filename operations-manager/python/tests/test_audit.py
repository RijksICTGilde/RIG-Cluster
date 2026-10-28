"""Het audittrail (RC-393) tegen een echte Postgres (``orm_db``).

Getoetst worden de vorm van één rij, de weigering, de invariant dat ``subject`` en
``summary`` geen e-mailadres dragen, dat een kapotte sessie ``record_action`` niet laat
gooien, en de afbakening van ``list_actions``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from opi.core.actor import Actor, ActorKind, user_actor
from opi.core.db import session_scope
from opi.core.metrics import OPICollector
from opi.services import (
    audit,
    persistence,  # noqa: F401  -- registreert de ORM-modellen op Base.metadata
)
from opi.services.persistence.notification_events import NotificationEvent
from sqlalchemy import select

if TYPE_CHECKING:
    import pytest


async def _rows() -> list[NotificationEvent]:
    async with session_scope() as db:
        return list((await db.execute(select(NotificationEvent).order_by(NotificationEvent.time))).scalars())


async def _record(**overrides) -> None:
    """``record_action`` met alle argumenten als keyword, zoals ontwerp 8.2 hem vastlegt."""
    argumenten: dict = {
        "actor": user_actor("ops@rijksoverheid.nl"),
        "action": "deployment.delete",
        "resource_path": "project/demo/deployment/live",
        "project": "demo",
        "deployment": "live",
        "environment": "productie",
        "allowed": True,
        "cause": None,
        "grant": "ops@production",
        "step_up": True,
        "status_code": 200,
        "commit_shas": ["abc1234"],
        "task_id": None,
        "origin": "10.0.0.7",
        "fields": {"image": "ghcr.io/x/y:1"},
    }
    argumenten.update(overrides)
    await audit.record_action(**argumenten)


async def test_een_toegestane_actie_levert_een_rij_met_de_juiste_kolommen(orm_db: None) -> None:
    taak = uuid.uuid4()
    await _record(task_id=taak)

    (rij,) = await _rows()
    assert rij.type == audit.TYPE_PERFORMED
    assert rij.severity == audit.SEVERITY_ALLOWED
    assert rij.category == audit.CATEGORY
    assert rij.subject == "demo/live"
    assert rij.project == "demo"
    assert rij.deployment == "live"
    assert rij.actor == "ops@rijksoverheid.nl"
    assert rij.actor_kind == ActorKind.USER.value
    assert rij.origin == "10.0.0.7"
    assert rij.task_id == taak
    assert rij.specversion == "1.0"
    assert rij.source == audit.event_source()
    assert audit.OIN_PLACEHOLDER in rij.source, "zonder OIN is dit geen geldig CloudEvent"
    assert rij.data == {
        "action": "deployment.delete",
        "resource_path": "project/demo/deployment/live",
        "environment": "productie",
        "allowed": True,
        "cause": None,
        "grant": "ops@production",
        "step_up": True,
        "status_code": 200,
        "commit_shas": ["abc1234"],
        "fields": {"image": "ghcr.io/x/y:1"},
    }


async def test_een_weigering_is_denied_met_de_cause_in_data(orm_db: None) -> None:
    await _record(allowed=False, cause="insufficient-role", grant=None, status_code=403, step_up=False)

    (rij,) = await _rows()
    assert rij.type == audit.TYPE_DENIED
    assert rij.severity == audit.SEVERITY_DENIED
    assert rij.data["cause"] == "insufficient-role"
    assert "geweigerd" in rij.summary


async def test_subject_en_summary_dragen_nooit_een_apenstaartje(orm_db: None) -> None:
    """Punt 7, regel 2: geen persoonsgegeven in de context-attributen."""
    await _record()
    await _record(allowed=False, cause="step-up-required", grant=None)

    for rij in await _rows():
        assert "@" not in (rij.subject or "")
        assert "@" not in rij.summary


async def test_summary_noemt_de_rol_van_de_grant(orm_db: None) -> None:
    await _record()

    (rij,) = await _rows()
    assert rij.summary == "demo/live: deployment.delete door ops"


async def test_een_projectactie_zonder_deployment_heeft_het_project_als_subject(orm_db: None) -> None:
    await _record(deployment=None, resource_path="project/demo", environment=None)

    (rij,) = await _rows()
    assert rij.subject == "demo"
    assert rij.deployment is None


async def test_een_kapotte_sessie_gooit_niet_en_telt_op(orm_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    voor = audit.write_failures()

    def kapot() -> None:
        raise RuntimeError("database weg")

    monkeypatch.setattr(audit, "session_scope", kapot)
    await _record()

    assert audit.write_failures() == voor + 1
    assert await _rows() == []


async def test_list_actions_geeft_alleen_het_eigen_project_nieuwste_eerst(orm_db: None) -> None:
    await _record(project="demo", deployment=None, action="project.refresh")
    await _record(project="ander", deployment=None, action="project.refresh")
    await _record(project="demo", deployment=None, action="project.delete")

    rijen = await audit.list_actions("demo", limit=10)
    assert [rij.data["action"] for rij in rijen] == ["project.delete", "project.refresh"]
    assert {rij.project for rij in rijen} == {"demo"}


async def test_list_actions_pagineert_op_time(orm_db: None) -> None:
    nu = datetime.now(UTC)
    async with session_scope() as db:
        for index in range(3):
            db.add(
                NotificationEvent(
                    source=audit.event_source(),
                    type=audit.TYPE_PERFORMED,
                    subject="demo",
                    time=nu - timedelta(minutes=index),
                    category=audit.CATEGORY,
                    severity=audit.SEVERITY_ALLOWED,
                    cluster="local",
                    project="demo",
                    actor_kind=ActorKind.USER.value,
                    summary=f"demo: actie {index}",
                    dedup_key=f"audit:actie{index}:demo:allowed",
                    data={"action": f"actie{index}"},
                )
            )

    eerste = await audit.list_actions("demo", limit=1)
    assert [rij.data["action"] for rij in eerste] == ["actie0"]

    volgende = await audit.list_actions("demo", limit=5, before=eerste[0].time)
    assert [rij.data["action"] for rij in volgende] == ["actie1", "actie2"]


async def test_een_actor_zonder_identiteit_laat_de_kolom_leeg(orm_db: None) -> None:
    await _record(actor=Actor(ActorKind.SCHEDULER), origin=None)

    (rij,) = await _rows()
    assert rij.actor is None
    assert rij.actor_kind == ActorKind.SCHEDULER.value


async def test_de_foutregel_draagt_geen_nieuwe_regel_uit_de_aanroeper(
    orm_db: None, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""Een ``\n`` in project zou anders een tweede, verzonnen logregel opleveren."""

    def kapot() -> None:
        raise RuntimeError("database weg")

    # Via de sessie, niet via een te lange kolomwaarde: die kapt record_action nu zelf af.
    monkeypatch.setattr(audit, "session_scope", kapot)
    with caplog.at_level(logging.ERROR, logger="opi.services.audit"):
        await _record(project="demo\nERROR verzonnen regel")

    (regel,) = [record.getMessage() for record in caplog.records]
    assert "\n" not in regel


async def test_een_te_lange_actor_of_origin_verliest_de_auditregel_niet(orm_db: None) -> None:
    """Een waarde boven de kolombreedte liet de INSERT falen, en dan was er geen rij.

    Zo laat een aanroeper die ``origin`` uit een proxy-header overneemt zich niet
    ongemerkt uit het trail schrijven met een lange header.
    """
    voor = audit.write_failures()
    await _record(actor=user_actor("a" * 290 + "@rijksoverheid.nl"), origin="10.0.0.7, " * 7)

    (rij,) = await _rows()
    assert len(rij.origin or "") == 64
    assert len(rij.actor or "") == 255
    assert audit.write_failures() == voor


def test_write_failures_metriek_telt_mee_in_de_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    """De teller uit ontwerp 8.2 staat ook echt op de scrape, naast de bestaande in metrics.py."""
    monkeypatch.setattr(audit, "_write_failures", 3)

    families = {familie.name: familie for familie in OPICollector().collect()}
    (sample,) = families["opi_audit_write_failures"].samples
    assert sample.value == 3
