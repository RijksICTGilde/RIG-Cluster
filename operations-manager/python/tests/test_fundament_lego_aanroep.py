"""How the lego process is started, and what it is handed.

lego itself is replaced by a recorded call: what has to hold is that the token travels in
the environment and not in an argument, that the challenge hook is there and executable,
that the ACME account survives a run, and that the working directory with the private key
in it is gone afterwards.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from opi.core.config import settings
from opi.fundament_registration import certificate
from opi.fundament_registration.certificate import CertificateError, obtain_certificate
from opi.fundament_registration.configuration import FundamentConfiguration

if TYPE_CHECKING:
    from collections.abc import Iterator

TOKEN = "nep-transip-token"
DOMAIN = "*.team42.zadlab.nl"
DIRECTORY = "https://acme-staging-v02.api.letsencrypt.org/directory"

CERTIFICATE = "-----BEGIN CERTIFICATE-----\nnep\n-----END CERTIFICATE-----\n"
PRIVATE_KEY = "-----BEGIN EC PRIVATE KEY-----\nnep\n-----END EC PRIVATE KEY-----\n"

CONFIG = FundamentConfiguration(
    transip_token=TOKEN,
    zone="zadlab.nl",
    acme_directory=DIRECTORY,
    acme_email="fieldlab@voorbeeld.nl",
    sso_client_id="zad-fieldlab",
    sso_client_secret="nep-client-secret",
    sso_discovery_url="https://keycloak.voorbeeld.nl/realms/rig-platform/.well-known/openid-configuration",
)


class _FakeLego:
    """A recorded lego run that writes what the real one leaves behind."""

    def __init__(self, *, exit_code: int = 0, writes: bool = True) -> None:
        self.exit_code = exit_code
        self.writes = writes
        self.arguments: list[str] = []
        self.environment: dict[str, str] = {}
        self.restored: list[str] = []
        self.hook = ""
        self.hook_mode = 0
        self.returncode = exit_code

    async def __call__(self, program: str, *arguments: str, env: dict[str, str], **_: Any) -> _FakeLego:
        self.program = program
        self.arguments = list(arguments)
        self.environment = env
        path = Path(arguments[arguments.index("--path") + 1])
        self.restored = sorted(str(file.relative_to(path)) for file in path.rglob("*") if file.is_file())
        # Read while the run is on: the working directory is gone once it ends.
        hook = Path(env["EXEC_PATH"])
        self.hook = hook.read_text()
        self.hook_mode = hook.stat().st_mode & 0o777
        if self.writes:
            self._write_output(path)
        return self

    def _write_output(self, path: Path) -> None:
        certificates = path / "certificates"
        certificates.mkdir(parents=True, exist_ok=True)
        (certificates / "_.team42.zadlab.nl.crt").write_text(CERTIFICATE)
        (certificates / "_.team42.zadlab.nl.key").write_text(PRIVATE_KEY)
        account = path / "accounts" / "acme-staging-v02.api.letsencrypt.org" / CONFIG.acme_email
        account.mkdir(parents=True, exist_ok=True)
        (account / "account.json").write_text('{"registration": {"uri": "https://acme/acct/1"}}')
        (account / f"{CONFIG.acme_email}.key").write_text(
            "-----BEGIN PRIVATE KEY-----\nnep\n-----END PRIVATE KEY-----\n"
        )

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b"ERROR lego kon het niet\n" if self.exit_code else b""

    def kill(self) -> None:  # pragma: no cover -- only a timeout reaches this
        pass


@pytest.fixture
def work_directory(tmp_path: Path) -> Iterator[Path]:
    """The temporary directory lives under SECRET_TEMP_DIR, a tmpfs in the pod."""
    with patch.object(settings, "SECRET_TEMP_DIR", str(tmp_path)):
        yield tmp_path


@pytest.fixture
def account_storage() -> Iterator[dict[str, dict[str, str]]]:
    """The ACME account as the database holds it, without a database."""
    stored: dict[str, dict[str, str]] = {}

    async def read(directory: str) -> dict[str, str]:
        return stored.get(directory, {})

    async def write(directory: str, files: dict[str, str]) -> None:
        stored[directory] = files

    with (
        patch.object(certificate.store, "read_acme_account", AsyncMock(side_effect=read)),
        patch.object(certificate.store, "write_acme_account", AsyncMock(side_effect=write)),
    ):
        yield stored


async def _obtain(lego: _FakeLego) -> Any:
    with patch.object(asyncio, "create_subprocess_exec", lego):
        return await obtain_certificate(DOMAIN, CONFIG)


async def test_a_run_hands_back_what_lego_wrote(
    work_directory: Path, account_storage: dict[str, dict[str, str]]
) -> None:
    issued = await _obtain(_FakeLego())

    assert issued.chain == CERTIFICATE
    assert issued.private_key == PRIVATE_KEY


async def test_the_token_travels_in_the_environment_and_not_in_an_argument(
    work_directory: Path, account_storage: dict[str, dict[str, str]]
) -> None:
    """An argument is readable in ``ps`` for anyone on the node."""
    lego = _FakeLego()

    await _obtain(lego)

    assert TOKEN not in " ".join(lego.arguments)
    assert lego.environment["FUNDAMENT_REGISTRATION_TRANSIP_TOKEN"] == TOKEN


async def test_the_run_asks_for_a_wildcard_with_an_ec_key_through_the_exec_provider(
    work_directory: Path, account_storage: dict[str, dict[str, str]]
) -> None:
    lego = _FakeLego()

    await _obtain(lego)

    assert lego.program == "lego"
    assert lego.arguments[0] == "run"
    assert "--accept-tos" in lego.arguments
    assert lego.arguments[lego.arguments.index("--domains") + 1] == DOMAIN
    assert lego.arguments[lego.arguments.index("--key-type") + 1] == "ec256"
    assert lego.arguments[lego.arguments.index("--dns") + 1] == "exec"
    assert lego.arguments[lego.arguments.index("--server") + 1] == DIRECTORY


async def test_the_challenge_hook_is_executable_and_calls_the_module(
    work_directory: Path, account_storage: dict[str, dict[str, str]]
) -> None:
    lego = _FakeLego()

    await _obtain(lego)

    assert certificate.DNS_HOOK_MODULE in lego.hook
    assert lego.hook_mode == 0o700


async def test_the_propagation_timeout_and_the_poll_interval_are_set(
    work_directory: Path, account_storage: dict[str, dict[str, str]]
) -> None:
    """Without these two lego waits with its own defaults, which we have not chosen."""
    lego = _FakeLego()

    await _obtain(lego)

    assert lego.environment["EXEC_PROPAGATION_TIMEOUT"] == str(certificate.PROPAGATION_TIMEOUT_SECONDS)
    assert lego.environment["EXEC_POLLING_INTERVAL"] == str(certificate.POLLING_INTERVAL_SECONDS)


async def test_the_working_directory_is_gone_afterwards(
    work_directory: Path, account_storage: dict[str, dict[str, str]]
) -> None:
    """It holds the private key, so it may not survive the request."""
    lego = _FakeLego()

    await _obtain(lego)

    assert list(work_directory.iterdir()) == []


async def test_the_acme_account_is_kept_and_put_back(
    work_directory: Path, account_storage: dict[str, dict[str, str]]
) -> None:
    """Let's Encrypt allows ten new accounts per IP per three hours, so there is one."""
    first = _FakeLego()
    await _obtain(first)

    assert first.restored == ["dns-hook.sh"]
    assert "acme-staging-v02.api.letsencrypt.org/fieldlab@voorbeeld.nl/account.json" in account_storage[DIRECTORY]

    second = _FakeLego(writes=False)
    with pytest.raises(CertificateError):
        await _obtain(second)

    assert "accounts/acme-staging-v02.api.letsencrypt.org/fieldlab@voorbeeld.nl/account.json" in second.restored


async def test_a_failed_run_still_keeps_the_account(
    work_directory: Path, account_storage: dict[str, dict[str, str]]
) -> None:
    """The account key is what the limit counts, and a run that fails later already made it."""
    with pytest.raises(CertificateError, match="lego kon het niet"):
        await _obtain(_FakeLego(exit_code=1))

    assert account_storage[DIRECTORY]


async def test_a_run_without_a_certificate_is_a_failure(
    work_directory: Path, account_storage: dict[str, dict[str, str]]
) -> None:
    with pytest.raises(CertificateError, match="geen certificaat"):
        await _obtain(_FakeLego(writes=False))
