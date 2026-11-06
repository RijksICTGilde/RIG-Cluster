"""Het audittrail (RC-393) tegen een echte Postgres (``orm_db``).

Getoetst worden de vorm van één rij, de weigering, de invariant dat ``subject`` en
``summary`` geen e-mailadres dragen, dat een kapotte sessie ``record_action`` niet laat
gooien, en de afbakening van ``list_actions``: het eigen project, het zoeken, de periode
en het bladeren met een cursor.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opi.core.actor import Actor, ActorKind, user_actor
from opi.core.db import session_scope
from opi.core.metrics import OPICollector
from opi.services import (
    audit,
    persistence,  # noqa: F401  -- registreert de ORM-modellen op Base.metadata
)
from opi.services.persistence.notification_events import NotificationEvent
from sqlalchemy import select


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


async def test_een_step_up_poging_heeft_geen_project_en_geen_subject(orm_db: None) -> None:
    """``record_step_up_attempt`` (``opi/services/audit.py``): geen project, dus ``_subject``
    geeft ``None`` en de rij komt niet via ``list_actions`` bij een project terecht."""
    await audit.record_step_up_attempt("beheer@rijksoverheid.nl", allowed=True, cause=None, origin="10.0.0.9")

    (rij,) = await _rows()
    assert rij.project is None
    assert rij.subject is None
    assert rij.data["action"] == "step-up.verify"
    assert rij.actor == "beheer@rijksoverheid.nl"
    assert await audit.list_actions("demo") == []


async def test_een_geweigerde_step_up_is_denied_met_de_reden(orm_db: None) -> None:
    await audit.record_step_up_attempt(
        "beheer@rijksoverheid.nl", allowed=False, cause="code-invalid", origin="10.0.0.9"
    )

    (rij,) = await _rows()
    assert rij.type == audit.TYPE_DENIED
    assert rij.data["cause"] == "code-invalid"


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


# --- zoeken, periode en bladeren (RC-405) -----------------------------------
#
# English per RC-405: new names, test names and comments are English, also in this
# otherwise Dutch module.


async def _insert(**overrides: Any) -> NotificationEvent:
    """One row straight into the table, so a test can pick the field it searches on."""
    fields: dict[str, Any] = {
        "source": audit.event_source(),
        "type": audit.TYPE_PERFORMED,
        "subject": "demo",
        "time": datetime.now(UTC),
        "category": audit.CATEGORY,
        "severity": audit.SEVERITY_ALLOWED,
        "cluster": "local",
        "project": "demo",
        "actor": "ops@rijksoverheid.nl",
        "actor_kind": ActorKind.USER.value,
        "origin": "10.0.0.7",
        "summary": "demo: project.refresh door ops",
        "dedup_key": "audit:project.refresh:demo:allowed",
        "data": {"action": "project.refresh", "allowed": True},
    }
    fields.update(overrides)
    event = NotificationEvent(**fields)
    async with session_scope() as db:
        db.add(event)
    return event


@pytest.mark.parametrize(
    ("row", "term"),
    [
        ({"actor": "jansen@rijksoverheid.nl"}, "jansen"),
        ({"actor_kind": "service-account"}, "service-account"),
        ({"summary": "demo/live: deployment.delete door ops"}, "deployment.delete"),
        ({"deployment": "kantoortuin"}, "kantoortuin"),
        ({"data": {"action": "component.write"}}, "component.write"),
        ({"data": {"resource_path": "project/demo/component/web"}}, "component/web"),
        ({"data": {"cause": "step-up-required"}}, "step-up"),
        ({"data": {"grant": "ops@production"}}, "ops@production"),
        ({"data": {"commit_shas": ["9f1c2b3a"]}}, "9f1c2b3a"),
        ({"data": {"fields": {"image": "ghcr.io/x/y:1"}}}, "ghcr.io/x/y"),
    ],
)
async def test_search_finds_a_row_on_every_shown_field(orm_db: None, row: dict[str, Any], term: str) -> None:
    await _insert(**row)
    await _insert(actor="niemand@rijksoverheid.nl", summary="demo: niets", data={"action": "project.read"})

    found = await audit.list_actions("demo", query=term)
    assert len(found) == 1


async def test_search_is_case_insensitive(orm_db: None) -> None:
    await _insert(actor="Jansen@Rijksoverheid.nl")

    assert len(await audit.list_actions("demo", query="JANSEN")) == 1
    assert len(await audit.list_actions("demo", query="jansen")) == 1


async def test_wildcards_in_the_term_are_literal_characters(orm_db: None) -> None:
    """``a_b`` must not match ``axb``, and ``%`` must not match everything."""
    await _insert(summary="demo: axb")

    assert await audit.list_actions("demo", query="a_b") == []
    assert await audit.list_actions("demo", query="%") == []
    assert len(await audit.list_actions("demo", query="axb")) == 1


async def test_search_does_not_reach_the_origin_column(orm_db: None) -> None:
    """``origin`` is a personal detail: a reader must not be able to probe an IP with it."""
    await _insert(origin="10.11.12.13")

    assert await audit.list_actions("demo", query="10.11.12.13") == []


@pytest.mark.parametrize("term", ["allowed", "action", "status_code"])
async def test_a_json_key_does_not_match_every_row(orm_db: None, term: str) -> None:
    """Guards that ``data`` is searched per key and never as ``data::text``."""
    await _insert(data={"action": "project.refresh", "allowed": True, "status_code": 200})

    assert await audit.list_actions("demo", query=term) == []


async def test_search_stays_inside_the_project(orm_db: None) -> None:
    await _insert(project="ander", subject="ander", actor="jansen@rijksoverheid.nl")

    assert await audit.list_actions("demo", query="jansen") == []
    assert len(await audit.list_actions("ander", query="jansen")) == 1


async def test_the_period_is_closed_on_since_and_open_on_until(orm_db: None) -> None:
    since = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    until = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    for moment, name in (
        (since - timedelta(seconds=1), "ervoor"),
        (since, "op-since"),
        (until - timedelta(seconds=1), "ervoor-until"),
        (until, "op-until"),
    ):
        await _insert(time=moment, data={"action": name})

    found = await audit.list_actions("demo", since=since, until=until)
    assert [row.data["action"] for row in found] == ["ervoor-until", "op-since"]


async def test_paging_on_time_and_id_passes_every_row_exactly_once(orm_db: None) -> None:
    """Three rows share a timestamp, so a cursor on ``time`` alone would drop one."""
    moment = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    await _insert(time=moment + timedelta(minutes=2), data={"action": "los0"})
    await _insert(time=moment + timedelta(minutes=1), data={"action": "los1"})
    for index in range(3):
        await _insert(time=moment, data={"action": f"gelijk{index}"})

    def _cursor(row: NotificationEvent) -> audit.AuditCursor:
        return audit.AuditCursor(time=row.time, id=row.id)

    pages: list[list[str]] = []
    cursor: audit.AuditCursor | None = None
    rows: list[NotificationEvent] = []
    while True:
        page = await audit.list_actions("demo", limit=2, before=cursor)
        if not page:
            break
        pages.append([row.data["action"] for row in page])
        rows.append(page[0])
        cursor = _cursor(page[-1])

    passed = [action for page in pages for action in page]
    assert len(passed) == len(set(passed)) == 5
    assert passed[:2] == ["los0", "los1"]
    assert sorted(passed[2:]) == ["gelijk0", "gelijk1", "gelijk2"]

    # Back up again: every page comes back in the same order, without a gap.
    for index in range(len(pages) - 1, 0, -1):
        terug = await audit.list_actions("demo", limit=2, after=_cursor(rows[index]))
        assert [row.data["action"] for row in terug] == pages[index - 1]


async def test_before_and_after_together_is_a_programming_error(orm_db: None) -> None:
    cursor = audit.AuditCursor(time=datetime.now(UTC), id=uuid.uuid4())
    with pytest.raises(ValueError, match="mutually exclusive"):
        await audit.list_actions("demo", before=cursor, after=cursor)
