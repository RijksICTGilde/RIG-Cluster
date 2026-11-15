"""The audit trail: who did what, to what, and whether it was allowed.

One row per allowed write action and per refusal, in the ``notification_events`` table. This
module is the only writer of that table and the only thing that updates it, and that only
through the retention loop below.

The caller of ``record_action`` is the hook around the authorization decision;
``list_actions`` reads one project's trail back. Retention: after 90 days ``actor`` and
``origin`` go to NULL, after a year the row goes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from prometheus_client.core import CounterMetricFamily
from sqlalchemy import Text, cast, delete, literal, or_, select, tuple_, update

from opi.core.actor import user_actor
from opi.core.authz.actions import Action
from opi.core.config import settings
from opi.core.db import session_scope
from opi.core.flow_id import get_flow_id
from opi.core.logging_redact import sanitize_for_log
from opi.core.metrics import register_metric_source
from opi.services.persistence.notification_events import NotificationEvent

if TYPE_CHECKING:
    import uuid

    from prometheus_client.core import Metric
    from sqlalchemy.sql.elements import ColumnElement

    from opi.core.actor import Actor

logger = logging.getLogger(__name__)

#: The two types this module writes. A closed set, like ``TaskType``.
TYPE_PERFORMED = "nl.rig.zad.action.performed.v1"
TYPE_DENIED = "nl.rig.zad.action.denied.v1"
ACTION_TYPES = (TYPE_PERFORMED, TYPE_DENIED)

#: The event category these rows carry.
CATEGORY = "beheer-en-beveiliging"

SEVERITY_ALLOWED = "informational"
SEVERITY_DENIED = "actionable"

#: While no OIN has been assigned, ``source`` carries this placeholder and the row is
#: CloudEvents-shaped but not a valid CloudEvent. An exporter should refuse it.
OIN_PLACEHOLDER = "oin-nog-niet-toegekend"

#: How many rows a reader gets at a time, and the ceiling on that. ``list_actions`` accepts
#: any ``limit``; the route is the guard that clips it here. Paging goes through ``before``,
#: because an offset still makes the database sort every skipped row.
LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200

#: The two clocks on the same row.
PSEUDONYMIZE_AFTER = timedelta(days=90)
DELETE_AFTER = timedelta(days=365)

_write_failures = 0


def write_failures() -> int:
    """The number of failed audit writes since startup."""
    return _write_failures


def event_source() -> str:
    """The ``source`` of an event from this instance."""
    return f"urn:nld:oin:{settings.EVENT_SOURCE_OIN}:systeem:zad-{settings.CLUSTER_MANAGER}"


def _subject(project: str | None, deployment: str | None) -> str | None:
    """``<project>`` or ``<project>/<deployment>``, never an email address."""
    if project is None:
        return None
    return f"{project}/{deployment}" if deployment else project


def _summary(subject: str | None, action: str, allowed: bool, cause: str | None, grant: str | None) -> str:
    """One Dutch line, without the actor: this field may carry no personal data either."""
    target = subject or "platform"
    if allowed:
        # The role out of the grant and not the grant itself: that carries an ``@``, which is
        # exactly the character the no-personal-data invariant checks on.
        role = grant.split("@", 1)[0] if grant else None
        return f"{target}: {action} door {role}" if role else f"{target}: {action}"
    return f"{target}: {action} geweigerd ({cause})" if cause else f"{target}: {action} geweigerd"


def _dedup_key(action: str, subject: str | None, allowed: bool, cause: str | None) -> str:
    """The key the notification side groups on: the kind, not the case.

    Comply-or-explain: NOT through ``log_watcher.signature()``, which normalises a free log
    line by stripping linked identifiers out. These fields are already normalised, and that
    function would turn them into ``X``.
    """
    outcome = "allowed" if allowed else (cause or "denied")
    return f"audit:{action}:{subject or '-'}:{outcome}"[:160]


async def record_step_up_attempt(email: str, *, allowed: bool, cause: str | None, origin: str) -> None:
    """Leave a trail of a step-up: a refused code is a security signal, not noise."""
    await record_action(
        actor=user_actor(email),
        action=Action.STEP_UP_VERIFY.value,
        resource_path="step-up",
        project=None,
        deployment=None,
        environment=None,
        allowed=allowed,
        cause=cause,
        grant=None,
        step_up=allowed,
        status_code=None,
        commit_shas=[],
        task_id=None,
        origin=origin,
    )


async def record_action(
    *,
    actor: Actor,
    action: str,
    resource_path: str,
    project: str | None,
    deployment: str | None,
    environment: str | None,
    allowed: bool,
    cause: str | None,
    grant: str | None,
    step_up: bool,
    status_code: int | None,
    commit_shas: list[str],
    task_id: uuid.UUID | None,
    origin: str | None,
    fields: dict[str, object] | None = None,
) -> None:
    """Record one decision. Never raises: a broken audit holds up no rollout."""
    subject = _subject(project, deployment)
    data: dict[str, Any] = {
        "action": action,
        "resource_path": resource_path,
        "environment": environment,
        "allowed": allowed,
        "cause": cause,
        "grant": grant,
        "step_up": step_up,
        "status_code": status_code,
        "commit_shas": commit_shas,
    }
    if fields is not None:
        data["fields"] = fields

    event = NotificationEvent(
        source=event_source(),
        type=TYPE_PERFORMED if allowed else TYPE_DENIED,
        subject=subject,
        category=CATEGORY,
        severity=SEVERITY_ALLOWED if allowed else SEVERITY_DENIED,
        cluster=settings.CLUSTER_MANAGER,
        project=project,
        deployment=deployment,
        actor=actor.identity[:255] if actor.identity else None,
        actor_kind=actor.kind.value,
        # Truncated to the column width, like summary and dedup_key: a value that does not
        # fit makes the INSERT fail, and then the whole audit line disappears quietly.
        origin=origin[:64] if origin else None,
        summary=_summary(subject, action, allowed, cause, grant)[:255],
        flow_id=get_flow_id(),
        task_id=task_id,
        dedup_key=_dedup_key(action, subject, allowed, cause),
        data=data,
    )
    try:
        async with session_scope() as session:
            session.add(event)
    except Exception:
        # Comply-or-explain: broad, because whatever error the database or the network gives,
        # the request itself has to go on. The counter keeps it from happening quietly.
        global _write_failures
        _write_failures += 1
        # action and subject come from the caller, so sanitizing keeps them out of the log raw.
        logger.exception(
            "Audit write failed (action=%s, subject=%s)",
            sanitize_for_log(action),
            sanitize_for_log(subject),
        )


@dataclass(frozen=True, slots=True)
class AuditCursor:
    """A position in the trail: the ``(time, id)`` pair of one row."""

    time: datetime
    id: uuid.UUID


def _search_filter(term: str) -> ColumnElement[bool]:
    """Case-insensitive match of ``term`` against the fields the audit tab shows.

    ``data`` is matched per key and never as a whole: ``data::text`` carries the keys
    themselves, so searching for "action" or "allowed" would return every row. ``origin``
    is left out on purpose: it is a personal detail, for platform admins in the database.
    """
    pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    data = NotificationEvent.data
    haystack = (
        NotificationEvent.actor,
        NotificationEvent.actor_kind,
        NotificationEvent.summary,
        NotificationEvent.deployment,
        data["action"].astext,
        data["resource_path"].astext,
        data["cause"].astext,
        data["grant"].astext,
        cast(data["commit_shas"], Text),
        cast(data["fields"], Text),
    )
    return or_(*(column.ilike(pattern, escape="\\") for column in haystack))


async def list_actions(
    project: str,
    *,
    limit: int = LIST_DEFAULT_LIMIT,
    query: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before: AuditCursor | None = None,
    after: AuditCursor | None = None,
) -> list[NotificationEvent]:
    """The trail of one project, newest first.

    ``query`` searches the shown fields, ``since`` (inclusive) and ``until`` (exclusive)
    bound the period, and ``before``/``after`` page on the ``(time, id)`` pair: a plain
    ``time`` cursor drops a row whenever the page boundary falls between two rows that
    share a timestamp. ``after`` walks back towards the newest rows and is reversed before
    it is returned, so the result is always newest first.
    """
    if before is not None and after is not None:
        raise ValueError("before and after are mutually exclusive")

    def _at(cursor: AuditCursor) -> ColumnElement[tuple[Any, ...]]:
        """The cursor as a row value, with the column types so the bind is right."""
        return tuple_(literal(cursor.time, NotificationEvent.time.type), literal(cursor.id, NotificationEvent.id.type))

    row = tuple_(NotificationEvent.time, NotificationEvent.id)
    statement = select(NotificationEvent).where(
        NotificationEvent.project == project, NotificationEvent.type.in_(ACTION_TYPES)
    )
    if query:
        statement = statement.where(_search_filter(query))
    if since is not None:
        statement = statement.where(NotificationEvent.time >= since)
    if until is not None:
        statement = statement.where(NotificationEvent.time < until)
    if before is not None:
        statement = statement.where(row < _at(before))
    if after is not None:
        statement = statement.where(row > _at(after))

    if after is not None:
        statement = statement.order_by(NotificationEvent.time.asc(), NotificationEvent.id.asc())
    else:
        statement = statement.order_by(NotificationEvent.time.desc(), NotificationEvent.id.desc())

    async with session_scope() as session:
        found = list((await session.execute(statement.limit(limit))).scalars())
    return list(reversed(found)) if after is not None else found


async def run_retention(now: datetime | None = None) -> dict[str, int]:
    """One round of the retention loop: two passes, on the table's two sweep indexes.

    Scoped to :data:`ACTION_TYPES`. The audit is the only kind in this table today, but a
    later kind gets its own term, and without this limit that kind would be wiped quietly on
    the audit's term.

    Returns how many rows were pseudonymised and how many were deleted.
    """
    moment = now or datetime.now(UTC)
    async with session_scope() as session:
        # Deleting first, so a row that goes anyway is not emptied out beforehand.
        deleted = await session.execute(
            delete(NotificationEvent).where(
                NotificationEvent.time < moment - DELETE_AFTER,
                NotificationEvent.type.in_(ACTION_TYPES),
            )
        )
        # Both columns to NULL and never to a string: otherwise the predicate of
        # idx_notification_events_actor_sweep stays true and that index never shrinks.
        pseudonymized = await session.execute(
            update(NotificationEvent)
            .where(
                NotificationEvent.time < moment - PSEUDONYMIZE_AFTER,
                NotificationEvent.type.in_(ACTION_TYPES),
                (NotificationEvent.actor.is_not(None)) | (NotificationEvent.origin.is_not(None)),
            )
            .values(actor=None, origin=None)
        )
    return {"pseudonymized": pseudonymized.rowcount or 0, "deleted": deleted.rowcount or 0}


def audit_metrics() -> list[Metric]:
    """The audit write failures, for the Prometheus scrape.

    ``record_action`` raises nothing at its caller, so without this counter a broken audit
    trail would be silent. The name follows the house prefix opi_, not the zad_ from the
    design.
    """
    failures = CounterMetricFamily(
        "opi_audit_write_failures",
        "Mislukte schrijfacties naar het audittrail sinds de start",
    )
    failures.add_metric([], _write_failures)
    return [failures]


register_metric_source(audit_metrics)
