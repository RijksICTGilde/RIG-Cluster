"""Tests for the temporary upload staging store."""

import os
import time

import pytest
from opi.services import upload_staging


def test_stage_read_delete_round_trip() -> None:
    payload = bytes(range(256)) * 10  # binary, includes null bytes
    token = upload_staging.stage_file(payload, "keystore.p12")
    assert len(token) == 32

    result = upload_staging.read_staged(token)
    assert result is not None
    content, filename = result
    assert content == payload
    assert filename == "keystore.p12"

    upload_staging.delete_staged(token)
    assert upload_staging.read_staged(token) is None


def test_empty_and_oversized_rejected() -> None:
    with pytest.raises(ValueError, match="Leeg"):
        upload_staging.stage_file(b"", "x")
    with pytest.raises(ValueError, match="te groot"):
        upload_staging.stage_file(b"x" * (upload_staging.MAX_SIZE_BYTES + 1), "x")


def test_invalid_token_rejected() -> None:
    assert upload_staging.read_staged("../etc/passwd") is None
    with pytest.raises(ValueError, match="Invalid staging token"):
        upload_staging._paths("../evil")


def test_sweep_removes_expired() -> None:
    token = upload_staging.stage_file(b"old", "old.txt")
    data_path, _ = upload_staging._paths(token)
    # Backdate the file beyond the TTL
    old = time.time() - (upload_staging.STAGING_TTL_SECONDS + 60)
    os.utime(data_path, (old, old))
    removed = upload_staging.sweep()
    assert removed >= 1
    assert upload_staging.read_staged(token) is None
