"""What a caller gets to see when something goes wrong.

An error has two readers with opposite needs. The administrator wants the full exception:
the IP address, the port, the path, the stack trace. The user may precisely not see that,
it is the network layout of the platform, and it is of no use to them either. What they
need is whether to wait, to correct something, or to call someone.

This file serves both, with a reference in between: the message on the screen names the
reference from :mod:`opi.core.flow_id`, and that same reference sits on every log line of
the same request. That is how an administrator greps the full error back from a message
that gives nothing away.

The pages are deliberately standalone: they also have to render when the theme, the
template directory or the session is the problem itself.
"""

from __future__ import annotations

import html
from enum import StrEnum
from http import HTTPStatus
from typing import TYPE_CHECKING

from opi.core.flow_id import get_flow_id

if TYPE_CHECKING:
    import logging

    from starlette.requests import Request


class ErrorCategory(StrEnum):
    """Programmatic categorization of a cluster error. Use ``message`` for the raw text.

    Categories are intentionally broader than literal Kubernetes reasons (e.g.
    ``ImagePull`` covers ``ImagePullBackOff``, ``ErrImagePull``, manifest-unknown
    pulls, etc.) so app-level categories can be added later without breaking
    consumers tied to specific K8s state names.
    """

    ImagePull = "ImagePull"
    CrashLoop = "CrashLoop"
    # The destination the caller named could not be used: it did not resolve, refused
    # the connection, or rejected the credentials. Not a cluster state at all -- it is
    # what makes "your input" separable from "our platform" on a failed restore, so a
    # pipeline can stop retrying a typo (RC-82). Only ever set when the caller supplied
    # the destination; a restore into the project's own service can never be this.
    InvalidTarget = "InvalidTarget"
    # What the caller sent cannot be acted on: a name that does not exist, a service that
    # needs a project-level decision first, a value the schema rejects. Retrying it changes
    # nothing, and that is the point of separating it out (gemeld door zad-cli, punt 26):
    # without a category a client must treat every failure as unattributable, so a plain
    # input mistake came out of the CLI as "could not be attributed" instead of "your call
    # was wrong". Distinct from ``InvalidTarget``, which stays what it was: the restore
    # destination the caller supplied.
    InvalidInput = "InvalidInput"
    # The platform itself stumbled: a database that does not answer, a connector that breaks
    # off, a template that does not render. Deliberately apart from ``Unknown``, which says
    # "cannot be attributed" where this one is attributed, to us. For the caller that is the
    # difference between "correct your request" and "wait a moment and try again".
    InternalError = "InternalError"
    OutOfMemory = "OutOfMemory"
    HealthCheck = "HealthCheck"
    SyncFailed = "SyncFailed"
    ComparisonError = "ComparisonError"
    # ArgoCD leaves a resource out of the rollout because its kind is not on the cluster's
    # allowlist (``spec.resourceInclusions``). The rest of the application does roll out.
    ExcludedResource = "ExcludedResource"
    Unknown = "Unknown"


#: What a 5xx says when the place that raised it had nothing of its own to report. Not
#: "something went wrong" and nothing else: the sentence has to say what you can do about it.
GENERIC_ERROR_TEXT = "Er ging iets mis bij het verwerken van je verzoek. Probeer het over een minuut opnieuw."

