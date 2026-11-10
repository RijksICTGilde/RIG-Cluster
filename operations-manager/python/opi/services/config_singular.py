"""A list in a service config that the API presents as ONE object.

A TEMPORARY facade: read this before building on it.

``invite.active`` is the case. A project has exactly one today, and the form pins that down
(``INVITE_ACTIVE_EDITABLE``: min == max == 1, no add or remove button). The API therefore
presents the singular, while STORAGE stays a list everywhere: in the project file, in the
config model, in ``project_v2.json`` and in the recorded schema fragment. There is no
migration and no schema change, so taking the name out of ``Service.api_singular_lists``
brings the list surface back without touching anything on disk.

THE FACADE MAY ONLY EXIST WHILE IT IS TRUE. With two or more entries in the file, by hand
or through the PATCH route, this layer must NOT show the first and overwrite the rest: for
an invite that loss is unrecoverable, because the project file is the only place that
invitation lives and an overwritten invitation link is invalid at once for everyone who
already had it.

``overflowing_list`` is that detection. Its callers (the read route and the PUT in
``opi/api/v2/router.py``) refuse on it with a 409 that names the way that does work: the
PATCH on the list, which handles entries one at a time.

THE PATCH THEREFORE MAY NOT ADD A SECOND ONE. The limit sits with the action that creates
the second entry: ``ServiceAdapter.patch_service_config_list`` refuses an ADDITION that
takes the list above one. Removing stays allowed, which is why a file that already holds
more does not lock up: take one out and the facade is true again. Replacing is allowed too,
because it does not change the count.

The DELETE falls outside this on purpose. It says "wipe this config block" and does that,
facade or no facade; it does not pretend there is one entry. Refusing it as well would
leave a project with several invites no way to a clean slate.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, create_model

from opi.services.config_lists import list_item_type

#: Marker in ``/openapi.json`` on a field that is singular here and a list in the project
#: file, so a client sees the deviation in the document instead of discovering it.
SINGULAR_MARKER = "x-api-singular"

#: Built facade models, so two routes of the same service share ONE schema in the OpenAPI
#: document instead of two with the same name.
_MODELS: dict[tuple[type[BaseModel], frozenset[str]], type[BaseModel]] = {}


def _fields_by_key(config_model: type[BaseModel]) -> dict[str, tuple[str, Any]]:
    """The fields of *config_model* by their on-disk key: the alias, or else the name."""
    return {(field.alias or name): (name, field) for name, field in config_model.model_fields.items()}


def singular_config_model(config_model: type[BaseModel], names: frozenset[str]) -> type[BaseModel]:
    """*config_model*, with every field in *names* as one entry instead of a list.

    A subclass of the real model, so every other field, its aliases and the ``model_config``
    (``extra="forbid"``, ``populate_by_name``) are the same by definition. A name that is not
    a list field of the model raises rather than quietly doing nothing: a typo would
    otherwise leave the list surface in place unnoticed.
    """
    key = (config_model, names)
    cached = _MODELS.get(key)
    if cached is not None:
        return cached

    by_key = _fields_by_key(config_model)
    overrides: dict[str, Any] = {}
    for on_disk in sorted(names):
        found = by_key.get(on_disk)
        if found is None:
            raise ValueError(
                f"{config_model.__name__} has no field '{on_disk}', so it cannot be presented as a single entry"
            )
        name, field = found
        item = list_item_type(field.annotation)
        if item is None:
            raise ValueError(f"{config_model.__name__}.{on_disk} is not a list, so there is no list character to hide")
        description = " ".join(
            part
            for part in (
                field.description,
                f"Exactly one is accepted here. The project file stores '{on_disk}' as a list; this API presents "
                "that list as a single entry for as long as there is only ever one of them. A file that already "
                "holds more than one is refused rather than overwritten -- use the PATCH route on this list to "
                "manage entries one at a time.",
            )
            if part
        )
        overrides[name] = (
            item | None,
            Field(default=None, alias=field.alias, description=description, json_schema_extra={SINGULAR_MARKER: True}),
        )

    built = create_model(f"{config_model.__name__}Singular", __base__=config_model, **overrides)
    _MODELS[key] = built
    return built


def overflowing_list(config: Any, names: frozenset[str]) -> tuple[str, int] | None:
    """The first list from *names* in *config* with more than one entry, and how many.

    ``None`` when the facade is true: zero or one entry per list, or nothing of this service.
    """
    if not isinstance(config, dict):
        return None
    for on_disk in sorted(names):
        stored = config.get(on_disk)
        if isinstance(stored, list) and len(stored) > 1:
            return on_disk, len(stored)
    return None


def to_singular(config: Any, names: frozenset[str]) -> Any:
    """Stored form -> API form: the list becomes its only entry, or ``None`` when empty.

    Call this only once ``overflowing_list`` found nothing: with more than one entry this
    would hide the rest.
    """
    if not isinstance(config, dict):
        return config
    result = dict(config)
    for on_disk in names:
        stored = result.get(on_disk)
        if isinstance(stored, list):
            result[on_disk] = stored[0] if stored else None
    return result


def to_stored(config: Any, names: frozenset[str]) -> Any:
    """API form -> stored form: the one entry becomes a list of one.

    A field the sender did not name is absent (``exclude_unset``) and stays absent: what they
    did not send, they do not write. An explicit ``null`` means "no entry" and becomes the
    empty list, because storage has no singular.
    """
    if not isinstance(config, dict):
        return config
    result = dict(config)
    for on_disk in names:
        if on_disk not in result:
            continue
        entry = result[on_disk]
        result[on_disk] = [] if entry is None else [entry]
    return result
