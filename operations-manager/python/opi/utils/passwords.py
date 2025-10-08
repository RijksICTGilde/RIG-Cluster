"""
Password generation utilities for secure random password creation.

This module provides utilities for generating cryptographically secure passwords
with configurable character set requirements.
"""

import secrets
import string


def generate_secure_password(
    min_uppercase: int = 3,
    min_lowercase: int = 3,
    min_digits: int = 3,
    total_length: int = 20,
    additional_chars: str = "",
) -> str:
    """
    Generate a cryptographically secure password with specified character requirements.

    Args:
        min_uppercase: Minimum number of uppercase letters to include (default: 3)
        min_lowercase: Minimum number of lowercase letters to include (default: 3)
        min_digits: Minimum number of digits to include (default: 3)
        total_length: Total length of the generated password (default: 20)
        additional_chars: Additional characters to include in the character set (default: "")

    Returns:
        A cryptographically secure password meeting the specified requirements

    Raises:
        ValueError: If the minimum character requirements exceed the total length

    Example:
        >>> password = generate_secure_password(min_uppercase=2, min_lowercase=2, min_digits=2, total_length=12)
        >>> len(password)
        12
    """
    # Validate input parameters
    min_required = min_uppercase + min_lowercase + min_digits
    if min_required > total_length:
        raise ValueError(f"Minimum character requirements ({min_required}) exceed total length ({total_length})")

    password_chars = []

    # Add minimum required uppercase letters
    password_chars.extend(secrets.choice(string.ascii_uppercase) for _ in range(min_uppercase))

    # Add minimum required lowercase letters
    password_chars.extend(secrets.choice(string.ascii_lowercase) for _ in range(min_lowercase))

    # Add minimum required digits
    password_chars.extend(secrets.choice(string.digits) for _ in range(min_digits))

    # Calculate remaining characters needed
    remaining_chars = total_length - len(password_chars)

    # Define the character set for remaining positions
    # Use alphanumeric characters plus any additional characters specified
    char_set = string.ascii_letters + string.digits + additional_chars

    # Fill remaining positions with random characters from the full set
    password_chars.extend(secrets.choice(char_set) for _ in range(remaining_chars))

    # Shuffle all characters to avoid predictable patterns
    secrets.SystemRandom().shuffle(password_chars)

    return "".join(password_chars)


def generate_alphanumeric_password(length: int = 20) -> str:
    """
    Generate a cryptographically secure alphanumeric password.

    This is a convenience function that generates a password with balanced
    character requirements suitable for most applications.

    Args:
        length: Total length of the password (default: 20)

    Returns:
        A cryptographically secure alphanumeric password

    Example:
        >>> password = generate_alphanumeric_password(16)
        >>> len(password)
        16
    """
    # Calculate balanced distribution for the given length
    min_each = max(1, length // 6)  # At least 1 of each type, or 1/6 of total length

    return generate_secure_password(
        min_uppercase=min_each, min_lowercase=min_each, min_digits=min_each, total_length=length
    )
