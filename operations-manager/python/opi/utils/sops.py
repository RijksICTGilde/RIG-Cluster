"""The sops CLI, and the file names a secret carries through it. AGE itself is age.py."""

import glob
import logging
import os
import subprocess
from typing import NamedTuple

from opi.utils.subprocess_env import child_env
from opi.utils.yaml_util import YAMLError, load_yaml_safe

logger = logging.getLogger(__name__)

#: The two names one secret file carries through the encryption pipeline: plain text as
#: ``.to-sops.yaml``, replaced by the encrypted ``.sops.yaml`` before anything is
#: committed. The values are fixed by the GitOps contract (features/sops-bestandsnamen.md).
SOPS_SUFFIX = ".sops.yaml"
TO_SOPS_SUFFIX = ".to-sops.yaml"


class SopsFilenames(NamedTuple):
    plaintext: str
    encrypted: str


def sops_filenames(name: str) -> SopsFilenames:
    """Both names belonging to ``name``, bare or already carrying either suffix."""
    stem = name.removesuffix(TO_SOPS_SUFFIX).removesuffix(SOPS_SUFFIX)
    return SopsFilenames(f"{stem}{TO_SOPS_SUFFIX}", f"{stem}{SOPS_SUFFIX}")


class SOPSKeyNotAvailableError(Exception):
    """Raised when SOPS AGE key is not available for encryption."""


class SOPSEncryptionError(Exception):
    """
    Raised when encrypting one or more .to-sops.yaml files fails.

    This is a security-critical failure: any remaining .to-sops.yaml file in
    the working tree holds the secret in plain text. Callers MUST treat this
    as fatal and abort before committing/pushing to git.
    """


def _sops_plaintext_unchanged(plaintext_path: str, encrypted_path: str, private_key: str) -> bool:
    """Return True when an existing SOPS file decrypts to the same content.

    SOPS encryption is non-deterministic (fresh nonces + MAC + lastmodified per
    run), so re-encrypting unchanged plaintext rewrites the whole file and churns
    git on every deployment. When the existing ``encrypted_path`` decrypts to the
    same parsed YAML as the freshly generated ``plaintext_path``, the new
    encryption can be skipped and the existing ciphertext kept verbatim.

    Compares parsed YAML (not raw bytes) so key-order or formatting differences
    never count as a change. Both sides go through the same loader: a reader that resolves
    scalars differently from the writer turns an unchanged value into a change, or the
    other way round. Returns False on any doubt (no existing file, a decrypt failure after
    a key rotation, or an unparseable side), so the caller re-encrypts.
    """
    if not os.path.exists(encrypted_path):
        return False

    decrypted = decrypt_sops_with_key(encrypted_path, private_key)
    if decrypted is None:
        return False

    try:
        with open(plaintext_path) as f:
            new_doc = load_yaml_safe(f.read())
        old_doc = load_yaml_safe(decrypted)
    except YAMLError:
        return False
    return new_doc == old_doc


def decrypt_sops_with_key(file_path: str, private_key: str) -> str | None:
    """Decrypt a SOPS file with an explicit AGE private key. None on failure."""
    process = subprocess.run(  # noqa: S603  # no shell; the variable parts are separate argv elements
        ["sops", "--decrypt", file_path],  # noqa: S607  # a tool from the image's PATH, not a fixed path
        capture_output=True,
        text=True,
        check=False,
        env=child_env({"SOPS_AGE_KEY": private_key}),
    )
    if process.returncode != 0:
        logger.info(f"Could not decrypt {os.path.basename(file_path)}: {process.stderr.strip()}")
        return None
    return process.stdout


