# TODO: check all except, and remove if None is returned, we allways raise exceptions

"""
SOPS encryption/decryption utilities.

This module handles SOPS-specific operations including key generation and encryption.
For pure Age encryption, use the age.py module.
"""

import contextlib
import glob
import logging
import os
import subprocess
import tempfile
import uuid

from opi.core.config import settings
from opi.utils.age import encrypt_age_content

logger = logging.getLogger(__name__)


class SOPSKeyEncryptionError(Exception):
    """Raised when SOPS key encryption fails."""


class SOPSKeyNotAvailableError(Exception):
    """Raised when SOPS AGE key is not available for encryption."""


class SOPSEncryptionError(Exception):
    """
    Raised when encrypting one or more .to-sops.yaml files fails.

    This is a security-critical failure: any remaining .to-sops.yaml file in
    the working tree holds the secret in plain text. Callers MUST treat this
    as fatal and abort before committing/pushing to git.
    """


def get_sops_private_key() -> str | None:
    """
    Get the SOPS private key from settings.

    Returns:
        The SOPS AGE private key, or None if not available
    """
    return settings.SOPS_AGE_PRIVATE_KEY


def decrypt_sops_file(file_path: str) -> str | None:
    """
    Decrypt a SOPS-encrypted file.

    Args:
        file_path: Path to the SOPS-encrypted file

    Returns:
        Decrypted content as string, or None if decryption failed
    """
    cmd = ["sops", "--decrypt", file_path]
    logger.debug(f"Running SOPS decryption command: {' '.join(cmd)}")

    process = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if process.returncode != 0:
        error_msg = process.stderr.strip()
        logger.error(f"SOPS decryption failed: {error_msg}")
        return None

    decrypted_content = process.stdout
    logger.debug("Successfully decrypted SOPS file")
    return decrypted_content


def encrypt_sops_file(file_path: str) -> bool:
    """
    Encrypt a file using SOPS.

    Args:
        file_path: Path to the file to encrypt

    Returns:
        True if encryption was successful, False otherwise
    """
    cmd = ["sops", "--encrypt", "--in-place", file_path]
    logger.debug(f"Running SOPS encryption command: {' '.join(cmd)}")

    process = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if process.returncode != 0:
        error_msg = process.stderr.strip()
        logger.error(f"SOPS encryption failed: {error_msg}")
        return False

    logger.debug("Successfully encrypted file with SOPS")
    return True


def encrypt_to_sops_files(directory: str, public_key: str) -> bool:
    """
    Encrypt all .to-sops.yaml files in a directory using SOPS, renaming them to .sops.yaml.

    Every file is attempted even if an earlier one fails: we never early-return
    leaving not-yet-processed files in plain text. A .to-sops.yaml file is only
    removed once its encrypted .sops.yaml counterpart has been written
    successfully. Any remaining .to-sops.yaml after this call still holds the
    secret in plain text.

    Args:
        directory: Directory containing .to-sops.yaml files
        public_key: The AGE public key for encryption

    Returns:
        True if all files were encrypted successfully

    Raises:
        SOPSEncryptionError: If one or more files could not be encrypted. This
            is security-critical and callers MUST abort before any git
            commit/push, because the failed files remain in plain text.
    """

    pattern = os.path.join(directory, "*.to-sops.yaml")
    to_sops_files = sorted(glob.glob(pattern))

    if not to_sops_files:
        logger.debug(f"No .to-sops.yaml files found in {directory}")
        return True

    logger.info(f"Found {len(to_sops_files)} .to-sops.yaml files to encrypt")

    failed_files: list[str] = []

    for file_path in to_sops_files:
        base_name = os.path.basename(file_path)
        if not base_name.endswith(".to-sops.yaml"):
            continue

        output_name = base_name[: -len(".to-sops.yaml")] + ".sops.yaml"
        output_path = os.path.join(directory, output_name)

        try:
            cmd = ["sops", "--encrypt", "--age", public_key, file_path]
            logger.debug(f"Running SOPS encryption command: {' '.join(cmd)}")

            process = subprocess.run(cmd, capture_output=True, text=True, check=False)

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
            logger.info(f"Successfully encrypted {file_path} -> {output_path}")
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

    return True


def encrypt_to_sops_files_or_fail(directory: str, public_key: str, context: str) -> None:
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

    Raises:
        RuntimeError: If encryption fails or any .to-sops.yaml file remains.
    """
    pattern = os.path.join(directory, "*.to-sops.yaml")

    try:
        encrypt_to_sops_files(directory, public_key)
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


def generate_sops_key_pair() -> tuple[str, str]:
    """
    Generate a new SOPS AGE key pair.

    Returns:
        Tuple of (private_key, public_key)

    Raises:
        SOPSKeyEncryptionError: When key generation fails
    """
    try:
        # Create a unique temporary file that doesn't exist yet
        temp_dir = tempfile.gettempdir()
        temp_file_path = os.path.join(temp_dir, f"age_key_{uuid.uuid4().hex}")

        try:
            # Generate age key pair
            result = subprocess.run(["age-keygen", "-o", temp_file_path], capture_output=True, text=True, check=True)

            # Read the generated private key
            with open(temp_file_path) as f:
                private_key_content = f.read().strip()

            # Extract public key from stderr (age-keygen outputs it there)
            public_key = None
            for line in result.stderr.splitlines():
                if line.startswith("Public key: "):
                    public_key = line.replace("Public key: ", "").strip()
                    break

            if not public_key:
                raise SOPSKeyEncryptionError("Failed to extract public key from age-keygen output")

            # Extract private key (should be the AGE-SECRET-KEY line)
            private_key = None
            for line in private_key_content.splitlines():
                if line.startswith("AGE-SECRET-KEY-"):
                    private_key = line.strip()
                    break

            if not private_key:
                raise SOPSKeyEncryptionError("Failed to extract private key from generated content")

            logger.debug("Successfully generated SOPS AGE key pair")
            return private_key, public_key

        finally:
            # Clean up temp file
            with contextlib.suppress(OSError):
                os.unlink(temp_file_path)

    except subprocess.CalledProcessError as e:
        raise SOPSKeyEncryptionError(f"age-keygen command failed: {e.stderr}") from e
    except Exception as e:
        raise SOPSKeyEncryptionError(f"Failed to generate SOPS key pair: {e}") from e


async def generate_and_encrypt_sops_key_pair() -> tuple[str, str, str]:
    """
    Generate a SOPS AGE key pair and encrypt the private key using the global AGE key.
    Returns:
        Tuple of (plain_private_key, encrypted_private_key, public_key)
    Raises:
        SOPSKeyEncryptionError: When key generation or encryption fails
    """
    try:
        private_key, public_key = generate_sops_key_pair()
        encrypted_private_key = await encrypt_age_content(private_key, settings.SOPS_AGE_PUBLIC_KEY)
        return private_key, encrypted_private_key, public_key
    except Exception as e:
        raise SOPSKeyEncryptionError(f"Failed to generate encrypted SOPS key pair: {e}") from e
