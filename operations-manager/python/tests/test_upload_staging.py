"""Tests for the temporary upload staging store."""

import os
import time

import pytest
from opi.services import upload_staging
from opi.services.catalog.attachments.catalog_model import MAX_ATTACHMENT_BYTES
from opi.utils.age import AgeDecryptionError, generate_age_key_pair

PRIVATE_KEY, PUBLIC_KEY = generate_age_key_pair()


def test_stage_read_delete_round_trip() -> None:
    payload = bytes(range(256)) * 10  # binary, includes null bytes
    token = upload_staging.stage_file(payload, "keystore.p12", PUBLIC_KEY)
    assert len(token) == 32

    result = upload_staging.read_staged(token, PRIVATE_KEY)
    assert result is not None
    content, filename = result
    assert content == payload
    assert filename == "keystore.p12"

    upload_staging.delete_staged(token)
    assert upload_staging.read_staged(token, PRIVATE_KEY) is None


def test_nothing_plain_is_written_to_disk() -> None:
    """What is staged may be an unencrypted private key, so the file may not hold it."""
    payload = b"-----BEGIN PRIVATE KEY-----\nMIIBVQIBADAN\n-----END PRIVATE KEY-----\n"
    token = upload_staging.stage_file(payload, "server.key", PUBLIC_KEY)
    data_path, _ = upload_staging._paths(token)
    with open(data_path, "rb") as handle:
        on_disk = handle.read()

    assert payload not in on_disk
    assert on_disk.startswith(b"-----BEGIN AGE ENCRYPTED FILE-----")
    upload_staging.delete_staged(token)


def test_another_key_does_not_open_a_staged_file() -> None:
    other_private, _ = generate_age_key_pair()
    token = upload_staging.stage_file(b"geheim", "x.txt", PUBLIC_KEY)
    with pytest.raises(AgeDecryptionError):
        upload_staging.read_staged(token, other_private)
    upload_staging.delete_staged(token)


def test_empty_and_oversized_rejected() -> None:
    with pytest.raises(ValueError, match="Leeg"):
        upload_staging.stage_file(b"", "x", PUBLIC_KEY)
    with pytest.raises(ValueError, match="te groot"):
        upload_staging.stage_file(b"x" * (MAX_ATTACHMENT_BYTES + 1), "x", PUBLIC_KEY)


def test_without_a_public_key_nothing_is_staged() -> None:
    with pytest.raises(ValueError, match="public age key"):
        upload_staging.stage_file(b"geheim", "x.txt", None)


def test_invalid_token_rejected() -> None:
    assert upload_staging.read_staged("../etc/passwd", PRIVATE_KEY) is None
    with pytest.raises(ValueError, match="Invalid staging token"):
        upload_staging._paths("../evil")


def test_sweep_removes_expired() -> None:
    token = upload_staging.stage_file(b"old", "old.txt", PUBLIC_KEY)
    data_path, _ = upload_staging._paths(token)
    # Backdate the file beyond the TTL
    old = time.time() - (upload_staging.STAGING_TTL_SECONDS + 60)
    os.utime(data_path, (old, old))
    removed = upload_staging.sweep()
    assert removed >= 1
    assert upload_staging.read_staged(token, PRIVATE_KEY) is None
