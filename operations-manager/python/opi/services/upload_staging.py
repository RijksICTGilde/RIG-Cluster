"""Temporary staging store for uploaded files.

Used by the wizard flows, where a file is uploaded before the project file that will carry
it is saved. The upload is parked in a tmp directory under a random token and read back at
submit-time (see the attachments combine generator). Entries older than the TTL are swept
opportunistically on each new upload, so no background scheduler is required.

The bytes are AGE-encrypted here, with the public key of the wizard session's own key pair
(RC-463 gives the wizard one from its first step). What is staged may be an unencrypted
private key, and the mode bits alone leave it readable to anything that reaches the
filesystem: a backup of TEMP_DIR, a second process in the pod, a container escape. The
directory and files keep their restrictive permissions on top of that.
"""

import contextlib
import json
import logging
import os
import re
import time
import uuid

from opi.core.config import settings
from opi.services.catalog.attachments.catalog_model import MAX_ATTACHMENT_BYTES, MAX_ATTACHMENT_KB
from opi.utils.age import decrypt_age_block_to_bytes_sync, encrypt_file_to_age_block_sync

logger = logging.getLogger(__name__)

STAGING_DIR = os.path.join(settings.TEMP_DIR, "upload-staging")
STAGING_TTL_SECONDS = 24 * 60 * 60

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _ensure_dir() -> None:
    os.makedirs(STAGING_DIR, mode=0o700, exist_ok=True)


def _paths(token: str) -> tuple[str, str]:
    """Return (data_path, meta_path) for a validated token."""
    if not _TOKEN_RE.match(token):
        raise ValueError(f"Invalid staging token: {token!r}")
    base = os.path.join(STAGING_DIR, token)
    return base, base + ".meta"


def sweep(ttl_seconds: int = STAGING_TTL_SECONDS) -> int:
    """Delete staged files older than ttl_seconds. Returns the number removed."""
    if not os.path.isdir(STAGING_DIR):
        return 0
    cutoff = time.time() - ttl_seconds
    removed = 0
    for name in os.listdir(STAGING_DIR):
        if name.endswith(".meta"):
            continue
        if not _TOKEN_RE.match(name):
            continue
        path = os.path.join(STAGING_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                _remove(name)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info(f"Swept {removed} expired staged upload(s)")
    return removed


def stage_file(content: bytes, filename: str, public_key: str | None) -> str:
    """Store ``content`` encrypted to ``public_key`` under a fresh token and return that token.

    Sweeps expired entries first. The size limit is judged on the plain content, before
    encryption grows it.

    Raises:
        ValueError: empty or oversized content, or a missing/refused public key.
    """
    if not content:
        raise ValueError("Leeg bestand geupload")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"Bestand te groot (max {MAX_ATTACHMENT_KB} KB)")

    block = encrypt_file_to_age_block_sync(content, public_key)
    sweep()
    _ensure_dir()
    token = uuid.uuid4().hex
    data_path, meta_path = _paths(token)
    with open(data_path, "w", encoding="utf-8") as handle:
        handle.write(block)
    os.chmod(data_path, 0o600)
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump({"filename": filename, "staged_at": int(time.time())}, handle)
    os.chmod(meta_path, 0o600)
    return token


def read_staged(token: str, private_key: str) -> tuple[bytes, str] | None:
    """Return (content_bytes, original_filename) for a token, or None if absent/invalid.

    Raises:
        ValueError: when ``private_key`` does not open the staged block. That is a wrong
            key or a damaged file, never an ordinary outcome, so it is not swallowed into
            the None that means "nothing staged here".
    """
    if not _TOKEN_RE.match(token or ""):
        return None
    data_path, meta_path = _paths(token)
    if not os.path.isfile(data_path):
        return None
    with open(data_path, encoding="utf-8") as handle:
        content = decrypt_age_block_to_bytes_sync(handle.read(), private_key)
    filename = token
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as handle:
                filename = json.load(handle).get("filename", token)
        except OSError, ValueError:
            filename = token
    return content, filename


def delete_staged(token: str) -> None:
    """Remove a staged file and its metadata. No-op if absent/invalid."""
    if not _TOKEN_RE.match(token or ""):
        return
    _remove(token)


def _remove(token: str) -> None:
    data_path, meta_path = _paths(token)
    for path in (data_path, meta_path):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
