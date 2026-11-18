"""One wildcard certificate from Let's Encrypt, obtained by running lego.

The lego process sits here and not in ``opi/connectors``: everything of this fieldlab
feature stays in one directory, so removing it afterwards is removing this directory. lego
solves the DNS-01 challenge through its ``exec`` provider, which runs
:mod:`opi.fundament_registration.dns_hook`; its own TransIP provider cannot be used,
because that one wants an account name and a private key.

The working directory is temporary and holds the private key, so it lives in
``SECRET_TEMP_DIR`` (a tmpfs in the pod) and is removed whatever happens. The ACME account
does survive: it is read from and written back to the database around every run, because
Let's Encrypt allows only ten new accounts per IP per three hours.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from opi.core.config import settings
from opi.fundament_registration import store
from opi.fundament_registration.configuration import hook_environment

if TYPE_CHECKING:
    from opi.fundament_registration.configuration import FundamentConfiguration

logger = logging.getLogger(__name__)

#: Ceiling on the whole lego run. Shorter than ``ATTEMPT_TIMEOUT_SECONDS``, so a hanging
#: process ends as a failure instead of as an attempt nobody finishes.
LEGO_TIMEOUT_SECONDS = 480.0

#: How long lego waits for the TXT record to become visible, and how often it looks.
PROPAGATION_TIMEOUT_SECONDS = 300
POLLING_INTERVAL_SECONDS = 10

#: The module lego's hook script starts.
DNS_HOOK_MODULE = "opi.fundament_registration.dns_hook"

ACCOUNTS_DIRECTORY = "accounts"
CERTIFICATES_DIRECTORY = "certificates"
HOOK_NAME = "dns-hook.sh"


class CertificateError(Exception):
    """lego did not deliver a certificate."""


@dataclass(frozen=True)
class IssuedCertificate:
    """What one run leaves behind: the chain in PEM, and the key that belongs to it."""

    chain: str
    private_key: str


async def obtain_certificate(domain: str, config: FundamentConfiguration) -> IssuedCertificate:
    """Obtain the certificate for ``domain`` (a wildcard name) through DNS-01.

    Raises:
        CertificateError: when lego fails, times out, or leaves no certificate behind.
    """
    with tempfile.TemporaryDirectory(dir=settings.SECRET_TEMP_DIR, prefix="fundament-lego-") as name:
        path = Path(name)
        _write_hook(path)
        _restore_account_files(path, await store.read_acme_account(config.acme_directory))
        try:
            await _run_lego(path, domain, config)
        finally:
            # Also after a failure: the account key is what the limit counts, and a run that
            # fails later on has already created it.
            await store.write_acme_account(config.acme_directory, _collect_account_files(path))
        return _read_issued(path, domain)


def _write_hook(path: Path) -> None:
    """The script lego calls. It only forwards, so the token travels in the environment."""
    hook = path / HOOK_NAME
    hook.write_text(f'#!/bin/sh\nexec {sys.executable} -m {DNS_HOOK_MODULE} "$@"\n')
    hook.chmod(0o700)


async def _run_lego(path: Path, domain: str, config: FundamentConfiguration) -> None:
    arguments = [
        "run",
        "--accept-tos",
        "--email",
        config.acme_email,
        "--server",
        config.acme_directory,
        "--path",
        str(path),
        "--key-type",
        "ec256",
        "--dns",
        "exec",
        "--domains",
        domain,
    ]
    process = await asyncio.create_subprocess_exec(
        "lego",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_environment(path, config),
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=LEGO_TIMEOUT_SECONDS)
    except TimeoutError as error:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise CertificateError(f"lego gaf binnen {LEGO_TIMEOUT_SECONDS:.0f} seconden geen antwoord") from error
    if process.returncode != 0:
        logger.warning("lego failed for %s with exit code %s", domain, process.returncode)
        raise CertificateError(_last_line(stderr.decode(errors="replace")))


def _environment(path: Path, config: FundamentConfiguration) -> dict[str, str]:
    """The environment of the lego process: what it needs, and nothing else.

    The TransIP token goes in here and never into an argument, because an argument is
    readable in ``ps``. The hook inherits this environment, which is how it gets the token,
    and PYTHONPATH makes it independent of the directory lego happens to run in. What the
    hook itself reads back comes from :func:`hook_environment`, so this cannot hand it less
    than it asks for.
    """
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "EXEC_PATH": str(path / HOOK_NAME),
        "EXEC_PROPAGATION_TIMEOUT": str(PROPAGATION_TIMEOUT_SECONDS),
        "EXEC_POLLING_INTERVAL": str(POLLING_INTERVAL_SECONDS),
        **hook_environment(config),
    }


def _last_line(output: str) -> str:
    """The last line lego logged, which names what went wrong, or a fixed sentence."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else "lego gaf geen reden"


def _collect_account_files(path: Path) -> dict[str, str]:
    """The ACME account tree as relative path to content, so it can be stored as one value."""
    accounts = path / ACCOUNTS_DIRECTORY
    if not accounts.is_dir():
        return {}
    return {str(file.relative_to(accounts)): file.read_text() for file in sorted(accounts.rglob("*")) if file.is_file()}


def _restore_account_files(path: Path, files: dict[str, str]) -> None:
    """Put a stored ACME account back, so lego recognises its own account."""
    for relative, content in files.items():
        target = path / ACCOUNTS_DIRECTORY / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(0o600)


def _read_issued(path: Path, domain: str) -> IssuedCertificate:
    """The chain and the key lego wrote, under the name it sanitises a wildcard to."""
    directory = path / CERTIFICATES_DIRECTORY
    chain = directory / f"{_sanitized(domain)}.crt"
    key = directory / f"{_sanitized(domain)}.key"
    if not chain.is_file() or not key.is_file():
        raise CertificateError("lego liet geen certificaat achter")
    return IssuedCertificate(chain=chain.read_text(), private_key=key.read_text())


def _sanitized(domain: str) -> str:
    """The file name lego gives a domain: see ``SanitizedName`` in its storage."""
    return domain.replace(":", "-").replace("*", "_")
