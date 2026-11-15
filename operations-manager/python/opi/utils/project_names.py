import random
import re
import string

from opi.core.config import settings
from opi.utils.patterns import PROJECT_NAME, PROJECT_NAME_MAX_LENGTH


def generate_project_name(display_name: str) -> tuple[str, str]:
    """Build a technical project name from a display name, and return it with that name.

    Rules:
    - several words: the first letter of each word
    - one word: its first 4 to 5 characters
    - always a dash plus 3 random characters (a-z, 0-9)
    - always lowercase, starting on a letter, at most ``PROJECT_NAME_MAX_LENGTH`` characters

    Examples:
        "My New Application" -> "mna-x7k"
        "WebShop" -> "webs-a9z"
        "API Gateway Service" -> "ags-m2n"
    """
    if not display_name or not display_name.strip():
        raise ValueError("Display name cannot be empty")

    display_name = display_name.strip()

    cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", display_name)

    words = [word for word in cleaned.split() if word]

    if not words:
        raise ValueError("Display name must contain at least one word with letters or numbers")

    if len(words) > 1:
        base = "".join(word[0].lower() for word in words if word)
        if len(base) < 2:
            base = words[0][:4].lower()
    else:
        word = words[0]
        base = word[:5].lower() if len(word) >= 5 else word[:4].lower()

    if settings.FIXED_PROJECT_POSTFIX:
        postfix = settings.FIXED_PROJECT_POSTFIX
    else:
        postfix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(3))  # noqa: S311  # a postfix for readability, with no security value

    technical_name = f"{base}-{postfix}"

    if not technical_name[0].isalpha():
        technical_name = random.choice(string.ascii_lowercase) + technical_name[1:]  # noqa: S311  # a letter to meet the name shape, with no security value

    if len(technical_name) > PROJECT_NAME_MAX_LENGTH:
        available_chars = PROJECT_NAME_MAX_LENGTH - len(postfix) - 1
        technical_name = f"{base[:available_chars]}-{postfix}"

    return technical_name, display_name


def validate_project_name(name: str) -> bool:
    """Whether this is a name a project may carry: the one rule, read by every entrance."""
    if not name:
        return False
    if len(name) > PROJECT_NAME_MAX_LENGTH:
        return False
    return re.match(PROJECT_NAME, name) is not None


def ensure_unique_project_name(display_name: str, existing_names: set[str] | None = None) -> tuple[str, str]:
    """Build a project name that is none of ``existing_names``, and return it with the name.

    Raises:
        ValueError: when no unique name came out after the regenerations and the numbered
            fallback.
    """
    if existing_names is None:
        existing_names = set()

    max_attempts = 10
    for _attempt in range(max_attempts):
        technical_name, display = generate_project_name(display_name)

        if technical_name not in existing_names and validate_project_name(technical_name):
            return technical_name, display

    # No unique name out of the regenerations: number the last character instead.
    base_technical, display = generate_project_name(display_name)
    for i in range(1, 100):
        technical_name = f"{base_technical[:-1]}{i % 10}"
        if technical_name not in existing_names and validate_project_name(technical_name):
            return technical_name, display

    raise ValueError(f"Could not generate unique project name for '{display_name}' after {max_attempts} attempts")
