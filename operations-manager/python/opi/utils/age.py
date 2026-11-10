"""AGE encryption and decryption, in process, plus the wrapping of a project key.

Encrypting and decrypting runs through pyrage instead of the age binary, so there is no
fork and no tempfile per field. The two stored forms (the armored block and the one-line
``base64+age:``) and the prefixes that name them live here as well, so every reader
recognises a secret the same way.
"""

import base64
import binascii
import contextlib
import logging
import re
from typing import Any, cast

import pyrage

from opi.core.config import settings

logger = logging.getLogger(__name__)


class AgeDecryptionError(ValueError):
    """Age could not open this content: wrong key, damaged ciphertext, or non-text plaintext.

    A ``ValueError``, so a caller that only distinguishes "unusable value" catches it with
    everything else this module refuses; a type of its own for a caller that treats "this
    key does not open it" as a normal outcome.
    """


class AgeEncryptionError(ValueError):
    """A value that has to be stored encrypted could not be encrypted.

    Raised instead of writing the plain value down, so the save that triggered it stops. The
    message names the field, never the value.
    """


def get_global_private_key() -> str:
    return cast("str", settings.SOPS_AGE_PRIVATE_KEY)


def _encrypt_in_process(plain_content: str, public_key: str) -> str:
    """Not byte-identical to the age binary (it is not deterministic) but readable by it."""
    recipient = pyrage.x25519.Recipient.from_str(public_key.strip())
    return pyrage.encrypt(plain_content.encode(), [recipient], armored=True).decode("utf-8").strip()


def generate_age_key_pair() -> tuple[str, str]:
    """A fresh AGE key pair as ``(private_key, public_key)``, generated in process.

    No ``age-keygen`` and no tempfile: the private key is never written to disk, and the
    call does not hold the event loop while a process starts.
    """
    identity = pyrage.x25519.Identity.generate()
    return str(identity), str(identity.to_public())


def _decrypt_block(encrypted_content: str, private_key: str) -> str:
    """Decrypt an armored AGE block, without starting the age binary.

    No fork per field, and the private key stays in memory instead of in a tempfile. The
    only place a pyrage failure becomes ours, so every variant below answers the same way.

    Raises:
        ValueError: when content or key is missing.
        AgeDecryptionError: wrong key, damaged ciphertext, or plaintext that is not utf-8.
            The message never carries the key or the ciphertext.
    """
    if not encrypted_content or not private_key:
        raise ValueError("Missing encrypted content or private key for decryption")
    try:
        identity = pyrage.x25519.Identity.from_str(private_key.strip())
        return pyrage.decrypt(encrypted_content.strip().encode(), [identity]).decode("utf-8").strip()
    except (pyrage.DecryptError, pyrage.IdentityError, UnicodeDecodeError) as e:
        raise AgeDecryptionError(f"Age decryption failed: {e}") from e


async def decrypt_age_content(encrypted_content: str, private_key: str) -> str:
    """Decrypt an armored AGE block, or raise.

    Stays async without awaiting anything: that is what keeps every caller unchanged.

    Raises:
        ValueError: when content or key is missing.
        AgeDecryptionError: when age cannot open the content.
    """
    return _decrypt_block(encrypted_content, private_key)


async def encrypt_to_base64_age(plain_content: str, public_key: str | None) -> str:
    """The async side of :func:`encrypt_to_base64_age_sync`."""
    return encrypt_to_base64_age_sync(plain_content, public_key)


def encrypt_to_base64_age_sync(plain_content: str, public_key: str | None) -> str:
    """Encrypt to the one-line ``base64+age:`` form.

    That form fits where a single-line value is expected, such as a ``KEY=value`` env line
    or a password in a project file.
    """
    encrypted_content = encrypt_age_content_sync(plain_content, public_key)
    return f"{BASE64_AGE_PREFIX}{base64.b64encode(encrypted_content.encode()).decode()}"


async def encrypt_age_content(plain_content: str, public_key: str | None) -> str:
    """The async side of :func:`encrypt_age_content_sync`."""
    return encrypt_age_content_sync(plain_content, public_key)