def encrypt_to_sops_files(directory: str, public_key: str, private_key: str | None = None) -> bool:
    """Encrypt every .to-sops.yaml file in a directory with SOPS, renaming it to .sops.yaml.

    Every file is attempted even if an earlier one fails: we never early-return leaving
    not-yet-processed files in plain text. A .to-sops.yaml file is only removed once its
    encrypted .sops.yaml counterpart has been written successfully. Any remaining
    .to-sops.yaml after this call still holds the secret in plain text.

    With ``private_key`` an existing ``.sops.yaml`` is decrypted and compared first: when the
    plaintext is unchanged the existing ciphertext is kept verbatim and re-encryption is
    skipped. That avoids rewriting every secret on every deployment (SOPS is
    non-deterministic), which keeps the git diff and the number of committed files minimal.
    Without a key the file is always re-encrypted.

    Raises:
        SOPSEncryptionError: when one or more files could not be encrypted. This is
            security-critical and callers MUST abort before any git commit or push, because
            the failed files remain in plain text.
    """

    pattern = os.path.join(directory, f"*{TO_SOPS_SUFFIX}")
    to_sops_files = sorted(glob.glob(pattern))

    if not to_sops_files:
        logger.debug(f"No .to-sops.yaml files found in {directory}")
        return True

    logger.debug(f"Found {len(to_sops_files)} .to-sops.yaml files to encrypt")

    failed_files: list[str] = []
    skipped = 0

    for file_path in to_sops_files:
        base_name = os.path.basename(file_path)
        if not base_name.endswith(TO_SOPS_SUFFIX):
            continue

        output_name = sops_filenames(base_name).encrypted
        output_path = os.path.join(directory, output_name)

        # Skip re-encryption when the secret is unchanged: keep the existing
        # ciphertext, drop only the plaintext source so nothing leaks.
        if private_key and _sops_plaintext_unchanged(file_path, output_path, private_key):
            os.remove(file_path)
            skipped += 1
            logger.debug(f"Secret unchanged, kept existing ciphertext: {output_name}")
            continue

        try:
            cmd = ["sops", "--encrypt", "--age", public_key, file_path]
            logger.debug(f"Running SOPS encryption command: {' '.join(cmd)}")

            process = subprocess.run(cmd, capture_output=True, text=True, check=False, env=child_env())  # noqa: S603  # no shell; the variable parts are separate argv elements

            if process.returncode != 0:
                error_msg = process.stderr.strip()
                logger.error(f"SOPS encryption failed for {file_path}: {error_msg}")
                failed_files.append(base_name)
                # Keep going: encrypt the remaining files instead of leaving
                # them in plain text. The plaintext file is left for the caller
                # and the pre-commit guard to detect and refuse.
                continue

            # Write the encrypted content, then remove the plaintext source.
            with open(output_path, "w") as f:
                f.write(process.stdout)
            os.remove(file_path)
            logger.debug(f"Successfully encrypted {file_path} -> {output_path}")
        except FileNotFoundError:
            logger.exception("sops command not found. Please install SOPS (https://github.com/mozilla/sops)")
            # sops is missing entirely; no point retrying the other files.
            remaining = [os.path.basename(p) for p in glob.glob(pattern)]
            raise SOPSEncryptionError(
                f"SOPS-binary niet gevonden; kon {len(remaining)} bestand(en) niet versleutelen "
                f"in {directory}: {', '.join(remaining)}. "
                "Doorgaan zou secrets in platte tekst naar git committen."
            ) from None
        except OSError:
            logger.exception(f"I/O error while encrypting {file_path}")
            failed_files.append(base_name)
            continue

    if failed_files:
        raise SOPSEncryptionError(
            f"SOPS-versleuteling mislukt voor {len(failed_files)} bestand(en) in {directory}: "
            f"{', '.join(sorted(failed_files))}. "
            "Doorgaan zou secrets in platte tekst naar git committen."
        )

    if skipped:
        logger.debug(f"Kept {skipped} unchanged secret(s) as-is (no re-encryption)")

    return True


def encrypt_to_sops_files_or_fail(
    directory: str, public_key: str, context: str, private_key: str | None = None
) -> None:
    """
    Encrypt all .to-sops.yaml files in a directory and fail closed.

    This is the only entry point that secret-bearing call sites should use. It
    encrypts every .to-sops.yaml file and then verifies that none remain. On
    any failure it raises RuntimeError, so the caller aborts before committing
    plain-text secrets to git.

    Args:
        directory: Directory containing .to-sops.yaml files
        public_key: The AGE public key for encryption
        context: Human-readable description for error messages (Dutch),
            e.g. "infrastructuur-secrets voor project 'foo'"
        private_key: Optional AGE private key matching ``public_key``; when
            given, unchanged secrets keep their existing ciphertext instead of
            being re-encrypted (avoids needless git-churn).

    Raises:
        RuntimeError: If encryption fails or any .to-sops.yaml file remains.
    """
    pattern = os.path.join(directory, f"*{TO_SOPS_SUFFIX}")

    try:
        encrypt_to_sops_files(directory, public_key, private_key)
    except SOPSEncryptionError as e:
        raise RuntimeError(
            f"SOPS-versleuteling mislukt voor {context}: {e}. "
            "Dit zou secrets in platte tekst naar git committen; deployment afgebroken."
        ) from e

    remaining = sorted(os.path.basename(p) for p in glob.glob(pattern))
    if remaining:
        raise RuntimeError(
            f"Na SOPS-versleuteling van {context} bleven {len(remaining)} onversleutelde "
            f".to-sops.yaml bestand(en) over: {', '.join(remaining)}. "
            "Dit zou secrets in platte tekst naar git committen; deployment afgebroken."
        )
