"""
Age encryption/decryption utilities.
"""

import asyncio
import base64
import logging
import subprocess
import tempfile
from typing import cast

from opi.core.config import settings

logger = logging.getLogger(__name__)


# TODO: replace this method with direct configuration value
def get_global_private_key() -> str:
    return cast("str", settings.SOPS_AGE_PRIVATE_KEY)


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

    # Write private key to a temporary file to avoid shell injection
    with tempfile.NamedTemporaryFile(mode="w", suffix=".key", delete=True) as key_file:
        key_file.write(private_key)
        key_file.flush()

        from opi.core.metrics import track_subprocess_memory

        process = await asyncio.create_subprocess_exec(
            "age",
            "-d",
            "-i",
            key_file.name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with track_subprocess_memory("age"):
            stdout, stderr = await process.communicate(input=encrypted_content.encode())

    if process.returncode != 0:
        error_msg = stderr.decode("utf-8").strip()
        logger.error(f"Age decryption failed: {error_msg}")
        raise Exception(f"Age decryption failed: {error_msg}")

    return stdout.decode("utf-8").strip()


async def encrypt_age_content_as_base64_prefixed(client_secret: str, public_key: str | None) -> str:
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

    from opi.core.metrics import track_subprocess_memory

    encrypt_process = await asyncio.create_subprocess_exec(
        "age",
        "--armor",
        "-r",
        public_key,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    async with track_subprocess_memory("age"):
        stdout, stderr = await encrypt_process.communicate(input=plain_content.encode())

    if encrypt_process.returncode != 0:
        logger.error(f"age encryption failed: {stderr.decode('utf-8', errors='replace')}")
        raise Exception("Age encryption failed")

    # AGE produces ASCII-armored output, but handle encoding issues gracefully
    return stdout.decode("utf-8").strip()


def encrypt_age_content_sync(plain_content: str, public_key: str | None) -> str:
    """
    Encrypt content using age encryption with the provided public key (synchronous version).

    Args:
        plain_content: The content to encrypt
        public_key: The age public key

    Returns:
        Encrypted content as string with AGE markers

    Raises:
        ValueError: If public_key or plain_content is missing
        Exception: If age encryption fails
    """
    if not public_key:
        raise ValueError("Missing public age key for encryption")
    if not plain_content:
        raise ValueError("Missing plain content for encryption")

    process = subprocess.run(
        ["age", "--armor", "-r", public_key],
        input=plain_content,
        capture_output=True,
        text=True,
        check=False,
    )

    if process.returncode != 0:
        error_msg = process.stderr.strip()
        logger.error(f"age encryption failed (sync): {error_msg}")
        raise Exception(f"Age encryption failed: {error_msg}")

    return process.stdout.strip()


def encrypt_age_content_as_base64_prefixed_sync(client_secret: str, public_key: str | None) -> str:
    """De enkelregelige ``base64+age:``-vorm, zonder event loop.

    Zelfde uitkomst als de async variant. Nodig omdat de instellingen bij het opstarten
    genormaliseerd worden, en dat gebeurt buiten een draaiende loop.
    """
    encrypted_content = encrypt_age_content_sync(client_secret, public_key)
    return f"base64+age:{base64.b64encode(encrypted_content.encode()).decode()}"


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

    # Write private key to a temporary file to avoid shell injection
    logger.debug("Running age decryption command (sync)")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".key", delete=True) as key_file:
        key_file.write(private_key)
        key_file.flush()

        process = subprocess.run(
            ["age", "-d", "-i", key_file.name],
            input=encrypted_content,
            capture_output=True,
            text=True,
            check=False,
        )

    if process.returncode != 0:
        error_msg = process.stderr.strip()
        logger.error(f"Age decryption failed with return code {process.returncode}: {error_msg}")
        return None

    decrypted_content = process.stdout.strip()
    logger.info(f"Successfully decrypted age content (sync) - result length: {len(decrypted_content)}")
    return decrypted_content


async def encrypt_file_to_age_block(raw_bytes: bytes, public_key: str | None) -> str:
    """
    Encrypt raw file bytes to an armored AGE block.

    The bytes are base64-encoded first so an arbitrary (binary) payload survives the
    string-based age helpers; the inner content is therefore always base64. Use
    decrypt_age_block_to_bytes to recover the original bytes.
    """
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return await encrypt_age_content(b64, public_key)


def encrypt_file_to_age_block_sync(raw_bytes: bytes, public_key: str | None) -> str:
    """Synchronous variant of encrypt_file_to_age_block."""
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return encrypt_age_content_sync(b64, public_key)


async def decrypt_age_block_to_bytes(age_block: str, private_key: str) -> bytes:
    """Decrypt an AGE block produced by encrypt_file_to_age_block back to the original bytes."""
    b64 = await decrypt_age_content(age_block, private_key)
    return base64.b64decode(b64)


def decrypt_age_block_to_bytes_sync(age_block: str, private_key: str) -> bytes:
    """Synchronous variant of decrypt_age_block_to_bytes."""
    b64 = decrypt_age_content_sync(age_block, private_key)
    if b64 is None:
        raise ValueError("Age decryption of attachment block failed")
    return base64.b64decode(b64)


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


#: Prefix of the single-line encrypted form. Deliberately a plain string in the YAML (it
#: is base64, so it needs no block scalar); ``is_age_encrypted`` does not recognise it
#: because that function answers "can I hand this to age --decrypt", which this form
#: cannot without being decoded first.
BASE64_AGE_PREFIX = "base64+age:"


def carries_encrypted_value(value: object) -> bool:
    """Whether a value holds an encrypted secret, in EITHER stored form.

    The project schema declares exactly two (``$defs/age-encrypted``): the armored block
    and the ``base64+age:`` prefix. Which one a field uses is a storage decision -- the
    armored block is multi-line and needs a literal scalar, the prefixed form is a plain
    one-line string -- but for the question "is this a secret" they are equal.

    Use this rather than testing for one marker: real project files carry the repository
    password, the api key and the project private key in the prefixed form, so code that
    only looks for the armored block silently treats those as ordinary values.

    ``plain:`` is NOT covered. It marks a deliberately unencrypted password, so it is not
    an encrypted value; it is still sensitive, which is worth knowing wherever this is used
    to decide what may be written down.
    """
    if not isinstance(value, str) or not value:
        return False
    return is_age_encrypted(value) or value.strip().startswith(BASE64_AGE_PREFIX)


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


def has_password_prefix(password: str) -> bool:
    """Whether a password already states how it is stored.

    ``parse_password_with_prefix`` answers ``plain`` both for ``plain:secret`` and for a
    bare ``secret``, and here that difference matters. The first is a deliberate choice to
    keep something readable; the second is a value that arrived straight from a Kubernetes
    Secret and has not been through anything yet. Only the second still needs encrypting
    before it can be written to a file that goes to git.
    """
    if not password:
        return False
    password = password.strip()
    return password.startswith(("age:", "base64+age:", "plain:")) or is_age_encrypted(password)


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
        return "plain", password or ""

    password = password.strip()

    # Check for explicit prefixes
    if password.startswith("age:"):
        content = password[4:]
        return ("age", content) if content else ("plain", password)
    elif password.startswith("base64+age:"):
        content = password[11:]
        return ("base64+age", content) if content else ("plain", password)
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
        return password or ""

    encoding_type, content = parse_password_with_prefix(password)

    logger.debug(f"Encoding type detected: {encoding_type}")

    if encoding_type == "plain":
        return content

    elif encoding_type == "age":
        if not private_key:
            raise ValueError("Age encrypted password found but no private key available")

        decrypted = await decrypt_age_content(content, private_key)
        if decrypted is None:
            raise ValueError("Failed to decrypt Age password")
        return decrypted

    elif encoding_type == "base64+age":
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

    raise ValueError(f"Unknown encoding type: {encoding_type}")


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
    return await decrypt_age_content(encoded_private_key, cast("str", settings.SOPS_AGE_PRIVATE_KEY))


def get_decoded_project_private_key_sync(project_config: dict) -> str:
    """Get a project's AGE private key from inside a synchronous callback.

    The async sibling above is the normal path. This one exists for callers that run
    inside a synchronous change function (``ProjectStore.mutate``) and still have to
    decrypt a stored value. It raises rather than returning None on every failure: it
    is used on write paths where "no key" must stop the write, not fall back to
    plaintext.
    """
    config = project_config.get("config", {})
    encoded_private_key = config.get("age-private-key")
    if not encoded_private_key:
        raise ValueError("Missing age-private-key, check and fix legacy sops-private-key if exists")
    system_private_key = settings.SOPS_AGE_PRIVATE_KEY
    if not system_private_key:
        raise ValueError("Missing system AGE private key; cannot decode the project private key")
    decoded = decrypt_age_content_sync(encoded_private_key, system_private_key)
    if not decoded:
        raise ValueError("Could not decrypt the project's age-private-key with the system key")
    return decoded


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
        return password or ""

    encoding_type, content = parse_password_with_prefix(password)

    logger.debug(f"Encoding type detected: {encoding_type}")

    if encoding_type == "plain":
        return content

    elif encoding_type == "age":
        if not private_key:
            raise ValueError("Age encrypted password found but no private key available")

        decrypted = decrypt_age_content_sync(content, private_key)
        if decrypted is None:
            raise ValueError("Failed to decrypt Age password, returning original")
        return decrypted

    elif encoding_type == "base64+age":
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

    raise ValueError(f"Unknown encoding type: {encoding_type}")
