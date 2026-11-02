"""Wat een formulier wel en niet in het projectbestand mag schrijven.

De rolpoort is hier weg: die loopt sinds RC-392 via ``decide()``, in
:mod:`opi.web.authz`. Wat hier blijft is de merge van ingediende formuliergegevens op het
opgeslagen project, met de velden die geen formulier mag zetten.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

# Top-level fields no form should ever submit. name=on-disk filename;
# clusters=not yet editable (see features/futures/cluster-editing.md).
IMMUTABLE_PROJECT_FIELDS: tuple[str, ...] = ("name", "clusters")


def apply_form_data_to_project(
    existing_data: dict[str, Any],
    submitted_data: dict[str, Any],
) -> dict[str, Any]:
    """Merge submitted form data into the stored project data. Returns a new dict.

    Raises HTTPException 400 if the submission tries to CHANGE any
    ``IMMUTABLE_PROJECT_FIELDS``. That is a structural-integrity violation:
    no form may set these, so receiving a different value means either a
    form bug or a tampered submission. Fail loud rather than silently
    dropping the value.

    Een veld dat GEMERGDE data meedraagt (de bewerkwizard levert ZIJN hele project -
    de sessie is uit de opgeslagen stand gezaaid) is geen schending: gelijk aan wat er
    al staat is "niet gewijzigd", en de wizard kan daardoor opslaan. Een WIJZIGING blijft
    een weigering.
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
