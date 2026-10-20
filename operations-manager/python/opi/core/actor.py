"""De handelende partij van de huidige uitvoering.

SERVER-SIDE bepaald op de ingang die legitimeert, nooit uit request-body gelezen.
"""

import contextvars
from dataclasses import dataclass
from enum import StrEnum


class ActorKind(StrEnum):
    """Wat voor partij er handelde, los van wie het was."""

    USER = "user"
    PROJECT_API_KEY = "project-api-key"
    PLATFORM_API_KEY = "platform-api-key"
    ADMIN_API_KEY = "admin-api-key"
    SCHEDULER = "scheduler"
    UNKNOWN = "onbekend"


@dataclass(frozen=True, slots=True)
class Actor:
    """Soort en identiteit apart, zodat een lezer geen tekst hoeft te ontleden.

    ``str()`` geeft de vorm voor de git-trailer en voor ``async_tasks.created_by``.
    """

    kind: ActorKind
    identity: str | None = None

    def __str__(self) -> str:
        # Een e-mailadres identificeert zichzelf al, en een opgeslagen waarde zonder
        # soort heeft niets om voor te zetten. Die twee blijven kaal, zodat de waarde
        # heen en terug door created_by komt, waar hij met een e-mailadres wordt
        # vergeleken (_caller_started_this_task).
        if self.kind in (ActorKind.USER, ActorKind.UNKNOWN):
            return self.identity or self.kind.value
        return f"{self.kind.value}:{self.identity}" if self.identity else self.kind.value


ACTOR_UNKNOWN = Actor(ActorKind.UNKNOWN)
"""Geen ingang heeft een actor gezet. Expliciet, zodat een gat zichtbaar is."""

ACTOR_PLATFORM_API_KEY = Actor(ActorKind.PLATFORM_API_KEY)
"""Platforminstantie of operator, geen persoon."""

ACTOR_ADMIN_API_KEY = Actor(ActorKind.ADMIN_API_KEY)
"""Voor onderhoudsoperaties."""

actor: contextvars.ContextVar[Actor] = contextvars.ContextVar("actor", default=ACTOR_UNKNOWN)


def set_actor(value: Actor) -> None:
    """Leg vast wie deze uitvoering startte."""
    actor.set(value)


def get_actor() -> Actor:
    """De handelende partij, of ``ACTOR_UNKNOWN`` als geen ingang hem zette."""
    return actor.get()


def user_actor(email: str) -> Actor:
    """Een geverifieerd e-mailadres uit een sessie of een bearer-token."""
    return Actor(ActorKind.USER, email)


def project_api_key_actor(project_name: str) -> Actor:
    """Een projectsleutel legitimeert een project, geen persoon."""
    return Actor(ActorKind.PROJECT_API_KEY, project_name)


def scheduler_actor(name: str) -> Actor:
    """Een achtergrondproces dat zonder aanroeper draait."""
    return Actor(ActorKind.SCHEDULER, name)


def actor_from_stored(value: str | None) -> Actor:
    """Een opgeslagen ``created_by`` terug naar soort en identiteit.

    Niet elke producent van dat veld schrijft deze vorm ("API", "backup-scheduler");
    zo'n waarde houdt zijn tekst als identiteit en krijgt geen soort toegedicht.
    """
    if not value:
        return ACTOR_UNKNOWN

    prefix, _, identity = value.partition(":")
    try:
        kind = ActorKind(prefix)
    except ValueError:
        return Actor(ActorKind.USER, value) if "@" in value else Actor(ActorKind.UNKNOWN, value)
    return Actor(kind, identity or None)
