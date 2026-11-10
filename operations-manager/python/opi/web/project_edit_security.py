"""What a form may and may not write into the project file.

Merging submitted form data onto the stored project, with the fields no form may set. The role
gate is not here: it runs through ``decide()`` in :mod:`opi.http.auth.authz`.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from opi.forms.wizard.save import IMMUTABLE_PROJECT_FIELDS

__all__ = ["IMMUTABLE_PROJECT_FIELDS", "apply_form_data_to_project"]


def apply_form_data_to_project(
    existing_data: dict[str, Any],
    submitted_data: dict[str, Any],
) -> dict[str, Any]:
    """Merge submitted form data onto the stored project data; returns a new dict.

    Raises ``HTTPException`` 400 when the submission CHANGES an ``IMMUTABLE_PROJECT_FIELDS``
    entry. A submission that carries such a field unchanged is allowed: the edit wizard submits
    its whole project, seeded from the stored state, so equal to what is already there counts
    as "not changed".
    """
    offending = [
        field
        for field in IMMUTABLE_PROJECT_FIELDS
        if field in submitted_data and submitted_data[field] != existing_data.get(field)
    ]
    if offending:
        raise HTTPException(
            status_code=400,
            detail=f"Submitted form data contains immutable project fields: {', '.join(offending)}",
        )
    return {**existing_data, **submitted_data}
