"""Which lists inside a service config a PATCH addresses entry by entry.

A service whose config IS a list (storage mounts, attachment couplings) has had a PATCH
next to its PUT since RC: vraag 18: ``ITEM_KEY`` on the ``RootModel`` names the field
that identifies one entry, and the generated route lets a caller add or remove one
without resending the rest.

A service whose config is an OBJECT with a list *inside* it had only the PUT --
``invite.active``, ``cross-domain-access.inbound``/``outbound`` and ``sleep-mode.match``.
A PUT writes the whole block, so putting one entry in means resending every entry that
is already there, and whoever did not know that wiped them. That happened: a project
lost its invites, and with the invite key deliberately absent from every read response
(it is the secret in the link) there was no way to resend the first invite at all, so a
second one could not be added without losing the first.

Such a model declares ``ITEM_KEYS``: per list, the field that identifies one entry, or
``None`` when the entries are plain strings and the value IS its own identity
(``sleep-mode.match`` holds glob patterns, which have no key to hang an identity on --
adding "the same pattern twice" is meaningless, so the pattern is the key and add is a
set union).

Both declarations are read here and produce the same ``PatchableList``, so the route
generator (``opi/api/v2/router.py``) and the merge
(``ServiceAdapter.patch_service_config_list``) keep ONE add/remove mechanism instead of
growing a second one for objects-with-lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, get_args, get_origin

from pydantic import BaseModel, RootModel


@dataclass(frozen=True)
class PatchableList:
    """One list in a service config that can be patched entry by entry."""

    #: The config key holding the list, or ``None`` when the config itself IS the list.
    name: str | None
    #: The field identifying one entry, or ``None`` when an entry is its own identity.
    item_key: str | None
    #: The type of one entry: a model, or ``str`` for a list of plain values.
    item_type: Any

    @property
    def item_model(self) -> type[BaseModel] | None:
        """The entry model, or ``None`` when the entries are plain values."""
        if isinstance(self.item_type, type) and issubclass(self.item_type, BaseModel):
            return self.item_type
        return None

    @property
    def path_suffix(self) -> str:
        """What this list adds to the service's config path: nothing for a root list,
        so the existing storage/attachments routes keep their address."""
        return f"/{self.name}" if self.name else ""


def list_item_type(annotation: Any) -> Any:
    """The entry type of a ``list[...]`` annotation, or ``None`` for anything else."""
    if get_origin(annotation) is not list:
        return None
    args = get_args(annotation)
    return args[0] if len(args) == 1 else None


def _root_list(config_model: type[BaseModel]) -> list[PatchableList]:
    """The single patchable list of a ``RootModel[list[Item]]`` config, if it declares one."""
    item_key = getattr(config_model, "ITEM_KEY", None)
    if not isinstance(item_key, str):
        return []
    item_type = list_item_type(config_model.model_fields["root"].annotation)
    if not (isinstance(item_type, type) and issubclass(item_type, BaseModel)):
        return []
    return [PatchableList(name=None, item_key=item_key, item_type=item_type)]


def patchable_lists(config_model: type | None) -> list[PatchableList]:
    """The lists in this config model a PATCH can address, in declaration order.

    Empty for a config with nothing list-shaped to patch, which is most of the catalog.
    A declaration that does not match the model raises: a typo in ``ITEM_KEYS`` would
    otherwise silently generate no route at all, which is the failure this whole
    mechanism exists to prevent.
    """
    if not (isinstance(config_model, type) and issubclass(config_model, BaseModel)):
        return []
    if issubclass(config_model, RootModel):
        return _root_list(config_model)

    declared = getattr(config_model, "ITEM_KEYS", None)
    if not isinstance(declared, dict):
        return []

    fields_by_name = {(field.alias or name): field for name, field in config_model.model_fields.items()}
    lists: list[PatchableList] = []
    for name, item_key in declared.items():
        field = fields_by_name.get(name)
        if field is None:
            raise ValueError(f"{config_model.__name__}.ITEM_KEYS names '{name}', which is not a field of the model")
        item_type = list_item_type(field.annotation)
        if item_type is None:
            raise ValueError(f"{config_model.__name__}.{name} is not a list, so it cannot be patched entry by entry")
        is_model = isinstance(item_type, type) and issubclass(item_type, BaseModel)
        if item_key is None and is_model:
            raise ValueError(
                f"{config_model.__name__}.{name} holds records, so ITEM_KEYS must name the field that identifies one"
            )
        if item_key is not None and not is_model:
            raise ValueError(
                f"{config_model.__name__}.{name} holds plain values, so ITEM_KEYS must map it to None: the value "
                "is its own identity"
            )
        lists.append(PatchableList(name=name, item_key=item_key, item_type=item_type))
    return lists


def find_patchable_list(config_model: type | None, name: str | None) -> PatchableList | None:
    """The patchable list called ``name`` (``None`` = the root list), or ``None``."""
    for candidate in patchable_lists(config_model):
        if candidate.name == name:
            return candidate
    return None
