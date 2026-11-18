"""What one registration question leads to, and the attempt that runs behind it.

The decision per question is the table in ``features/fundament-registratie.md``: an
unknown team starts an attempt, a running one is told to come back, a finished one gets
its certificate, and a failure is reported once and then starts over.

The attempt itself runs as a background task in this process, because a DNS-01 issuance
takes longer than a request may. The row in the database is the truth about it: a pod that
restarts mid-attempt leaves a row that the next question reads as failed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

from opi.connectors.transip import TransIPAuthError, TransIPConnector, TransIPError
from opi.core.logging_redact import redact_text
from opi.fundament_registration import store
from opi.fundament_registration.certificate import CertificateError, obtain_certificate
from opi.fundament_registration.configuration import RECORD_TTL, FundamentConfiguration
from opi.fundament_registration.store import Registration, State, Step

logger = logging.getLogger(__name__)

RECORD_TYPE = "A"

#: Why an attempt failed, in the words the installer shows its user.
_FAILURE_TEXT: dict[Step, str] = {
    Step.DNS: "Het A-record kon niet bij TransIP gezet worden",
    Step.CERTIFICATE: "Het certificaat kon niet bij Let's Encrypt opgehaald worden",
}

#: The tasks of the running attempts, held so the loop does not collect them halfway.
_attempts: set[asyncio.Task[None]] = set()


class Outcome(StrEnum):
    """What the caller is told. The router turns this into a status code."""

    RUNNING = "running"
    READY = "ready"
    CLAIMED = "claimed"
    FAILED = "failed"


@dataclass(frozen=True)
class Answer:
    """The outcome of one question, with what belongs to it."""

    outcome: Outcome
    registration: Registration | None = None
    reason: str = ""


async def handle(team: str, cluster_id: str, ip: str, config: FundamentConfiguration) -> Answer:
    """Decide what this question leads to, and start an attempt when it is this one's turn."""
    existing = await store.read(team)
    if existing is None:
        return await _start_first_attempt(team, cluster_id, ip, config)
    if existing.cluster_id != cluster_id:
        logger.info("fundament: team %s is al van een ander cluster", team)
        return Answer(Outcome.CLAIMED)
    if existing.state is State.DONE:
        return await _answer_ready(existing, ip, config)
    if existing.state is State.BUSY and not existing.is_stale:
        return Answer(Outcome.RUNNING)
    if existing.step is not None:
        return await _report_failure(existing.team, existing.step)
    return await _start_next_attempt(team, ip, config)


async def _start_first_attempt(team: str, cluster_id: str, ip: str, config: FundamentConfiguration) -> Answer:
    """Bind the free team name to this cluster and start its attempt."""
    if not await store.claim_first_attempt(team, cluster_id, ip):
        # Another question for the same team got here first; it owns the attempt.
        return Answer(Outcome.RUNNING)
    logger.info("fundament: aanvraag gestart voor team %s op cluster %s (%s)", team, cluster_id, ip)
    _spawn(team, ip, config)
    return Answer(Outcome.RUNNING)


async def _start_next_attempt(team: str, ip: str, config: FundamentConfiguration) -> Answer:
    """Start a new attempt after a failure that has already been reported."""
    if not await store.claim_next_attempt(team, ip):
        return Answer(Outcome.RUNNING)
    logger.info("fundament: nieuwe aanvraag gestart voor team %s (%s)", team, ip)
    _spawn(team, ip, config)
    return Answer(Outcome.RUNNING)


async def _report_failure(team: str, step: Step) -> Answer:
    """Tell the caller which step failed, and clear it so the next question starts over."""
    await store.mark_reported(team)
    logger.info("fundament: team %s krijgt de mislukking van stap %s te horen", team, step.value)
    return Answer(Outcome.FAILED, reason=_FAILURE_TEXT[step])


async def _answer_ready(existing: Registration, ip: str, config: FundamentConfiguration) -> Answer:
    """Hand back the stored certificate, after moving the A record when the address changed."""
    if existing.ip != ip:
        try:
            await _point_at(existing.team, ip, config)
        except TransIPError as error:
            logger.warning("fundament: het A-record van %s verhuisde niet: %s", existing.team, _cause(error, config))
            return Answer(Outcome.FAILED, reason=_FAILURE_TEXT[Step.DNS])
        await store.record_address(existing.team, ip)
        logger.info("fundament: team %s wijst nu naar %s", existing.team, ip)
    return Answer(Outcome.READY, registration=existing)


def _spawn(team: str, ip: str, config: FundamentConfiguration) -> None:
    """Run the attempt next to this request, which only hands out a 202."""
    task = asyncio.create_task(run_attempt(team, ip, config))
    _attempts.add(task)
    task.add_done_callback(_attempts.discard)


async def run_attempt(team: str, ip: str, config: FundamentConfiguration) -> None:
    """The A record, then the certificate. A failure is recorded, never raised: nobody waits."""
    try:
        await _point_at(team, ip, config)
    except TransIPError as error:
        await _fail(team, Step.DNS, error, config)
        return

    await store.mark_step(team, Step.CERTIFICATE)
    try:
        issued = await obtain_certificate(wildcard_name(team, config.zone), config)
    except (CertificateError, TransIPError) as error:
        await _fail(team, Step.CERTIFICATE, error, config)
        return

    await store.mark_done(team, issued.chain, issued.private_key)
    logger.info("fundament: certificaat afgegeven voor team %s", team)


async def _fail(team: str, step: Step, error: Exception, config: FundamentConfiguration) -> None:
    logger.warning("fundament: stap %s mislukte voor team %s: %s", step.value, team, _cause(error, config))
    await store.mark_failed(team, step)


async def _point_at(team: str, ip: str, config: FundamentConfiguration) -> None:
    """Replace the wildcard A record of this team with one that points at ``ip``."""
    connector = TransIPConnector.with_access_token(config.transip_token)
    name = f"*.{team}"
    for entry in await connector.get_dns_entries(config.zone):
        if entry.get("name") == name and entry.get("type") == RECORD_TYPE:
            await connector.delete_dns_entry(
                config.zone, name, RECORD_TYPE, str(entry.get("content", "")), int(entry.get("expire", RECORD_TTL))
            )
    await connector.add_dns_entry(config.zone, name, RECORD_TYPE, ip, RECORD_TTL)


def domain_name(team: str, zone: str) -> str:
    """The domain a team gets."""
    return f"{team}.{zone}"


def wildcard_name(team: str, zone: str) -> str:
    """The name the certificate is for."""
    return f"*.{domain_name(team, zone)}"


def _cause(error: Exception, config: FundamentConfiguration) -> str:
    """What to log about a failure: the kind, plus a text with no secret left in it."""
    if isinstance(error, TransIPAuthError):
        return "TransIP weigert het token (verlopen, of deze bron staat niet op de whitelist)"
    return f"{type(error).__name__}: {_masked(str(error), config)}"


def _masked(text: str, config: FundamentConfiguration) -> str:
    """A text from outside, with this attempt's own secrets out of it.

    ``redact_text`` knows the shapes of a platform secret; these two are plain values, so
    only the attempt that carries them can recognise them.
    """
    text = redact_text(text)
    for secret in (config.transip_token, config.sso_client_secret):
        text = text.replace(secret, "[weggelaten]")
    return text
