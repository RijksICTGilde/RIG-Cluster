"""
Age encryption/decryption utilities.
"""

import asyncio
import base64
import logging
import subprocess
from typing import cast

from opi.core.config import settings

logger = logging.getLogger(__name__)


# TODO: replace this method with direct configuration value
def get_global_private_key() -> str:
    return cast(str, settings.SOPS_AGE_PRIVATE_KEY)


async def decrypt_age_content(encrypted_content: str, private_key: str) -> str:
    """
    Decrypt age-encrypted content using the provided private key.

    Args:
        encrypted_content: The age-encrypted content (including BEGIN/END markers)
        private_key: The age private key (AGE-SECRET-KEY-...)

    Returns:
        Decrypted content as string
    """
    if not encrypted_content or not private_key:
        raise ValueError("Missing encrypted content or private key for decryption")

    cmd = ["bash", "-c", f'echo "{encrypted_content}" | age -d -i <(echo "{private_key}")']

    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        error_msg = stderr.decode("utf-8").strip()
        logger.error(f"Age decryption failed: {error_msg}")
        raise Exception(f"Age decryption failed: {error_msg}")

    return stdout.decode("utf-8").strip()


async def _encrypt_with_age_and_base64encode_as_prefixed_string(client_secret: str, public_key: str | None) -> str:
    """
    Encrypt the client secret using age+base64 encoding.

    Args:
        client_secret: Plain text client secret

    Returns:
        Encrypted and base64 encoded client secret with prefix base64+age which can be used in f.e. .env files
        or other places where single line values are expected.
    """
    encrypted_content = await encrypt_age_content(client_secret, public_key)
    return f"base64+age:{base64.b64encode(encrypted_content.encode()).decode()}"


async def encrypt_age_content(plain_content: str, public_key: str | None) -> str:
    """
    Encrypt content using age encryption with the provided public key.

    Args:
        plain_content: The content to encrypt
        public_key: The age public key

    Returns:
        Encrypted content as string with AGE markers
    """

    if not public_key:
        raise ValueError("Missing public age key for encryption")
    if not plain_content:
        raise ValueError("Missing plain content for encryption")

    encrypt_process = await asyncio.create_subprocess_exec(
        "age",
        "--armor",
        "-r",
        public_key,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await encrypt_process.communicate(input=plain_content.encode())

    if encrypt_process.returncode != 0:
        logger.error(f"age encryption failed: {stderr.decode('utf-8', errors='replace')}")
        raise Exception("Age encryption failed")

    # AGE produces ASCII-armored output, but handle encoding issues gracefully
    return stdout.decode("utf-8").strip()


def decrypt_age_content_sync(encrypted_content: str, private_key: str) -> str | None:
    """
    Decrypt age-encrypted content using the provided private key (synchronous version).

    Args:
        encrypted_content: The age-encrypted content (including BEGIN/END markers)
        private_key: The age private key (AGE-SECRET-KEY-...)

    Returns:
        Decrypted content as string, or None if decryption failed
    """
    if not encrypted_content or not private_key:
        logger.error("Missing encrypted content or private key for decryption")
        logger.error(f"Encrypted content provided: {bool(encrypted_content)}")
        logger.error(f"Private key provided: {bool(private_key)}")
        return None

    # Use echo to pipe encrypted content to age for decryption
    # echo "encrypted_content" | age -d -i <(echo "private_key")
    cmd = ["bash", "-c", f'echo "{encrypted_content}" | age -d -i <(echo "{private_key}")']
    logger.debug("Running age decryption command with piped input (sync)")
    logger.debug(f"Command: {' '.join(cmd[:2])} [REDACTED_SECRETS]")

    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)

    if process.returncode != 0:
        error_msg = process.stderr.strip()
        logger.error(f"Age decryption failed with return code {process.returncode}: {error_msg}")
        return None

    decrypted_content = process.stdout.strip()
    logger.info(f"Successfully decrypted age content (sync) - result length: {len(decrypted_content)}")
    return decrypted_content


def is_age_encrypted(content: str) -> bool:
    """
    Check if content is age-encrypted by looking for age markers.

    Args:
        content: Content to check

    Returns:
        True if content appears to be age-encrypted
    """
    if not content:
        return False

    content = content.strip()
    return content.startswith("-----BEGIN AGE ENCRYPTED FILE-----") and content.endswith(
        "-----END AGE ENCRYPTED FILE-----"
    )


async def decrypt_if_encrypted(content: str, private_key: str | None) -> str:
    """
    Decrypt content if it's age-encrypted, otherwise return as-is.

    Args:
        content: Content that may or may not be encrypted
        private_key: Age private key for decryption

    Returns:
        Decrypted content or original content if not encrypted
    """
    if not is_age_encrypted(content):
        return content

    if not private_key:
        raise ValueError("Can not decode content if no private key is provided")

    decrypted = await decrypt_age_content(content, private_key)
    if decrypted is None:
        raise ValueError("Failed to decrypt content")

    return decrypted


