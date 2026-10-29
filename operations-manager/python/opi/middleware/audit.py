"""De hook die van elke rechtenbeslissing een auditrij maakt.

Eén middleware in plaats van code per route: de poort heeft bij elke beslissing de
action, de resource, de uitkomst en de step-up op het verzoek gezet (``record_decision``,
ontwerp 8.3), en hier komen de twee dingen erbij die alleen na het verzoek bekend zijn:
de statuscode en wat het verzoek onderweg achterliet (``opi/core/audit_trail.py``).

Een rij per BESLISSING, niet per verzoek: een upsert met ``cloneFrom`` weegt eerst het
doel en daarna de bron, en beide horen in de audit. Wat niet in een rij komt is een
toegestane LEESactie; een geweigerde leesactie wel.

Waarom dit een kale ASGI-middleware is en geen ``BaseHTTPMiddleware``: die laatste draait
de rest van de applicatie in een eigen taak, en een contextvar die daarbinnen wordt gezet
komt dan niet meer terug. De commit-SHA's en de taak-id reizen juist als contextvar, dus
deze middleware hoort ook als BINNENSTE om de router te staan (``opi/server.py``), en
opent het spoor zelf.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from starlette.requests import Request

from opi.core.actor import get_actor
from opi.core.audit_trail import begin_trail, trail
from opi.core.authz.engine import RecordedDecision, RuleError, rule_for
from opi.services.audit import record_action
from opi.utils.client_ip import client_ip

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from opi.core.actor import Actor
    from opi.core.audit_trail import Trail

logger = logging.getLogger(__name__)

#: De achtergrondtaken die nog schrijven. Zonder verwijzing mag de vuilnisophaler een
#: taak die nog niet aan de beurt is geweest opruimen.
_schrijvers: set[asyncio.Task[None]] = set()


def _hoort_in_de_audit(genomen: RecordedDecision) -> bool:
    """Of deze beslissing een rij krijgt: elke weigering, en elke niet-leesactie.

    Een action zonder regel is een fout in het register en geen weigering van deze
    aanroep; de opstartcontrole hoort hem te vinden. Hier valt hij als "wel vastleggen",
    want een handeling waarvan we niet weten of ze leest is geen leesactie.
    """
    if not genomen.decision.allowed:
        return True
    try:
        return not rule_for(genomen.action).read
    except RuleError:
        return True


class AuditMiddleware:
    """Schrijft na het antwoord een auditrij per beslissing van dit verzoek."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Het spoor hoort bij DIT verzoek: hier geopend, zodat een schrijver buiten een
        # verzoek (de nachtelijke tuner) niets in een context achterlaat die niemand leest.
        begin_trail()
        status: int | None = None

        async def onderschep(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, onderschep)
        finally:
            # Ook na een uitzondering: de beslissing is dan genomen en de weigering of de
            # schrijfactie die eraan voorafging hoort in de audit, met de statuscode die
            # het antwoord had als het er al een was.
            self._schrijf(scope, status)

    def _schrijf(self, scope: Scope, status: int | None) -> None:
        """Zet de rijen van dit verzoek in de wachtrij, zonder het antwoord op te houden."""
        request = Request(scope)
        genomen: list[RecordedDecision] = getattr(request.state, "authz_decisions", [])
        te_schrijven = [beslissing for beslissing in genomen if _hoort_in_de_audit(beslissing)]
        if not te_schrijven:
            return

        taak = asyncio.create_task(
            _schrijf_rijen(
                te_schrijven,
                actor=get_actor(),
                status_code=status,
                origin=client_ip(request),
                spoor=trail(),
            )
        )
        _schrijvers.add(taak)
        taak.add_done_callback(_schrijvers.discard)


async def _schrijf_rijen(
    genomen: list[RecordedDecision], *, actor: Actor, status_code: int | None, origin: str, spoor: Trail
) -> None:
    """De rijen van dit verzoek, elk met zijn eigen databasesessie in ``record_action``."""
    for beslissing in genomen:
        resource = beslissing.resource
        decision = beslissing.decision
        await record_action(
            actor=actor,
            action=str(beslissing.action),
            resource_path=resource.path,
            project=resource.project,
            deployment=resource.deployment,
            environment=resource.environment,
            allowed=decision.allowed,
            cause=decision.cause.value if decision.cause else None,
            grant=str(decision.grant) if decision.grant else None,
            step_up=beslissing.step_up,
            status_code=status_code,
            commit_shas=spoor.commit_shas,
            task_id=spoor.task_id,
            origin=origin,
            fields=spoor.fields or None,
        )
