"""The acting party of the current execution.

Determined SERVER-SIDE at the entrance that legitimises, never read from a request body.
"""

import contextvars
from dataclasses import dataclass
from enum import StrEnum


class ActorKind(StrEnum):
    """What kind of party acted, apart from who it was."""

    USER = "user"
    PROJECT_API_KEY = "project-api-key"
    SERVICE_ACCOUNT = "service-account"
    PLATFORM_API_KEY = "platform-api-key"
    # No entrance sets this kind; it is only read back out of stored created_by values
    # (actor_from_stored).
    ADMIN_API_KEY = "admin-api-key"
    SCHEDULER = "scheduler"
    UNKNOWN = "onbekend"


@dataclass(frozen=True, slots=True)
class Actor:
    """``str()`` gives the shape for the git trailer and for ``async_tasks.created_by``."""

    kind: ActorKind
    identity: str | None = None

    def __str__(self) -> str:
        # An email address identifies itself, and a stored value without a kind has nothing to
        # prefix. Those two stay bare, so the value survives a round trip through created_by,
        # where it is compared against an email address (_caller_started_this_task).
        if self.kind in (ActorKind.USER, ActorKind.UNKNOWN):
            return self.identity or self.kind.value
        return f"{self.kind.value}:{self.identity}" if self.identity else self.kind.value


ACTOR_UNKNOWN = Actor(ActorKind.UNKNOWN)
"""No entrance set an actor. Explicit, so that a gap is visible."""

ACTOR_PLATFORM_API_KEY = Actor(ActorKind.PLATFORM_API_KEY)
"""A platform instance or an operator, not a person."""

actor: contextvars.ContextVar[Actor] = contextvars.ContextVar("actor", default=ACTOR_UNKNOWN)


def set_actor(value: Actor) -> None:
    """Record who started this execution."""
    actor.set(value)


def get_actor() -> Actor:
    """The acting party, or ``ACTOR_UNKNOWN`` when no entrance set one."""
    return actor.get()


def user_actor(email: str) -> Actor:
    """A verified email address out of a session or a bearer token."""
    return Actor(ActorKind.USER, email)


def project_api_key_actor(project_name: str) -> Actor:
    """A project key legitimises a project, not a person."""
    return Actor(ActorKind.PROJECT_API_KEY, project_name)


def service_account_actor(project_name: str, name: str) -> Actor:
    """A service account of a project, with a name the audit trail can mention."""
    return Actor(ActorKind.SERVICE_ACCOUNT, f"{project_name}/{name}")


def scheduler_actor(name: str) -> Actor:
    """A background process that runs without a caller."""
    return Actor(ActorKind.SCHEDULER, name)


def actor_from_stored(value: str | None) -> Actor:
    """A stored ``created_by`` back into kind and identity.

    Not every producer of that field writes this shape ("API", "backup-scheduler"); such a
    value keeps its text as identity and is not credited with a kind.
    """
    if not value:
        return ACTOR_UNKNOWN

    prefix, _, identity = value.partition(":")
    try:
        kind = ActorKind(prefix)
    except ValueError:
        return Actor(ActorKind.USER, value) if "@" in value else Actor(ActorKind.UNKNOWN, value)
    return Actor(kind, identity or None)