def encrypt_age_content_sync(plain_content: str, public_key: str | None) -> str:
    """Encrypt content to an armored AGE block with the given public key.

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
        logger.error(f"Age encryption failed: {e}")
        raise Exception(f"Age encryption failed: {e}") from e


def decrypt_age_content_sync(encrypted_content: str, private_key: str, *, log_failure: bool = True) -> str | None:
    """Decrypt an armored AGE block, returning None instead of raising.

    Args:
        encrypted_content: the age-encrypted content, BEGIN/END markers included.
        private_key: the age private key.
        log_failure: False when the caller tries several keys, as
            :func:`unwrap_project_key_sync` does: a candidate that does not fit is no
            error there.
    """
    try:
        return _decrypt_block(encrypted_content, private_key)
    except ValueError as e:
        logger.log(logging.ERROR if log_failure else logging.DEBUG, f"Age decryption failed: {e}")
        return None


def encrypt_file_to_age_block_sync(raw_bytes: bytes, public_key: str | None) -> str:
    """Encrypt raw file bytes to an armored AGE block.

    The bytes are base64-encoded first so an arbitrary (binary) payload survives the
    string-based age helpers; the inner content is therefore always base64. Use
    :func:`decrypt_age_block_to_bytes` to recover the original bytes.
    """
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return encrypt_age_content_sync(b64, public_key)


def decrypt_age_block_to_bytes_sync(age_block: str, private_key: str) -> bytes:
    """Decrypt a block from :func:`encrypt_file_to_age_block_sync` back to the original bytes.

    Raises:
        ValueError: when age cannot open the block, or the plaintext is not base64.
    """
    return base64.b64decode(_decrypt_block(age_block, private_key))


async def decrypt_age_block_to_bytes(age_block: str, private_key: str) -> bytes:
    """The async side of :func:`decrypt_age_block_to_bytes_sync`."""
    return decrypt_age_block_to_bytes_sync(age_block, private_key)


#: The markers around the armored form.
AGE_BLOCK_BEGIN = "-----BEGIN AGE ENCRYPTED FILE-----"
AGE_BLOCK_END = "-----END AGE ENCRYPTED FILE-----"


def is_age_encrypted(content: str) -> bool:
    """Whether the content carries the armored AGE markers."""
    if not content:
        return False

    content = content.strip()
    return content.startswith(AGE_BLOCK_BEGIN) and content.endswith(AGE_BLOCK_END)


#: Prefix of the single-line encrypted form. Deliberately a plain string in the YAML (it
#: is base64, so it needs no block scalar); ``is_age_encrypted`` does not recognise it
#: because that function answers "can I hand this to age --decrypt", which this form
#: cannot without being decoded first.
BASE64_AGE_PREFIX = "base64+age:"

#: The two stored forms as one regex, for a pydantic field or a JSON schema. Also what
#: ``find_plaintext_service_config_violations`` recognises a secret field BY, so a field that
#: must hold a secret carries this pattern and nothing looser.
AGE_ENCRYPTED_PATTERN = rf"({AGE_BLOCK_BEGIN}|^{re.escape(BASE64_AGE_PREFIX)})"

#: Prefix that marks a value as deliberately NOT encrypted. A project file may no longer
#: carry one (schema version 2.18); a setting still may, because the env secret is a SOPS file.
PLAIN_PREFIX = "plain:"


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


def has_encrypted_marker(value: object) -> bool:
    """Whether an encryption marker appears anywhere in *value*.

    The counterpart of :func:`carries_encrypted_value` for the in-between: a value that
    mentions a marker without BEING an encrypted value is read as plain text by every
    reader, and written down as it is. Refuse it rather than guess which half was meant.
    """
    return isinstance(value, str) and (AGE_BLOCK_BEGIN in value or BASE64_AGE_PREFIX in value)


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
    elif password.startswith(PLAIN_PREFIX):
        return "plain", password[len(PLAIN_PREFIX) :]

    if is_age_encrypted(password):
        return "age", password

    return "plain", password


async def decrypt_password_smart_auto(password: str) -> str:
    """:func:`decrypt_password_smart` with the platform key from the settings."""
    private_key = get_global_private_key()
    return await decrypt_password_smart(password, private_key)


async def decrypt_password_smart(password: str, private_key: str | None) -> str:
    """The async side of :func:`decrypt_password_smart_sync`."""
    return decrypt_password_smart_sync(password, private_key)


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
        except ValueError:
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


def project_private_key_or_none(project_data: dict[str, Any] | None) -> str | None:
    """The project's unwrapped AGE private key, or None when it cannot be resolved.

    For a read path that has something to show without the key: a form that leaves the
    stored value standing, or a skip-if-unchanged check that re-encrypts instead. A write
    path uses :func:`get_decoded_project_private_key_sync`, which refuses instead.
    """
    if not project_data:
        return None
    try:
        return get_decoded_project_private_key_sync(project_data)
    except ValueError as e:
        logger.warning(f"No usable project private key: {e}")
        return None


def decrypt_password_smart_auto_sync(password: str) -> str:
    """:func:`decrypt_password_smart_sync` with the platform key from the settings."""
    private_key = get_global_private_key()
    return decrypt_password_smart_sync(password, private_key)


def decrypt_password_smart_sync(password: str, private_key: str | None, *, log_failure: bool = True) -> str:
    """Decrypt a stored value in whichever form it carries, and return a plain one untouched.

    Args:
        password: an armored block, ``age:``, ``base64+age:``, ``plain:`` or bare text.
        private_key: the age private key; only an encrypted form needs one.
        log_failure: False when the caller tries several keys, as
            :func:`unwrap_project_key_sync` does: a candidate that does not fit is no
            error there.

    Raises:
        ValueError: an encrypted form without a key, or a ``base64+age:`` payload that is
            not base64.
        AgeDecryptionError: when age cannot open the content.
    """
    if not password:
        return password or ""

    encoding_type, content = parse_password_with_prefix(password)

    if encoding_type == "plain":
        return content

    if not private_key:
        raise ValueError(f"{encoding_type} encrypted password found but no private key available")

    if encoding_type == "base64+age":
        try:
            content = base64.b64decode(content).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as e:
            raise ValueError(f"Failed to decode base64 content: {e}") from e

    try:
        return _decrypt_block(content, private_key)
    except AgeDecryptionError as e:
        logger.log(logging.ERROR if log_failure else logging.DEBUG, str(e))
        raise


def ensure_base64_age_sync(value: str, private_key: str | None, public_key: str | None) -> str:
    """``value`` as ``base64+age:`` with ``public_key``. A value already in that form stays exactly as it is.

    For a secret from the environment that ends up in a file: whoever filled it in may have
    written it ``plain:`` or bare, and a file never knows where it goes next.
    """
    form, _ = parse_password_with_prefix(value)
    if form == "base64+age":
        return value
    return encrypt_to_base64_age_sync(decrypt_password_smart_sync(value, private_key), public_key)
