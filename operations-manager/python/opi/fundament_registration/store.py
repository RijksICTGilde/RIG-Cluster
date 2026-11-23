"""The two tables of the fieldlab registration, and the reads and writes on them.

The row is the truth, not the background task: a pod that restarts mid-attempt leaves a
``bezig`` row behind, and the next question sees from ``updated_at`` that nobody is
finishing it. Claiming an attempt is therefore a conditional write (``INSERT .. ON
CONFLICT DO NOTHING``, ``UPDATE .. WHERE state``), so two questions that arrive together
start one attempt.

``step`` carries what still has to be reported: the step an attempt is in while it runs,
the step that failed afterwards, and nothing once the caller has been told about it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import DateTime, Text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column

from opi.core.config import settings
from opi.core.db import Base, session_scope
from opi.fundament_registration.configuration import ATTEMPT_TIMEOUT_SECONDS
from opi.utils.age import decrypt_password_smart_auto_sync, encrypt_to_base64_age_sync


class State(StrEnum):
    """The state of a team's registration. The values are the domain's own words."""

    BUSY = "bezig"
    DONE = "klaar"
    FAILED = "mislukt"


class Step(StrEnum):
    """The steps an attempt walks through, in order."""

    DNS = "dns"
    CERTIFICATE = "certificaat"


class FundamentRegistration(Base):
    """One team: who asked for it, where it points, and how far the attempt got."""

    __tablename__ = "fundament_registrations"

    team: Mapped[str] = mapped_column(Text, primary_key=True)
    cluster_id: Mapped[str] = mapped_column(Text, nullable=False)
    ip: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    step: Mapped[str | None] = mapped_column(Text, nullable=True)
    certificate: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FundamentAcmeAccount(Base):
    """The lego account files per ACME directory, so every attempt uses one account.

    Not part of :class:`FundamentRegistration`: the account belongs to the directory and
    not to a team, and Let's Encrypt allows only ten new accounts per IP per three hours.
    """

    __tablename__ = "fundament_acme_accounts"

    directory: Mapped[str] = mapped_column(Text, primary_key=True)
    files: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@dataclass(frozen=True)
class Registration:
    """A row as a caller reads it, with the private key already opened."""

    team: str
    cluster_id: str
    ip: str
    state: State
    step: Step | None
    certificate: str | None
    private_key: str | None
    updated_at: datetime

    @property
    def is_stale(self) -> bool:
        """Whether a running attempt has been quiet for longer than the contract allows."""
        age = datetime.now(UTC) - self.updated_at
        return self.state is State.BUSY and age > timedelta(seconds=ATTEMPT_TIMEOUT_SECONDS)


async def read(team: str) -> Registration | None:
    """The row of this team, or None when the name is still free."""
    async with session_scope() as session:
        row = await session.get(FundamentRegistration, team)
        if row is None:
            return None
        return Registration(
            team=row.team,
            cluster_id=row.cluster_id,
            ip=row.ip,
            state=State(row.state),
            step=Step(row.step) if row.step else None,
            certificate=row.certificate,
            private_key=_pem(decrypt_password_smart_auto_sync(row.private_key)) if row.private_key else None,
            updated_at=row.updated_at,
        )


async def claim_first_attempt(team: str, cluster_id: str, ip: str) -> bool:
    """Bind the team to this cluster and start its first attempt. False when it already existed."""
    now = datetime.now(UTC)
    statement = (
        insert(FundamentRegistration)
        .values(
            team=team,
            cluster_id=cluster_id,
            ip=ip,
            state=State.BUSY.value,
            step=Step.DNS.value,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["team"])
    )
    async with session_scope() as session:
        return (await session.execute(statement)).rowcount == 1


async def claim_next_attempt(team: str, ip: str) -> bool:
    """Move a failed row back to a running attempt. False when another question got there first."""
    statement = (
        update(FundamentRegistration)
        .where(FundamentRegistration.team == team, FundamentRegistration.state == State.FAILED.value)
        .values(ip=ip, state=State.BUSY.value, step=Step.DNS.value, updated_at=datetime.now(UTC))
    )
    async with session_scope() as session:
        return (await session.execute(statement)).rowcount == 1


async def mark_step(team: str, step: Step) -> None:
    """Record which step the attempt is in; this is also its heartbeat."""
    await _write(team, step=step.value, updated_at=datetime.now(UTC))


async def mark_failed(team: str, step: Step) -> None:
    """Record that this step failed, so the next question can report it."""
    await _write(team, state=State.FAILED.value, step=step.value, updated_at=datetime.now(UTC))


async def mark_reported(team: str) -> None:
    """Clear the failure that has just been answered, so the next question starts over."""
    await _write(team, state=State.FAILED.value, step=None, updated_at=datetime.now(UTC))


async def mark_done(team: str, certificate: str, private_key: str) -> None:
    """Store the issued certificate. The key goes in encrypted with the platform key."""
    await _write(
        team,
        state=State.DONE.value,
        step=None,
        certificate=certificate,
        private_key=encrypt_to_base64_age_sync(private_key, settings.SOPS_AGE_PUBLIC_KEY),
        updated_at=datetime.now(UTC),
    )


async def record_address(team: str, ip: str) -> None:
    """Record the address the A record now points at."""
    await _write(team, ip=ip, updated_at=datetime.now(UTC))


async def _write(team: str, **values: object) -> None:
    statement = update(FundamentRegistration).where(FundamentRegistration.team == team).values(**values)
    async with session_scope() as session:
        await session.execute(statement)


async def read_acme_account(directory: str) -> dict[str, str]:
    """The stored lego account files for this directory, as relative path to content."""
    async with session_scope() as session:
        row = await session.get(FundamentAcmeAccount, directory)
    if row is None:
        return {}
    files = json.loads(decrypt_password_smart_auto_sync(row.files))
    return {str(path): str(content) for path, content in files.items()}


async def write_acme_account(directory: str, files: dict[str, str]) -> None:
    """Store the lego account files for this directory, encrypted with the platform key."""
    encrypted = encrypt_to_base64_age_sync(json.dumps(files), settings.SOPS_AGE_PUBLIC_KEY)
    now = datetime.now(UTC)
    statement = (
        insert(FundamentAcmeAccount)
        .values(directory=directory, files=encrypted, updated_at=now)
        .on_conflict_do_update(index_elements=["directory"], set_={"files": encrypted, "updated_at": now})
    )
    async with session_scope() as session:
        await session.execute(statement)


def _pem(text: str) -> str:
    """PEM with the trailing newline that the age helpers strip off."""
    return text if text.endswith("\n") else f"{text}\n"
