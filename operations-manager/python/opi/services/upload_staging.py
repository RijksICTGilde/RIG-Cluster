"""Temporary staging store for uploaded files.

Used by the create-wizard flow, where a file can be uploaded before the project
(and therefore its AGE key) exists. The raw bytes are parked in a tmp directory
under a random token; the file is encrypted into the project only at submit-time
(see the attachments combine generator). Entries older than the TTL are swept
opportunistically on each new upload, so no background scheduler is required.

Files here are short-lived but sensitive (they may be unencrypted private keys),
so the directory and files use restrictive permissions.
"""

import json
import logging
import os
import re
import time
import uuid

from opi.core.config import settings

logger = logging.getLogger(__name__)

STAGING_DIR = os.path.join(settings.TEMP_DIR, "upload-staging")
STAGING_TTL_SECONDS = 24 * 60 * 60
MAX_SIZE_BYTES = 256 * 1024

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


def stage_file(content: bytes, filename: str) -> str:
    """Store raw bytes under a fresh token and return that token.

    Sweeps expired entries first. Raises ValueError on empty/oversized content.
    """
    if not content:
        raise ValueError("Leeg bestand geupload")
    if len(content) > MAX_SIZE_BYTES:
        raise ValueError(f"Bestand te groot (max {MAX_SIZE_BYTES // 1024} KB)")

    sweep()
    _ensure_dir()
    token = uuid.uuid4().hex
    data_path, meta_path = _paths(token)
    with open(data_path, "wb") as handle:
        handle.write(content)
    os.chmod(data_path, 0o600)
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump({"filename": filename, "staged_at": int(time.time())}, handle)
    os.chmod(meta_path, 0o600)
    return token


def read_staged(token: str) -> tuple[bytes, str] | None:
    """Return (content_bytes, original_filename) for a token, or None if absent/invalid."""
    if not _TOKEN_RE.match(token or ""):
        return None
    data_path, meta_path = _paths(token)
    if not os.path.isfile(data_path):
        return None
    with open(data_path, "rb") as handle:
        content = handle.read()
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
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
