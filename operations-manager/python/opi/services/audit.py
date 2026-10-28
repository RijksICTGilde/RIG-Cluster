"""Het audittrail: wie deed wat, waarop, en of het mocht.

Eén rij per toegestane schrijfactie en per weigering, in de gebeurtenissentabel
``notification_events`` (datamodel: ``features/futures/gebeurtenissen-vastleggen-en-melden.md``
punt 7). Deze module is de enige schrijver van die tabel en de enige die hem bijwerkt, en
dat laatste alleen via de bewaarlus hieronder.

De aanroeper van ``record_action`` is de hook rond de rechtenbeslissing; ``list_actions``
leest het trail van één project terug. De bewaartermijn staat in punt 6 van dat document:
na 90 dagen gaan ``actor`` en ``origin`` op NULL, na een jaar gaat de rij weg.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select, update

from opi.core.config import settings
from opi.core.db import session_scope
from opi.core.flow_id import get_flow_id
from opi.services.persistence.notification_events import NotificationEvent

if TYPE_CHECKING:
    import uuid

    from opi.core.actor import Actor

logger = logging.getLogger(__name__)

#: De twee soorten die deze module schrijft. Gesloten verzameling, zoals ``TaskType``.
TYPE_PERFORMED = "nl.rig.zad.action.performed.v1"
TYPE_DENIED = "nl.rig.zad.action.denied.v1"
ACTION_TYPES = (TYPE_PERFORMED, TYPE_DENIED)

#: Categorie 12 uit ``features/futures/gebeurtenissen-inventarisatie.md``.
CATEGORY = "beheer-en-beveiliging"

SEVERITY_ALLOWED = "informational"
SEVERITY_DENIED = "actionable"

#: Zolang er geen OIN is toegekend draagt ``source`` deze placeholder en is de rij
#: CloudEvents-vormig maar geen geldig CloudEvent. Een exporteur hoort hem te weigeren.
OIN_PLACEHOLDER = "oin-nog-niet-toegekend"

#: De twee klokken op dezelfde rij, uit punt 6.
PSEUDONYMIZE_AFTER = timedelta(days=90)
DELETE_AFTER = timedelta(days=365)

_write_failures = 0


def write_failures() -> int:
    """Het aantal mislukte auditschrijfacties sinds de start; ``opi/core/metrics.py`` leest dit."""
    return _write_failures


def event_source() -> str:
    """De ``source`` van een gebeurtenis van deze instantie."""
    return f"urn:nld:oin:{settings.EVENT_SOURCE_OIN}:systeem:zad-{settings.CLUSTER_MANAGER}"


def _subject(project: str | None, deployment: str | None) -> str | None:
    """``<project>`` of ``<project>/<deployment>``, nooit een e-mailadres (punt 7, regel 2)."""
    if project is None:
        return None
    return f"{project}/{deployment}" if deployment else project


def _summary(subject: str | None, action: str, allowed: bool, cause: str | None, grant: str | None) -> str:
    """Eén Nederlandse regel, zonder actor: ook dit veld mag geen persoonsgegeven dragen."""
    waarop = subject or "platform"
    if allowed:
        # De rol uit de grant, niet de grant zelf: die draagt een ``@`` en dat is precies
        # het teken waarop de invariant van punt 7 toetst.
        rol = grant.split("@", 1)[0] if grant else None
        return f"{waarop}: {action} door {rol}" if rol else f"{waarop}: {action}"
    return f"{waarop}: {action} geweigerd ({cause})" if cause else f"{waarop}: {action} geweigerd"


def _dedup_key(action: str, subject: str | None, allowed: bool, cause: str | None) -> str:
    """De sleutel waarop de meldkant later groepeert: de soort, niet het geval.

    Comply-or-explain: niet via ``log_watcher.signature()``, dat een vrije logregel
    normaliseert door onder meer gekoppelde identifiers weg te strippen. Deze velden zijn
    al genormaliseerd, en die functie zou er ``X`` van maken.
    """
    uitkomst = "allowed" if allowed else (cause or "denied")
    return f"audit:{action}:{subject or '-'}:{uitkomst}"[:160]


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
    """Leg één beslissing vast. Gooit nooit: een kapotte audit houdt geen uitrol tegen."""
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
        # Afgekapt op de kolombreedte, net als summary en dedup_key: een waarde die niet
        # past laat de INSERT falen, en dan verdwijnt de hele auditregel stil.
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
        # Comply-or-explain: breed, want welke fout de database of het netwerk ook geeft,
        # het verzoek zelf moet doorgaan. De teller maakt dat het niet stil gebeurt.
        global _write_failures
        _write_failures += 1
        # Lokale import: services mogen opi.api.router niet op moduleniveau binnenhalen.
        # Gefilterd: action en subject komen van de aanroeper, dus saniteren voorkomt dubbele logregels.
        from opi.api.router import sanitize_for_log

        logger.exception(
            "Audit write failed (action=%s, subject=%s)",
            sanitize_for_log(action),
            sanitize_for_log(subject),
        )


async def list_actions(
    project: str,
    *,
    limit: int = 50,
    before: datetime | None = None,
) -> list[NotificationEvent]:
    """Het audittrail van één project, nieuwste eerst. ``before`` pagineert op ``time``."""
    query = (
        select(NotificationEvent)
        .where(NotificationEvent.project == project, NotificationEvent.type.in_(ACTION_TYPES))
        .order_by(NotificationEvent.time.desc(), NotificationEvent.id.desc())
        .limit(limit)
    )
    if before is not None:
        query = query.where(NotificationEvent.time < before)
    async with session_scope() as session:
        return list((await session.execute(query)).scalars())


async def run_retention(now: datetime | None = None) -> dict[str, int]:
    """Eén ronde van de bewaarlus: twee passes, op de twee indexen uit punt 7.

    Geeft terug hoeveel rijen zijn gepseudonimiseerd en hoeveel er zijn verwijderd.
    """
    moment = now or datetime.now(UTC)
    async with session_scope() as session:
        # Opruimen eerst, zodat een rij die toch weggaat niet eerst nog wordt leeggemaakt.
        deleted = await session.execute(delete(NotificationEvent).where(NotificationEvent.time < moment - DELETE_AFTER))
        # Allebei de kolommen op NULL, nooit op een tekst: anders blijft het predicaat van
        # idx_notification_events_actor_sweep waar en krimpt die index nooit (punt 6).
        pseudonymized = await session.execute(
            update(NotificationEvent)
            .where(
                NotificationEvent.time < moment - PSEUDONYMIZE_AFTER,
                (NotificationEvent.actor.is_not(None)) | (NotificationEvent.origin.is_not(None)),
            )
            .values(actor=None, origin=None)
        )
    return {"pseudonymized": pseudonymized.rowcount or 0, "deleted": deleted.rowcount or 0}
