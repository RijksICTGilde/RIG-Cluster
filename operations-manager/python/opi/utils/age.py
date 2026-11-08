"""AGE encryption and decryption, in process, plus the wrapping of a project key.

Encrypting and decrypting runs through pyrage instead of the age binary, so there is no
fork and no tempfile per field. The two stored forms (the armored block and the one-line
``base64+age:``) and the prefixes that name them live here as well, so every reader
recognises a secret the same way.
"""

import base64
import contextlib
import logging
from typing import Any, cast

import pyrage

from opi.core.config import settings

logger = logging.getLogger(__name__)


class AgeDecryptionError(Exception):
    """Age could not open this content: wrong key or damaged ciphertext.

    A type of its own, so a caller that treats "this key does not open it" as a normal
    outcome can tell it apart from any other error.
    """


def get_global_private_key() -> str:
    return cast("str", settings.SOPS_AGE_PRIVATE_KEY)


def _encrypt_in_process(plain_content: str, public_key: str) -> str:
    """Not byte-identical to the age binary (it is not deterministic) but readable by it."""
    recipient = pyrage.x25519.Recipient.from_str(public_key.strip())
    return pyrage.encrypt(plain_content.encode(), [recipient], armored=True).decode("utf-8").strip()


def _decrypt_in_process(encrypted_content: str, private_key: str) -> str:
    """Decrypt without starting the age binary: no fork per field, and the private key
    stays in memory instead of in a tempfile.
    """
    identity = pyrage.x25519.Identity.from_str(private_key.strip())
    return pyrage.decrypt(encrypted_content.strip().encode(), [identity]).decode("utf-8").strip()


async def decrypt_age_content(encrypted_content: str, private_key: str) -> str:
    """Decrypt age-encrypted content with the given private key.

    Stays async without awaiting anything: that is what keeps every caller unchanged.

    Raises:
        ValueError: when content or key is missing.
        AgeDecryptionError: when age cannot open the content.
    """
    if not encrypted_content or not private_key:
        raise ValueError("Missing encrypted content or private key for decryption")

    try:
        return _decrypt_in_process(encrypted_content, private_key)
    except (pyrage.DecryptError, pyrage.IdentityError) as e:
        logger.error(f"Age decryption failed: {e}")
        raise AgeDecryptionError(f"Age decryption failed: {e}") from e


async def _encrypt_with_age_and_base64encode_as_prefixed_string(client_secret: str, public_key: str | None) -> str:
    """Encrypt to the one-line ``base64+age:`` form.

    That form fits where a single-line value is expected, such as a ``KEY=value`` env line.
    """
    encrypted_content = await encrypt_age_content(client_secret, public_key)
    return f"base64+age:{base64.b64encode(encrypted_content.encode()).decode()}"


async def encrypt_age_content(plain_content: str, public_key: str | None) -> str:
    """Encrypt content to an armored AGE block with the given public key."""

    if not public_key:
        raise ValueError("Missing public age key for encryption")
    if not plain_content:
        raise ValueError("Missing plain content for encryption")

    try:
        return _encrypt_in_process(plain_content, public_key)
    except (pyrage.EncryptError, pyrage.RecipientError) as e:
        logger.error(f"Age encryption failed: {e}")
        raise Exception(f"Age encryption failed: {e}") from e


def encrypt_age_content_sync(plain_content: str, public_key: str | None) -> str:
    """Synchronous variant of :func:`encrypt_age_content`.

    Raises:
        ValueError: when public_key or plain_content is missing.
        Exception: when age encryption fails.
    """
    if not public_key:
        raise ValueError("Missing public age key for encryption")
    if not plain_content:
        raise ValueError("Missing plain content for encryption")

    try:
        return _encrypt_in_process(plain_content, public_key)
    except (pyrage.EncryptError, pyrage.RecipientError) as e:
        logger.error(f"Age encryption failed (sync): {e}")
        raise Exception(f"Age encryption failed: {e}") from e


def decrypt_age_content_sync(encrypted_content: str, private_key: str, *, log_failure: bool = True) -> str | None:
    """Synchronous variant of :func:`decrypt_age_content`, returning None on failure.

    Args:
        encrypted_content: the age-encrypted content, BEGIN/END markers included.
        private_key: the age private key.
        log_failure: False when the caller tries several keys, as
            :func:`unwrap_project_key_sync` does: a candidate that does not fit is no
            error there.
    """
    if not encrypted_content or not private_key:
        logger.error("Missing encrypted content or private key for decryption")
        logger.error(f"Encrypted content provided: {bool(encrypted_content)}")
        logger.error(f"Private key provided: {bool(private_key)}")
        return None

    try:
        decrypted_content = _decrypt_in_process(encrypted_content, private_key)
    except (pyrage.DecryptError, pyrage.IdentityError) as e:
        logger.log(logging.ERROR if log_failure else logging.DEBUG, f"Age decryption failed (sync): {e}")
        return None

    logger.debug(f"Successfully decrypted age content (sync) - result length: {len(decrypted_content)}")
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
    """Whether the content carries the armored AGE markers."""
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


