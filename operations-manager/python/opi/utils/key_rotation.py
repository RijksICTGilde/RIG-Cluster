"""The loop that moves an encrypted value from one AGE key to another.

``convert_value`` and what hangs off it live here and not in
``scripts/key_rotation.py``, which imports these names.

``rotate_project_key`` is the round over one project file. Every value that opens with the
old project key moves to the new public key; what does not open with that key
(``config.age-private-key`` and ``repositories[].password`` sit on the platform key) stays
exactly as it is.
"""

from __future__ import annotations

import base64
import binascii
import copy
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from opi.utils.age import (
    BASE64_AGE_PREFIX,
    AgeDecryptionError,
    decrypt_age_content,
    encrypt_age_content,
    encrypt_to_base64_age,
    is_age_encrypted,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


class Form(StrEnum):
    """The storage form of an encrypted value. It has to survive the conversion.

    ``BLOCK`` is the ASCII-armored AGE block: multi-line, a literal block scalar in YAML.
    ``BASE64`` is that same block base64-encoded once more behind ``base64+age:``, a single
    line, which is what lets it fit in a ``KEY=value`` env line. A field that switches form
    is a content change, and a rotation must not make one.
    """

    BLOCK = "age-block"
    BASE64 = "base64+age"


class ConversionError(RuntimeError):
    """A field does not open with the old key, or does not open with the new one afterwards."""


def form_of(value: object) -> Form | None:
    """The storage form of a value, or None when nothing encrypted is in it."""
    if not isinstance(value, str) or not value:
        return None
    stripped = value.strip()
    if stripped.startswith(BASE64_AGE_PREFIX):
        return Form.BASE64
    if is_age_encrypted(stripped):
        return Form.BLOCK
    return None


def to_age_block(value: str, form: Form) -> str:
    """Peel the storage form back to the armored AGE block that ``age -d`` accepts."""
    if form is Form.BASE64:
        return base64.b64decode(value.strip()[len(BASE64_AGE_PREFIX) :]).decode()
    return value.strip()


def sha256_of(plaintext: str) -> str:
    """The fingerprint of one field: sha256 of the plaintext, hex."""
    return sha256(plaintext.encode()).hexdigest()


@dataclass(frozen=True)
class Conversion:
    """The result of one conversion. Holds the new ciphertext, never the plaintext."""

    new_value: str
    sha256_before: str
    sha256_after: str
    replaced: bool

    @property
    def content_unchanged(self) -> bool:
        """Whether the plaintext stayed the same. For a replacement this should be False."""
        return self.sha256_before == self.sha256_after


async def convert_value(
    value: str,
    old_private_key: str,
    new_public_key: str,
    *,
    new_plaintext: str | None = None,
) -> Conversion:
    """THE loop: read, decrypt with A, keep or replace, encrypt for B, hand it back.

    Without ``new_plaintext`` this is a recrypt: the plaintext stays the same and the hash
    before and after is identical. With ``new_plaintext`` it is a replacement plus a
    recrypt, and then the hash is supposed to differ -- at exactly those fields and nowhere
    else.

    The storage form carries over: a ``base64+age:`` value comes back as ``base64+age:``
    and an armored block as an armored block. A field that switches form is itself a change.
    """
    form = form_of(value)
    if form is None:
        raise ConversionError("value is not encrypted and should not reach this loop")

    plain_before = await decrypt_age_content(to_age_block(value, form), old_private_key)
    plain_after = new_plaintext if new_plaintext is not None else plain_before

    if form is Form.BASE64:
        new_value = await encrypt_to_base64_age(plain_after, new_public_key)
    else:
        new_value = await encrypt_age_content(plain_after, new_public_key)

    return Conversion(
        new_value=new_value,
        sha256_before=sha256_of(plain_before),
        sha256_after=sha256_of(plain_after),
        replaced=new_plaintext is not None,
    )


async def decrypt_field(value: str, private_key: str) -> str | None:
    """The plaintext of a field, or None when this key does not open it.

    None rather than an exception: both outcomes are a normal answer here. The final check
    specifically wants to KNOW that the old key fails, and a verification round has to be
    able to name every closed field instead of aborting on the first one.
    """
    form = form_of(value)
    if form is None:
        return None
    try:
        return await decrypt_age_content(to_age_block(value, form), private_key)
    except AgeDecryptionError, binascii.Error, UnicodeDecodeError:
        # Only "this key does not open it" or an unreadable shape gives None. Every other
        # error has to break through: the final check reads a None as "no longer on the old
        # key" and would miss a field that did in fact stay behind.
        return None


async def opens_with(value: str, private_key: str) -> bool:
    """Whether a field can be decrypted with this key. The core of the final check."""
    return await decrypt_field(value, private_key) is not None


@dataclass(frozen=True)
class EncryptedField:
    """An encrypted value in the tree, with the place it is written back to."""

    path: str
    value: str
    container: Any
    key: Any

    def write(self, new_value: str) -> None:
        self.container[self.key] = new_value


@dataclass
class RotationResult:
    """What the conversion did with one project file: field paths, never values."""

    data: dict[str, Any]
    rotated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def encrypted_fields(node: Any, path: str = "") -> Iterator[EncryptedField]:  # noqa: ANN401  # a project-file value
    """Every encrypted value in the tree, with a field path such as ``components[0].aliases``.

    On the shape of the value and not on a list of fields: a service can carry encrypted
    config of its own, and that has to go through the same loop.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(value, str) and form_of(value) is not None:
                yield EncryptedField(path=child, value=value, container=node, key=key)
            else:
                yield from encrypted_fields(value, child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            child = f"{path}[{index}]"
            if isinstance(item, str) and form_of(item) is not None:
                yield EncryptedField(path=child, value=item, container=node, key=index)
            else:
                yield from encrypted_fields(item, child)


async def rotate_project_key(project_data: dict[str, Any], old_private_key: str, new_public_key: str) -> RotationResult:
    """Move every field on the old project key to the new public key.

    Works on a copy: ``project_data`` stays untouched, even when the conversion breaks off.
    This function does not put the new key pair in ``config`` itself; the caller does that,
    after the conversion.

    Raises:
        ConversionError: when a field still opens with the old project key after the
            conversion. No half-converted tree comes back then.
    """
    data = copy.deepcopy(project_data)
    result = RotationResult(data=data)

    for field_ in list(encrypted_fields(data)):
        if await decrypt_field(field_.value, old_private_key) is None:
            result.skipped.append(field_.path)
            continue
        conversion = await convert_value(field_.value, old_private_key, new_public_key)
        field_.write(conversion.new_value)
        result.rotated.append(field_.path)

    left_behind = [field_.path for field_ in encrypted_fields(data) if await opens_with(field_.value, old_private_key)]
    if left_behind:
        raise ConversionError(
            f"deze velden gaan na de omzetting nog met de oude projectsleutel open: {', '.join(left_behind)}"
        )

    return result
