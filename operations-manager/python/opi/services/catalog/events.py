"""How a service hooks in: one decorator, two event lists (RC-39).

Before this, every extension point was a new named method on the ``Service`` base class
plus a new place that scanned the registry for services overriding it. That is two edits
outside the service for every event, and six of those methods had exactly one inhabitant
-- bespoke code on a shared base class rather than a contract.

A service now declares what it listens to at the one place that also does the work::

    class SleepModeService(Service):
        @on(ActionEvent.REDEPLOY)
        async def _wake_on_rollout(self, payload: RedeployContext) -> list[str]:
            ...

``opi.services.registry`` indexes those declarations once, so "who listens to X" is one
lookup instead of a scan written per event, and adding an event is an enum member plus a
payload type -- never a method on the base class.

Two families, because hooks do two genuinely different things and one enum would hide the
difference: see ``ActionEvent`` (changes state, never commits, async) and ``UIEvent``
(returns something to show, mutates nothing, sync).

Three rules hold the whole mechanism together:

* **A handler returns a list of contributions.** Every handler, every event. The dispatch
  concatenates what a service's handlers return, so a service that carries two handlers
  for one event (a page mixin next to its own block) contributes both without the mixins
  having to cooperate through ``super()``.
* **Participation is derived, never declared twice.** The index is built from the
  decorated methods themselves, so a service cannot be listed as a listener while not
  implementing the event, nor the other way round.
* **The payload is one object per event.** A typed thing the type checker actually
  checks, rather than loose arguments (this project runs pyright with
  ``reportCallIssue``/``reportArgumentType`` off, so loose arguments are checked nowhere).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from opi.services.services_enums import ServiceEvent

#: Attribute the decorator stamps on a handler function. Read back off the class body, so
#: the declaration and the implementation are the same line of code.
EVENT_MARKER = "_zad_service_event"

F = TypeVar("F", bound=Callable[..., Any])


def on(event: ServiceEvent, *, order: int = 100) -> Callable[[F], F]:
    """Declare that this method handles ``event``.

    ``order`` decides the position of this service among the listeners of that event
    (lower first, default 100); it is per event, so a service on two events does not share
    one order. Ordering only matters where the result is displayed in sequence or where a
    handler builds on what an earlier one left behind -- state it explicitly there rather
    than depending on registry order.

    The method's name is free: it describes what the service does at that moment, which is
    the point of moving away from one fixed method name per extension point.
    """

    def decorate(func: F) -> F:
        existing = getattr(func, EVENT_MARKER, None)
        if existing is not None:
            raise TypeError(
                f"{func.__name__} already handles {existing[0]}; one method handles one event, "
                f"so the payload it takes is unambiguous."
            )
        setattr(func, EVENT_MARKER, (event, order))
        return func

    return decorate


def collect_event_handlers(cls: type) -> dict[ServiceEvent, list[tuple[str, int]]]:
    """Index the decorated handlers of ``cls``: event -> ``(method name, order)``, sorted.

    Walks the full MRO, so a handler a service inherits from a mixin (the backups block)
    counts as that service's own. A name found on a more derived class wins: overriding a
    handler by name replaces it rather than adding a second one, which is what overriding
    means everywhere else.

    The ``order`` travels with the name because an override does not have to repeat the
    decorator -- that is what "replaces it" means. Reading the order back off the bound
    method later would then hit a plain function with no marker on it, so the index keeps
    what it sorted by.
    """
    handlers: dict[ServiceEvent, list[tuple[str, int]]] = {}
    seen: set[str] = set()
    for klass in cls.__mro__:
        for name, member in vars(klass).items():
            if name in seen:
                continue
            marker = getattr(member, EVENT_MARKER, None)
            if marker is None:
                continue
            seen.add(name)
            event, order = marker
            handlers.setdefault(event, []).append((name, order))
    return {event: sorted(entries, key=lambda entry: (entry[1], entry[0])) for event, entries in handlers.items()}
