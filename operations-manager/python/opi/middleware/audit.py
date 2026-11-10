"""Turns every authorization decision on a request into an audit row.

One row per DECISION, not per request: an upsert with ``cloneFrom`` weighs the target and
then the source, and both belong in the audit. An allowed READ action gets no row; a
refused read does.

Must stay a plain ASGI middleware and the INNERMOST one around the router
(``opi/server.py``): ``BaseHTTPMiddleware`` runs the rest of the application in its own
task, so the contextvars this depends on (the commit SHAs and the task id) would not come
back out.
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

#: The background tasks still writing. Without a reference the garbage collector may
#: collect a task that has not had its turn yet.
_writers: set[asyncio.Task[None]] = set()


def _is_auditable(recorded: RecordedDecision) -> bool:
    """Whether this decision gets a row: every refusal, and every non-read action.

    An action without a rule is a fault in the register and not a refusal of this call;
    the startup check is meant to find it. It counts as auditable here, because an action
    we cannot tell is a read is not a read.
    """
    if not recorded.decision.allowed:
        return True
    try:
        return not rule_for(recorded.action).read
    except RuleError:
        return True


def _auditable_decisions(request: Request) -> list[RecordedDecision]:
    recorded: list[RecordedDecision] = getattr(request.state, "authz_decisions", [])
    return [decision_record for decision_record in recorded if _is_auditable(decision_record)]


class AuditMiddleware:
    """Writes one audit row per decision of this request, after the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Opened here so that a writer outside a request (the nightly tuner) leaves
        # nothing behind in a context nobody reads.
        begin_trail()
        status: int | None = None

        async def intercept(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, intercept)
        finally:
            # Also after an exception: the decision is already made by then, and the
            # refusal or the write that preceded it belongs in the audit.
            self._write(scope, status)

    def _write(self, scope: Scope, status: int | None) -> None:
        queue_audit_rows(Request(scope), status)


def queue_audit_rows(request: Request, status_code: int | None) -> None:
    """Queue the rows of this request without holding up the response.

    Also used by the path that never reaches this hook: ``opi/middleware/body_limit.py``
    refuses an image push before the tarball is in, and so sits outside the router this
    hook wraps. Every refusal needs a row (BIO2 8.15.01), but not by making the request
    wait on the INSERT: that would tie up a connection from the pod's ORM pool per
    refusal.
    """
    auditable = _auditable_decisions(request)
    if not auditable:
        return

    task = asyncio.create_task(
        _write_rows(
            auditable,
            actor=get_actor(),
            status_code=status_code,
            origin=client_ip(request),
            audit_trail=trail(),
        )
    )
    _writers.add(task)
    task.add_done_callback(_writers.discard)


async def _write_rows(
    recorded: list[RecordedDecision], *, actor: Actor, status_code: int | None, origin: str, audit_trail: Trail
) -> None:
    """Write the rows of this request, each with its own database session in ``record_action``."""
    for decision_record in recorded:
        resource = decision_record.resource
        decision = decision_record.decision
        await record_action(
            actor=actor,
            action=str(decision_record.action),
            resource_path=resource.path,
            project=resource.project,
            deployment=resource.deployment,
            environment=resource.environment,
            allowed=decision.allowed,
            cause=decision.cause.value if decision.cause else None,
            grant=str(decision.grant) if decision.grant else None,
            step_up=decision_record.step_up,
            status_code=status_code,
            commit_shas=audit_trail.commit_shas,
            task_id=audit_trail.task_id,
            origin=origin,
            fields=audit_trail.fields or None,
        )
