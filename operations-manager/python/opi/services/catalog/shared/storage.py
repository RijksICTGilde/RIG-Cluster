"""Config model for the storage services (RC-5 Phase 2).

``persistent-storage`` and ``temp-storage`` share one config shape: a component-level
*list* of mount specs (``[{name, size, mount-path}]``). The ``type``
(persistent/ephemeral) is derived from the service name at read time
(schema_migration._STORAGE_SERVICE_TO_TYPE) and is NOT a user field, so it is not
modelled here. Both services reference this same model.

Storage config is a list, so validation goes through the provider's list-aware
``validate_config``. v1.0 requires the three fields every real storage entry
carries (name/size/mount-path); failing early beats an invalid PVC at render time.

Lives in ``catalog/shared`` because both storage services reference it; it is the
one config model not owned by a single service package.
"""

from __future__ import annotations

import re
from typing import ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationInfo, field_validator

from opi.services.catalog.shared.revisions import CloneState
from opi.services.resource_analyzer import parse_k8s_memory_to_mi

#: Absolute path built from word characters, dots, slashes and dashes.
MOUNT_PATH_PATTERN = re.compile(r"^/[\w./-]+\Z")

#: The volume sizes the platform offers, smallest first. This tuple is the single
#: source: ``StorageSizeOptionsProvider`` builds the form's dropdown from it, and the
#: validator below caps ``size`` at the largest entry. Both storage services share it.
STORAGE_SIZES: Final[tuple[str, ...]] = ("50Mi", "100Mi", "250Mi", "500Mi", "1Gi")

#: The cap, in MiB: the largest size we offer. Everything that writes a mount passes
#: through the model, so this is the one place the ceiling lives.
MAX_STORAGE_MI: Final[float] = max(parse_k8s_memory_to_mi(size) for size in STORAGE_SIZES)

#: The size a mount starts on: what both storage services write when you enable them,
#: what the form prefills, and what the manifest generator falls back to when an entry
#: somehow carries no size at all. The smallest size that means something in practice,
#: and deliberately at the BOTTOM of the range: a volume can grow and cannot shrink, so
#: starting too small is the recoverable mistake and starting too large is not.
DEFAULT_STORAGE_SIZE: Final[str] = "100Mi"

#: Validation-context key that marks data as ALREADY STORED rather than submitted.
#: Set by the whole-file validation gate (``project_validation``), which runs on every
#: reprocess and replay of an existing project file -- see ``check_storage_size``.
STORED_CONTEXT_KEY: Final[str] = "stored_project_data"


def check_storage_size(value: str) -> str:
    """The one storage-size rule: a parseable quantity, at most the largest size offered.

    Shared by the config model (which types the generated API request bodies, so this
    runs on every API write) and by the size editable of both storage services (so the
    forms refuse it too, dropdown or not). One function, so the ceiling cannot mean two
    different things on two paths.

    The ceiling is the largest entry of ``STORAGE_SIZES`` rather than a number of its
    own: raising what the platform offers raises what it accepts, in one edit.

    Raises:
        ValueError: with a message naming the maximum and the available sizes.
    """
    try:
        size_mi = parse_k8s_memory_to_mi(value)
    except ValueError:
        raise ValueError(
            f"Ongeldige opslaggrootte '{value}': gebruik een Kubernetes-hoeveelheid zoals 500Mi of 1Gi"
        ) from None
    if size_mi <= 0:
        raise ValueError(f"Opslaggrootte '{value}' moet groter zijn dan nul")
    if size_mi > MAX_STORAGE_MI:
        raise ValueError(
            f"Opslaggrootte '{value}' is groter dan het maximum van {STORAGE_SIZES[-1]}. "
            f"Beschikbare maten: {', '.join(STORAGE_SIZES)}"
        )
    return value


class StorageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(description="Name of this mount within the component; also names the volume.")
    size: str = Field(
        description=(
            "Size of the volume as a Kubernetes quantity, e.g. 500Mi. "
            f"At most {STORAGE_SIZES[-1]}; the platform offers {', '.join(STORAGE_SIZES)}."
        )
    )
    mount_path: str = Field(
        alias="mount-path",
        description="Absolute path in the container to mount it at. No '..' anywhere in the path.",
    )

    @field_validator("mount_path")
    @classmethod
    def _reject_traversal(cls, value: str) -> str:
        """Absolute path, no ``..`` anywhere.

        This guard used to live in the JSON schema, on the v1 ``storage:`` block
        ($defs/storage-entry) -- which meant it only ever applied to v1 files, never
        to the current service-config shape. Removing that def with the rest of the
        v1 forms (RC-32) made the gap visible; the guard belongs here, on the model
        that describes the shape people actually write. Container-side a ``..`` can
        escape the intended storage root if any tool resolves the path.

        Pydantic's ``pattern=`` cannot express this (its regex engine has no
        look-ahead), hence a validator.
        """
        if not MOUNT_PATH_PATTERN.match(value) or ".." in value:
            raise ValueError(f"Ongeldig mount-pad '{value}': moet absoluut zijn en mag geen '..' bevatten")
        return value

    @field_validator("size")
    @classmethod
    def _within_offered_sizes(cls, value: str, info: ValidationInfo) -> str:
        """Cap the size of a mount that is being SUBMITTED; leave stored data alone.

        Until now the only brake was the form's dropdown, and a dropdown is not a rule:
        the size editable carries a ``values_provider`` and no validator, the JSON schema
        typed ``size`` as a bare string, and the config API was not checked at all. A
        ``10Gi`` mount went straight through to a PVC.

        This model types the generated config endpoints' request bodies, so the check
        lands on the API write path where it belongs: at the boundary, as a 422, before
        anything is stored.

        What it deliberately does NOT do is judge a project file that already exists.
        ``project_validation`` runs this same model over the whole file on every save and
        every reprocess, so a ceiling applied there would turn an older project with a
        larger mount into a file that can no longer be saved -- and a PVC cannot shrink,
        so its owner could not even comply. That is the ``dp-bn7`` fault: a validation
        gap that silently stalls every deploy of one project. The whole-file gate
        therefore passes ``STORED_CONTEXT_KEY`` and only the shape is checked there.
        """
        if info.context and info.context.get(STORED_CONTEXT_KEY):
            return value
        return check_storage_size(value)


class StorageConfig(RootModel[list[StorageEntry]]):
    """The component-level storage config: a list of mount specs."""

    #: The field that identifies one entry in the list -- the PATCH config endpoint
    #: adds/removes/updates entries by this key (the PUT replaces the whole list).
    ITEM_KEY: ClassVar[str] = "name"

    root: list[StorageEntry] = Field(
        default_factory=list, description="The volumes this component mounts, one entry per mount."
    )


class StorageCloneState(CloneState):
    """Per-mount clone state, carried on the deployment-component layer.

    A deployment records which PVC generation each mount points at, as a list of
    ``{reference, config}`` items keyed by the mount name. The content is the shared clone
    state, so this only exists to give the storage services something to return from
    ``config_model_for(DEPLOYMENT_COMPONENT)``; their ``config_model`` describes the mount
    specs on the component layer and cannot describe both.
    """
