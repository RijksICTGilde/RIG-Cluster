"""Cryptographically secure passwords, with a minimum per character class."""

import secrets
import string


def generate_secure_password(
    min_uppercase: int = 3,
    min_lowercase: int = 3,
    min_digits: int = 3,
    total_length: int = 20,
    additional_chars: str = "",
) -> str:
    """A secure password meeting the per-class minimums, padded to ``total_length``.

    ``additional_chars`` widens the set the padding is drawn from, never the minimums.

    Raises:
        ValueError: when the minimums together exceed ``total_length``.

    Example:
        >>> password = generate_secure_password(min_uppercase=2, min_lowercase=2, min_digits=2, total_length=12)
        >>> len(password)
        12
    """
    min_required = min_uppercase + min_lowercase + min_digits
    if min_required > total_length:
        raise ValueError(f"Minimum character requirements ({min_required}) exceed total length ({total_length})")

    password_chars = []
    password_chars.extend(secrets.choice(string.ascii_uppercase) for _ in range(min_uppercase))
    password_chars.extend(secrets.choice(string.ascii_lowercase) for _ in range(min_lowercase))
    password_chars.extend(secrets.choice(string.digits) for _ in range(min_digits))

    remaining_chars = total_length - len(password_chars)
    char_set = string.ascii_letters + string.digits + additional_chars
    password_chars.extend(secrets.choice(char_set) for _ in range(remaining_chars))

    # Shuffled, because the minimums were appended per class and would otherwise sit in a
    # predictable order.
    secrets.SystemRandom().shuffle(password_chars)

    return "".join(password_chars)


def generate_alphanumeric_password(length: int = 20) -> str:
    """:func:`generate_secure_password` with the minimums spread evenly over the length.

    Example:
        >>> password = generate_alphanumeric_password(16)
        >>> len(password)
        16
    """
    min_each = max(1, length // 6)  # at least one of each class, or a sixth of the length

    return generate_secure_password(
        min_uppercase=min_each, min_lowercase=min_each, min_digits=min_each, total_length=length
    )
