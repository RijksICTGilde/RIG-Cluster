"""What a form may and may not write into the project file.

The fields no form may set, checked against the submission. The merge itself is
``apply_write_paths`` (:mod:`opi.forms.wizard.write_set`), and the role gate runs through
``decide()`` in :mod:`opi.http.auth.authz`.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from opi.forms.wizard.save import IMMUTABLE_PROJECT_FIELDS

__all__ = ["IMMUTABLE_PROJECT_FIELDS", "refuse_immutable_changes"]


def refuse_immutable_changes(existing_data: dict[str, Any], submitted_data: dict[str, Any]) -> None:
    """Raise ``HTTPException`` 400 when the submission CHANGES an immutable project field.

    A submission that carries such a field unchanged is allowed: the edit wizard submits its
    whole project, seeded from the stored state, so equal to what is already there counts as
    "not changed". ``name`` is the on-disk filename and ``clusters`` is not editable yet.
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
