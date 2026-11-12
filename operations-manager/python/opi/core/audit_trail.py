"""What THIS request left behind on the way, for the audit row at the end.

The hook around the authorization decision (``opi/middleware/audit.py``) writes the row
after the answer, and at that moment does not know three things that are only known during
the request: which commits of the project file it made, which task it started, and what
fields the route itself named. They sit here as a contextvar per request, like the actor in
``opi/core/actor.py``, so the writers (the project store, the task service, the gate) need
not know the hook.

The trail is OPENED by the hook (``begin_trail``) and exists only inside a request. Outside
a request a writer records nothing: a commit out of the nightly
``ResourceTuningScheduler`` runs in a single long-lived task, and without that boundary the
contextvar would grow there endlessly without anyone ever reading it.

Every request runs in its own copy of the context, so another request does not see these
values.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from opi.core.authz.actions import audit_fields_of

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable

    from starlette.requests import Request

#: The only body that is read for the audit. A ``multipart`` body stays out of it: that is
#: the shape of an image upload, and parsing it whole to pull one field name out of it would
#: push the upload through memory for a log line.
FORM_TYPE = "application/x-www-form-urlencoded"

#: The length a field value is truncated to, as ``record_action`` does for ``origin``,
#: ``actor`` and ``summary``. ``data`` is JSONB and has no column width, so without this
#: bound a caller puts as much in it as its body is big, and that stays for a year. An image
#: with a digest fits in it comfortably.
MAX_FIELD_VALUE = 256


@dataclass(slots=True)
class Trail:
    """The trail of a request, in the shape the row carries it."""

    commit_shas: list[str] = field(default_factory=list)
    task_id: uuid.UUID | None = None
    #: ``object`` and not ``str``, because that is the shape ``record_action`` reads.
    fields: dict[str, object] = field(default_factory=dict)


_trail: contextvars.ContextVar[Trail | None] = contextvars.ContextVar("audit_trail", default=None)


def begin_trail() -> None:
    """Open the trail of this request; only the hook does this."""
    _trail.set(Trail())


def record_commit(ref: str) -> None:
    """Record a commit of a project file; a request can make more than one."""
    trail_now = _trail.get()
    if trail_now is not None:
        trail_now.commit_shas.append(ref)


def record_task(task_id: uuid.UUID) -> None:
    """Record the task this request started. The first one stays.

    The row has a column for it and not a list: it names the task the work of this request
    continued in, and that is the first one that came out of it.
    """
    trail_now = _trail.get()
    if trail_now is not None and trail_now.task_id is None:
        trail_now.task_id = task_id


async def fields_from_form(request: Request, names: tuple[str, ...]) -> dict[str, str]:
    """The named fields out of the form of this request.

    Two readers: the audit below, which reads the fields the route names with
    ``audit_fields=``, and the step-up repeat in ``opi/http/auth/auth_decorators.py``. Those
    two lists are independent: ``fields`` is there to let the dialog repeat the form and therefore also
    names a free text field, and that does not belong in a row that stays for a year.

    A field that was filled more than once (a multiple choice, such as the roles of a
    member) travels as a comma list: the dialog repeats it as a hidden field, and the route
    that reads it splits on it again.
    """
    if not names:
        return {}
    form = await request.form()
    getlist = getattr(form, "getlist", None)
    found: dict[str, str] = {}
    for name in names:
        values = getlist(name) if getlist is not None else ([form[name]] if name in form else [])
        if values:
            found[name] = ",".join(str(value) for value in values)
    return found


async def capture_fields(request: Request, endpoint: Callable[..., object]) -> None:
    """Record the fields THIS route names itself, with their submitted value.

    Only the named fields, never a whole body, and each truncated at
    :data:`MAX_FIELD_VALUE`: what goes in here stays for a year, also after the retention
    loop has emptied ``actor`` and ``origin``, so a route never names a field that can point
    at a person (``features/audit.md``).

    At the gate and not in the hook: the hook runs after the answer, and the form of this
    request can no longer be read then. And only AFTER the gate has let the caller in,
    because a caller who may do nothing should not get a body parsed.
    """
    trail_now = _trail.get()
    names = audit_fields_of(endpoint)
    if trail_now is None or not names:
        return
    found = {
        name: str(value)[:MAX_FIELD_VALUE]
        for name, value in (*request.path_params.items(), *request.query_params.items())
        if name in names
    }
    missing = tuple(name for name in names if name not in found)
    if missing and request.headers.get("content-type", "").startswith(FORM_TYPE):
        from_form = await fields_from_form(request, missing)
        found.update({name: value[:MAX_FIELD_VALUE] for name, value in from_form.items()})
    trail_now.fields = dict(found)


def trail() -> Trail:
    """The trail of this request, or an empty trail outside a request."""
    return _trail.get() or Trail()