def parse_password_with_prefix(password: str) -> tuple[str, str]:
    """Split a password into its prefix type and its content.

    Supported prefixes:
    - age:content          -> Age encrypted content (multiline)
    - base64+age:content   -> Base64 encoded Age content (for .env files)
    - plain:content        -> Plain text (explicit)
    - content              -> Auto-detect (plain text or Age)

    Returns the type as ``plain``, ``age`` or ``base64+age``, with the content behind it.
    """
    if not password:
        return "plain", password or ""

    password = password.strip()

    if password.startswith("age:"):
        content = password[4:]
        return ("age", content) if content else ("plain", password)
    elif password.startswith("base64+age:"):
        content = password[11:]
        return ("base64+age", content) if content else ("plain", password)
    elif password.startswith("plain:"):
        return "plain", password[6:]

    if is_age_encrypted(password):
        return "age", password

    return "plain", password


async def decrypt_password_smart_auto(password: str) -> str:
    """:func:`decrypt_password_smart` with the platform key from the settings."""
    private_key = get_global_private_key()
    return await decrypt_password_smart(password, private_key)


async def decrypt_password_smart(password: str, private_key: str | None) -> str:
    """Decrypt a password by the prefix it carries, and return a plain one untouched.

    Supports:
    - age:encrypted_content          -> Direct Age decryption
    - base64+age:base64_content      -> Base64 decode then Age decrypt
    - plain:password                 -> Return as-is (no decryption)
    - Auto-detect for backward compatibility
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
            decoded_content = base64.b64decode(content).decode("utf-8")
            decrypted = await decrypt_age_content(decoded_content, private_key)
            if decrypted is None:
                raise ValueError("Failed to decrypt base64+Age password")
            return decrypted

        except Exception as e:
            raise ValueError(f"Failed to decode base64 content: {e}") from e

    raise ValueError(f"Unknown encoding type: {encoding_type}")


async def decrypt_tree(data: Any, private_key: str, *, include_prefixed: bool = False) -> None:
    """Recursively walk ``data`` and decrypt every encrypted string value in place.

    Values that will not decrypt with this key are left untouched, so the walk can be
    run more than once with different keys: a project file carries values encrypted
    with the project key and values encrypted with the system key, and a second pass
    picks up what the first could not open.

    Args:
        data: Nested dict/list structure, mutated in place.
        private_key: AGE private key to try.
        include_prefixed: Also decrypt the one-line ``base64+age:`` form. Off by default
            because the caller that predates this flag (the decrypted project view) only
            ever opened armored blocks, and widening that silently would start showing
            repository passwords and api keys where they were not shown before.
    """
    carries = carries_encrypted_value if include_prefixed else is_age_encrypted

    async def _decrypt(value: str) -> str:
        return (
            await decrypt_password_smart(value, private_key)
            if include_prefixed
            else await decrypt_age_content(value, private_key)
        )

    if isinstance(data, dict):
        for key, value in list(data.items()):
            if isinstance(value, str) and carries(value):
                try:
                    data[key] = await _decrypt(value)
                except Exception:
                    logger.debug(f"Failed to decrypt field '{key}', leaving as-is")
            elif isinstance(value, dict | list):
                await decrypt_tree(value, private_key, include_prefixed=include_prefixed)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, str) and carries(item):
                with contextlib.suppress(Exception):
                    data[i] = await _decrypt(item)
            elif isinstance(item, dict | list):
                await decrypt_tree(item, private_key, include_prefixed=include_prefixed)


def get_project_public_key(project_config: dict) -> str | None:
    """The project's AGE public key, under the current ``age-`` or the legacy ``sops-`` name."""
    config = project_config.get("config", {})

    public_key = config.get("age-public-key")
    if public_key:
        return public_key

    return config.get("sops-public-key")


# The project keys sit under the project KEK, which exists only in the OPI pod; the platform
# key is the fallback for as long as not every project file has been converted (RC-334).
PROJECT_KEK_LABEL = "project-kek"
PLATFORM_KEY_LABEL = "platformsleutel"


def project_key_candidates_for(
    platform_private_key: str | None, kek_private_key: str | None = None
) -> list[tuple[str, str]]:
    """Keys that can open a project key, KEK first.

    For migration scripts that know the keys of the SOURCE cluster, not those of their own
    settings.
    """
    labelled = ((PROJECT_KEK_LABEL, kek_private_key), (PLATFORM_KEY_LABEL, platform_private_key))
    return [(label, key) for label, key in labelled if key]