def parse_password_with_prefix(password: str) -> tuple[str, str]:
    """
    Parse password with optional namespace prefix.

    Supported prefixes:
    - age:content          -> Age encrypted content (multiline)
    - base64+age:content   -> Base64 encoded Age content (for .env files)
    - plain:content        -> Plain text (explicit)
    - content              -> Auto-detect (plain text or Age)

    Args:
        password: Password string with optional prefix

    Returns:
        Tuple of (type, content) where type is 'plain', 'age', or 'base64+age'
    """
    if not password:
        return "plain", password

    password = password.strip()

    # Check for explicit prefixes
    if password.startswith("age:"):
        return "age", password[4:]  # Remove 'age:' prefix
    elif password.startswith("base64+age:"):
        return "base64+age", password[11:]  # Remove 'base64+age:' prefix
    elif password.startswith("plain:"):
        return "plain", password[6:]  # Remove 'plain:' prefix

    # Auto-detect: check if it looks like Age encrypted content
    if is_age_encrypted(password):
        return "age", password

    # Default to plain text
    return "plain", password


async def decrypt_password_smart_auto(password: str) -> str:
    """
    Smart password decryption that automatically retrieves the Age key from settings.

    Args:
        password: Password with optional prefix

    Returns:
        Decrypted or processed password
    """
    private_key = get_global_private_key()
    return await decrypt_password_smart(password, private_key)


async def decrypt_password_smart(password: str, private_key: str | None) -> str:
    """
    Smart password decryption with prefix support.

    Supports:
    - age:encrypted_content          -> Direct Age decryption
    - base64+age:base64_content      -> Base64 decode then Age decrypt
    - plain:password                 -> Return as-is (no decryption)
    - Auto-detect for backward compatibility

    Args:
        password: Password with optional prefix
        private_key: Age private key for decryption

    Returns:
        Decrypted or processed password
    """
    if not password:
        return password

    password_type, content = parse_password_with_prefix(password)

    logger.debug(f"Password type detected: {password_type}")

    if password_type == "plain":
        return content

    elif password_type == "age":
        if not private_key:
            raise ValueError("Age encrypted password found but no private key available")

        decrypted = await decrypt_age_content(content, private_key)
        if decrypted is None:
            raise ValueError("Failed to decrypt Age password")
        return decrypted

    elif password_type == "base64+age":
        if not private_key:
            raise ValueError("Base64+Age encrypted password found but no private key available")

        try:
            # First decode base64
            decoded_content = base64.b64decode(content).decode("utf-8")

            # Then decrypt with Age
            decrypted = await decrypt_age_content(decoded_content, private_key)
            if decrypted is None:
                raise ValueError("Failed to decrypt base64+Age password")
            return decrypted

        except Exception as e:
            raise ValueError(f"Failed to decode base64 content: {e}") from e

    raise ValueError(f"Unknown password type: {password_type}")


def get_project_public_key(project_config: dict) -> str | None:
    """
    Get project's AGE public key, supporting both new (age-) and legacy (sops-) key names.

    Args:
        project_config: Project configuration dictionary

    Returns:
        AGE public key or None if not found
    """
    config = project_config.get("config", {})

    # Try new key name first
    public_key = config.get("age-public-key")
    if public_key:
        return public_key

    # Fallback to legacy key name
    return config.get("sops-public-key")


async def get_decoded_project_private_key(project_config: dict) -> str:
    """
    Get project's AGE private key

    Args:
        project_config: Project configuration dictionary

    Returns:
        decrypted AGE private key
    """
    config = project_config.get("config", {})
    encoded_private_key = config.get("age-private-key")
    if not encoded_private_key:
        raise ValueError("Missing age-private-key, check and fix legacy sops-private-key if exists")
    return await decrypt_age_content(encoded_private_key, cast(str, settings.SOPS_AGE_PRIVATE_KEY))


def decrypt_password_smart_auto_sync(password: str) -> str:
    """
    Smart password decryption that automatically retrieves the Age key from settings (synchronous version).

    Args:
        password: Password with optional prefix

    Returns:
        Decrypted or processed password
    """
    private_key = get_global_private_key()
    return decrypt_password_smart_sync(password, private_key)


def decrypt_password_smart_sync(password: str, private_key: str | None) -> str:
    """
    Smart password decryption with prefix support (synchronous version).

    Supports:
    - age:encrypted_content          -> Direct Age decryption
    - base64+age:base64_content      -> Base64 decode then Age decrypt
    - plain:password                 -> Return as-is (no decryption)
    - Auto-detect for backward compatibility

    Args:
        password: Password with optional prefix
        private_key: Age private key for decryption

    Returns:
        Decrypted or processed password
    """

    if not password:
        raise ValueError("Missing password")

    password_type, content = parse_password_with_prefix(password)

    logger.debug(f"Password type detected: {password_type}")

    if password_type == "plain":
        return content

    elif password_type == "age":
        if not private_key:
            raise ValueError("Age encrypted password found but no private key available")

        decrypted = decrypt_age_content_sync(content, private_key)
        if decrypted is None:
            raise ValueError("Failed to decrypt Age password, returning original")
        return decrypted

    elif password_type == "base64+age":
        logger.debug("Processing base64-encoded Age encrypted password")
        if not private_key:
            raise ValueError("Base64+Age encrypted password found but no private key available")

        # First decode base64
        decoded_content = base64.b64decode(content).decode("utf-8")
        # Then decrypt with Age
        decrypted = decrypt_age_content_sync(decoded_content, private_key)
        if decrypted is None:
            raise ValueError("Failed to decrypt base64+Age password")
        return decrypted

    raise ValueError(f"Unknown password type: {password_type}")
