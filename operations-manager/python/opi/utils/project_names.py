"""
Project name generation utilities.
"""

import random
import re
import string

from opi.core.config import settings


def generate_project_name(display_name: str) -> tuple[str, str]:
    """
    Generate a technical project name from a user-friendly display name.

    Rules:
    - If multiple words: use first letter of each word
    - If single word: use first 4-5 characters
    - Always append underscore + 3 random characters (a-z, 0-9)
    - Always lowercase
    - Must comply with validation rules (start with letter, max 20 chars)

    Args:
        display_name: User-friendly project name

    Returns:
        Tuple of (technical_name, display_name)

    Examples:
        "My New Application" -> "mna_x7k", "My New Application"
        "WebShop" -> "webs_a9z", "WebShop"
        "API Gateway Service" -> "ags_m2n", "API Gateway Service"
    """
    if not display_name or not display_name.strip():
        raise ValueError("Display name cannot be empty")

    display_name = display_name.strip()

    # Clean the display name: remove special characters, keep letters/numbers/spaces
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", display_name)

    # Split into words
    words = [word for word in cleaned.split() if word]

    if not words:
        raise ValueError("Display name must contain at least one word with letters or numbers")

    # Generate the base name
    if len(words) > 1:
        # Multiple words: use first letter of each word
        base = "".join(word[0].lower() for word in words if word)
        # Ensure we have at least 2 characters for the base
        if len(base) < 2:
            base = words[0][:4].lower()
    else:
        # Single word: use first 4-5 characters
        word = words[0]
        base = word[:5].lower() if len(word) >= 5 else word[:4].lower()

    # Generate postfix: use fixed postfix if set, otherwise random 3 characters
    if settings.FIXED_PROJECT_POSTFIX:
        postfix = settings.FIXED_PROJECT_POSTFIX
    else:
        # Generate 3 random characters (letters and digits)
        postfix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(3))

    # Combine: base + dash + postfix
    technical_name = f"{base}-{postfix}"

    # Ensure it starts with a letter (required by validation)
    if not technical_name[0].isalpha():
        # Prepend a random letter if it doesn't start with one
        technical_name = random.choice(string.ascii_lowercase) + technical_name[1:]

    # Ensure it's not too long (max 20 characters)
    if len(technical_name) > 20:
        # Truncate the base part to fit
        available_chars = 20 - len(postfix) - 1  # -1 for dash, -len(postfix) for postfix
        technical_name = f"{base[:available_chars]}-{postfix}"

    return technical_name, display_name


def validate_generated_name(technical_name: str) -> bool:
    """
    Validate that the generated technical name meets all requirements.

    Args:
        technical_name: The generated technical project name

    Returns:
        True if valid, False otherwise
    """
    if not technical_name:
        return False
    if len(technical_name) > 20:
        return False
    if not technical_name[0].isalpha():
        return False
    if not re.match(r"^[a-z][a-z0-9-]*$", technical_name):
        return False
    return True


def ensure_unique_project_name(display_name: str, existing_names: set[str] | None = None) -> tuple[str, str]:
    """
    Generate a project name and ensure it's unique by regenerating if needed.

    Args:
        display_name: User-friendly display name
        existing_names: Set of existing technical names to avoid

    Returns:
        Tuple of (unique_technical_name, display_name)
    """
    if existing_names is None:
        existing_names = set()

    max_attempts = 10
    for attempt in range(max_attempts):
        technical_name, display = generate_project_name(display_name)

        if technical_name not in existing_names and validate_generated_name(technical_name):
            return technical_name, display

    # If we couldn't generate a unique name, append attempt number
    base_technical, display = generate_project_name(display_name)
    for i in range(1, 100):
        # Replace last character with attempt number
        technical_name = f"{base_technical[:-1]}{i % 10}"
        if technical_name not in existing_names and validate_generated_name(technical_name):
            return technical_name, display

    raise ValueError(f"Could not generate unique project name for '{display_name}' after {max_attempts} attempts")