def project_key_candidates() -> list[tuple[str, str]]:
    """Keys that can open a project key: KEK, then platform.

    Without a KEK only the platform key is used, so the sandbox and local dev need nothing.
    """
    return project_key_candidates_for(settings.SOPS_AGE_PRIVATE_KEY, settings.PROJECT_KEK_PRIVATE_KEY)


def project_wrapping_public_key() -> str:
    """The public half a project key is wrapped with: the KEK as soon as there is one."""
    public_key = settings.PROJECT_KEK_PUBLIC_KEY or settings.SOPS_AGE_PUBLIC_KEY
    if not public_key:
        raise ValueError("No AGE public key available to wrap a project private key with")
    return public_key


def unwrap_project_key_sync(wrapped: str, candidates: list[tuple[str, str]] | None = None) -> str:
    """Unwrap a project key with the first candidate that fits, and log which one it was."""
    if not wrapped:
        raise ValueError("Missing age-private-key, check and fix legacy sops-private-key if exists")
    candidates = project_key_candidates() if candidates is None else candidates
    if not candidates:
        raise ValueError("No AGE private key available to unwrap the project private key with")
    for label, private_key in candidates:
        try:
            # log_failure off: during the transition the KEK does not fit a file that has not
            # been converted yet, and that is the expected state, not an error.
            decoded = decrypt_password_smart_sync(wrapped, private_key, log_failure=False)
        except ValueError, pyrage.DecryptError, pyrage.IdentityError:
            continue
        if decoded:
            logger.debug(f"Project private key unwrapped with the {label}")
            return decoded
    raise ValueError(f"Could not unwrap the project private key with any of: {[label for label, _ in candidates]}")


async def unwrap_project_key(wrapped: str, candidates: list[tuple[str, str]] | None = None) -> str:
    """The async side of :func:`unwrap_project_key_sync`; same order and same fallback."""
    return unwrap_project_key_sync(wrapped, candidates)


def wrap_project_key_sync(private_key: str, public_key: str | None = None) -> str:
    """Wrap a project key with the KEK, or with the given public_key for a migration."""
    return encrypt_age_content_sync(private_key, public_key or project_wrapping_public_key())


async def wrap_project_key(private_key: str, public_key: str | None = None) -> str:
    """The async side of :func:`wrap_project_key_sync`."""
    return await encrypt_age_content(private_key, public_key or project_wrapping_public_key())


async def get_decoded_project_private_key(project_config: dict) -> str:
    """The project's AGE private key, unwrapped."""
    config = project_config.get("config", {})
    return await unwrap_project_key(config.get("age-private-key"))


def get_decoded_project_private_key_sync(project_config: dict) -> str:
    """Get a project's AGE private key from inside a synchronous callback.

    The async sibling above is the normal path. This one exists for callers that run
    inside a synchronous change function (``ProjectStore.mutate``) and still have to
    decrypt a stored value. It raises rather than returning None on every failure: it
    is used on write paths where "no key" must stop the write, not fall back to
    plaintext.
    """
    config = project_config.get("config", {})
    return unwrap_project_key_sync(config.get("age-private-key"))


def decrypt_password_smart_auto_sync(password: str) -> str:
    """:func:`decrypt_password_smart_sync` with the platform key from the settings."""
    private_key = get_global_private_key()
    return decrypt_password_smart_sync(password, private_key)


def decrypt_password_smart_sync(password: str, private_key: str | None, *, log_failure: bool = True) -> str:
    """Synchronous variant of :func:`decrypt_password_smart`, with the same prefixes."""

    if not password:
        return password or ""

    encoding_type, content = parse_password_with_prefix(password)

    logger.debug(f"Encoding type detected: {encoding_type}")

    if encoding_type == "plain":
        return content

    elif encoding_type == "age":
        if not private_key:
            raise ValueError("Age encrypted password found but no private key available")

        decrypted = decrypt_age_content_sync(content, private_key, log_failure=log_failure)
        if decrypted is None:
            raise ValueError("Failed to decrypt Age password, returning original")
        return decrypted

    elif encoding_type == "base64+age":
        logger.debug("Processing base64-encoded Age encrypted password")
        if not private_key:
            raise ValueError("Base64+Age encrypted password found but no private key available")

        decoded_content = base64.b64decode(content).decode("utf-8")
        decrypted = decrypt_age_content_sync(decoded_content, private_key, log_failure=log_failure)
        if decrypted is None:
            raise ValueError("Failed to decrypt base64+Age password")
        return decrypted

    raise ValueError(f"Unknown encoding type: {encoding_type}")