_PAGE = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titel} - ZAD</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; display: grid; place-items: center;
         min-height: 100vh; color: #154273; background: #fff; }}
  main {{ text-align: center; padding: 2rem; max-width: 34rem; }}
  h1 {{ font-size: 2rem; margin: 0 0 .5rem; }}
  p {{ color: #4a4a4a; margin: 0 0 1.5rem; }}
  code {{ font-family: ui-monospace, monospace; background: #f3f3f3; border-radius: 3px;
         padding: .1em .3em; }}
  a {{ color: #154273; }}
</style>
</head>
<body>
<main>
  <h1>{kop}</h1>
  <p>{tekst}</p>{kenmerkregel}
  <a href="/dashboard">Naar het dashboard</a>
</main>
</body>
</html>
"""

#: Heading and fallback text per 4xx, for when the raiser said nothing of its own.
_CLIENT_ERRORS: dict[int, tuple[str, str]] = {
    401: ("Je bent niet ingelogd", "Log opnieuw in en probeer het nog eens."),
    403: ("Geen toegang", "Je hebt geen rechten voor deze pagina."),
    404: ("Deze pagina bestaat niet", "De link klopt niet meer, of de pagina is verplaatst."),
}
_CLIENT_ERROR_DEFAULT = ("Dit verzoek kan niet worden uitgevoerd", "Controleer de link of ga terug naar het dashboard.")
_ACCESS_HINT = "Hoor je wel toegang te hebben, vraag het dan aan een beheerder."


def status_phrase(status_code: int) -> str:
    """The HTTP phrase for a status, or the code itself when it is unknown.

    ``HTTPStatus(599)`` raises a ValueError, and an exception in the error handling is the
    one thing that may never happen here.
    """
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return str(status_code)


def current_reference() -> str:
    """The reference of the request running now, or an empty string.

    For a place that holds no ``request`` (a helper that fills a fragment) but does run
    inside the same request.
    """
    current = get_flow_id()
    return "" if current == "-" else current


def reference_for(request: Request) -> str:
    """The reference of this request, or an empty string when there is none.

    Out of the scope first, then out of the contextvar. The handler for an uncaught
    exception runs in ``ServerErrorMiddleware``, outside the middleware that sets the
    contextvar; the scope does travel out with it.
    """
    from_scope = request.scope.get("state", {}).get("flow_id")
    if isinstance(from_scope, str) and from_scope not in ("", "-"):
        return from_scope
    return current_reference()


def server_error_page(text: str, reference: str) -> str:
    """The 5xx page: what went wrong, what you can do about it, and the reference."""
    reference_line = ""
    if reference:
        reference_line = (
            f"\n  <p>Blijft het misgaan, meld dan dit kenmerk:<br><code>{html.escape(reference)}</code></p>"
        )
    return _PAGE.format(
        titel="Er ging iets mis",
        kop="Er ging iets mis",
        tekst=html.escape(text),
        kenmerkregel=reference_line,
    )


def client_error_page(status_code: int, detail: object) -> str:
    """The 4xx page: what was refused, in the raiser's own words when it gave any.

    A 4xx detail is written for the caller (the API returns it verbatim), so it is safe
    to show; FastAPI's bare phrase ("Not Found") is not, it tells a reader nothing.
    """
    heading, fallback = _CLIENT_ERRORS.get(status_code, _CLIENT_ERROR_DEFAULT)
    own_words = isinstance(detail, str) and detail.strip() and detail != status_phrase(status_code)
    text = str(detail) if own_words else fallback
    if status_code == 403:
        text = f"{text} {_ACCESS_HINT}"
    return _PAGE.format(titel=heading, kop=heading, tekst=html.escape(text), kenmerkregel="")


def with_reference(text: str, reference: str) -> str:
    """The message plus the sentence a user can pass on to an administrator.

    For the JSON envelope as well: a client that prints only ``detail`` then holds
    something the error can be found back with.
    """
    if not reference:
        return text
    return f"{text} Blijft het misgaan, meld dan kenmerk {reference}."


def log_render_failure(log: logging.Logger, what: str, exc: BaseException) -> None:
    """Put a failed render in the log in full, with line number and source line.

    Not in the answer to the user: those two give away the template path. The reference on
    this line is the same as the one on the screen.
    """
    log.exception("Renderen van %s mislukt: %s", what, _template_detail(exc))


def _template_detail(exc: BaseException) -> str:
    """The exception plus the line number and source line Jinja2 hands over."""
    detail = str(exc)
    lineno = getattr(exc, "lineno", None)
    if not isinstance(lineno, int):
        return detail
    detail = f"regel {lineno}: {detail}"
    source = getattr(exc, "source", None)
    if isinstance(source, str):
        lines = source.splitlines()
        if 0 <= lineno - 1 < len(lines):
            detail = f"{detail}\nbron: {lines[lineno - 1].strip()}"
    return detail
