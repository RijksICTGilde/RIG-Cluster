"""Round-trip tests for attachment file encryption helpers in opi.utils.age."""

import shutil
import subprocess

import pytest
from opi.utils.age import decrypt_age_block_to_bytes_sync, encrypt_file_to_age_block_sync

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age/age-keygen binary not available",
)


@pytest.fixture
def age_keypair() -> tuple[str, str]:
    """Generate a throwaway age keypair, returning (public_key, private_key)."""
    result = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines() + result.stderr.splitlines()
    private_key = next(line for line in lines if line.startswith("AGE-SECRET-KEY"))
    public_key = next(line.split(": ", 1)[1].strip() for line in lines if "public key:" in line.lower())
    return public_key, private_key


def test_round_trip_binary(age_keypair: tuple[str, str]) -> None:
    public_key, private_key = age_keypair
    # Arbitrary binary content including null bytes and the full byte range (non-UTF-8).
    payload = bytes(range(256)) * 100
    block = encrypt_file_to_age_block_sync(payload, public_key)
    assert block.startswith("-----BEGIN AGE ENCRYPTED FILE-----")
    assert decrypt_age_block_to_bytes_sync(block, private_key) == payload


def test_round_trip_text(age_keypair: tuple[str, str]) -> None:
    public_key, private_key = age_keypair
    payload = b"-----BEGIN CERTIFICATE-----\nMIIBexample\n-----END CERTIFICATE-----\n"
    block = encrypt_file_to_age_block_sync(payload, public_key)
    assert decrypt_age_block_to_bytes_sync(block, private_key) == payload


def test_empty_bytes_rejected(age_keypair: tuple[str, str]) -> None:
    # base64 of b"" is "" which the underlying age helper rejects as empty content.
    public_key, _ = age_keypair
    with pytest.raises(ValueError, match="plain content"):
        encrypt_file_to_age_block_sync(b"", public_key)
