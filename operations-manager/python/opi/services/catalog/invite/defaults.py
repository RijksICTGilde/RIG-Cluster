"""Computed defaults for the invite fields.

These fill an invite that nobody has typed into yet. Every one of them is a callable
default (see ``Editable.default``), so it runs only when the field has no stored value
and never overwrites what a user entered.

Why computed and not constant: the useful default depends on the project. The contact
address is one of the project's own people, and the texts read better with the project's
name in them than with a placeholder. Both come straight out of the project data the form
is already working on, so no lookup or connector is involved.

The texts follow the shape of what projects were filling in by hand (see
``tests/fixtures/upgrade_safety/invites-legacy.yaml``): one short welcoming line, not a
letter. They are a starting point that a project is expected to rewrite, which is exactly
why they must not be empty.
"""

from __future__ import annotations

from typing import Any


def _project_label(yaml_data: dict[str, Any]) -> str:
    """The name to show a human: the display name, falling back to the technical name.

    ``display-name`` is optional in the schema while ``name`` is required, and the
    technical name is the lowercase-hyphenated one, so it is the poorer of the two to
    put in a sentence. Falls back to a neutral word rather than an empty string, which
    would otherwise produce "Welkom bij ." in a brand-new project.
    """
    label = yaml_data.get("display-name") or yaml_data.get("name")
    return str(label) if label else "dit project"


def default_contact_email(yaml_data: dict[str, Any]) -> str | None:
    """The first person listed on the project.

    Deliberately the first one and not, say, the first admin: the field is a "who do I
    mail when this does not work" hint that the project is expected to adjust, and
    picking by role would suggest an authority the value does not carry.
    """
    users = yaml_data.get("users")
    if not isinstance(users, list):
        return None
    for user in users:
        if isinstance(user, dict):
            email = user.get("email")
            if email:
                return str(email)
    return None


def default_message_nl(yaml_data: dict[str, Any]) -> str:
    return f"Je bent uitgenodigd voor {_project_label(yaml_data)}. Maak hieronder je account aan om toegang te krijgen."


def default_message_en(yaml_data: dict[str, Any]) -> str:
    return f"You have been invited to {_project_label(yaml_data)}. Create your account below to get access."


def default_success_title_nl(yaml_data: dict[str, Any]) -> str:
    return f"Je account voor {_project_label(yaml_data)} is aangemaakt"


def default_success_title_en(yaml_data: dict[str, Any]) -> str:
    return f"Your account for {_project_label(yaml_data)} has been created"


def default_success_button_nl(_yaml_data: dict[str, Any]) -> str:
    return "Naar de applicatie"


def default_success_button_en(_yaml_data: dict[str, Any]) -> str:
    return "Go to the application"
