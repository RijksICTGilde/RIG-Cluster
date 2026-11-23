"""Config model for the storage services.

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
from typing import Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationInfo, field_validator

from opi.services.catalog.shared.revisions import CloneState
from opi.services.config_managed import PLATFORM_MANAGED
from opi.utils.k8s_quantity import parse_k8s_memory_to_mi

#: Absolute path built from word characters, dots, slashes and dashes.
MOUNT_PATH_PATTERN = re.compile(r"^/[\w./-]+\Z")

#: The volume sizes the platform offers, smallest first. This tuple is the single
#: source: ``StorageSizeOptionsProvider`` builds the form's dropdown from it. It is NOT
#: the range: the editables and the API take every quantity between ``MIN_STORAGE_SIZE``
#: and ``MAX_STORAGE_SIZE``, and the select shows a stored value outside the series as an
#: own value. Both storage services share it.
STORAGE_SIZES: Final[tuple[str, ...]] = ("100Mi", "250Mi", "500Mi", "1Gi", "2Gi", "5Gi", "10Gi", "20Gi", "50Gi")

#: The smallest and largest volume a project may ask for. The series above is a
#: convenience; these two are the rule, so ``3Gi`` and ``750Mi`` are accepted as well.
MIN_STORAGE_SIZE: Final[str] = "50Mi"
MAX_STORAGE_SIZE: Final[str] = "50Gi"

#: Up to and including this size a project chooses for itself; above it a platform
#: administrator decides, and until then the volume renders on what was already allowed.
#: See ``catalog/shared/storage_size.py`` for the rules that hang on it.
FREE_MAX_STORAGE_SIZE: Final[str] = "2Gi"

#: The bounds in MiB, so ``check_storage_size`` compares quantities rather than text.
MIN_STORAGE_MI: Final[float] = parse_k8s_memory_to_mi(MIN_STORAGE_SIZE)
MAX_STORAGE_MI: Final[float] = parse_k8s_memory_to_mi(MAX_STORAGE_SIZE)
FREE_MAX_STORAGE_MI: Final[float] = parse_k8s_memory_to_mi(FREE_MAX_STORAGE_SIZE)

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
    """The one storage-size rule: a parseable quantity between the two bounds.

    Shared by the config model (which types the generated API request bodies, so this
    runs on every API write) and by the size editables of both storage services (so the
    forms refuse it too, dropdown or not). One function, so the bounds cannot mean two
    different things on two paths.

    Membership of ``STORAGE_SIZES`` is deliberately NOT the rule: the series is what the
    form offers, the range is what the platform accepts.

    Raises:
        ValueError: with a message naming the minimum and the maximum.
    """
    try:
        size_mi = parse_k8s_memory_to_mi(value)
    except ValueError:
        raise ValueError(
            f"Ongeldige opslaggrootte '{value}': gebruik een Kubernetes-hoeveelheid zoals 500Mi of 1Gi"
        ) from None
    if size_mi < MIN_STORAGE_MI or size_mi > MAX_STORAGE_MI:
        raise ValueError(
            f"Opslaggrootte '{value}' valt buiten het bereik van {MIN_STORAGE_SIZE} tot en met {MAX_STORAGE_SIZE}"
        )
    return value


class StorageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(description="Name of this mount within the component; also names the volume.")
    size: str = Field(
        description=(
            "Size of the volume as a Kubernetes quantity, e.g. 500Mi. "
            f"From {MIN_STORAGE_SIZE} up to and including {MAX_STORAGE_SIZE}. "
            f"Up to and including {FREE_MAX_STORAGE_SIZE} applies at once; above it a platform "
            "administrator approves a ceiling for the service, which then covers every volume "
            "of it, and until then the volume keeps the size it was already allowed."
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

        Container-side a ``..`` can escape the intended storage root if any tool resolves the
        path, and this model is what describes the shape people actually write.

        Pydantic's ``pattern=`` cannot express this (its regex engine has no look-ahead),
        hence a validator.
        """
        if not MOUNT_PATH_PATTERN.match(value) or ".." in value:
            raise ValueError(f"Ongeldig mount-pad '{value}': moet absoluut zijn en mag geen '..' bevatten")
        return value

    @field_validator("size")
    @classmethod
    def _within_offered_sizes(cls, value: str, info: ValidationInfo) -> str:
        """Cap the size of a mount that is being SUBMITTED; leave stored data alone.

        A dropdown is not a rule: the size editable carries a ``values_provider`` and no
        validator, and the JSON schema types ``size`` as a bare string. This model types the
        generated config endpoints' request bodies, so the check lands on the API write path
        where it belongs: at the boundary, as a 422, before anything is stored.

        What it deliberately does NOT do is judge a project file that already exists.
        ``project_validation`` runs this same model over the whole file on every save and
        every reprocess, so a ceiling applied there would turn an older project with a
        larger mount into a file that can no longer be saved -- and a PVC cannot shrink,
        so its owner could not even comply, which silently stalls every deploy of that
        project. The whole-file gate therefore passes ``STORED_CONTEXT_KEY`` and only the
        shape is checked there.
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


#: Key of the per-volume size on both the component entry and the deployment override, and
#: so also the stem of the approval blocks (``size-approval``).
SIZE_FIELD: Final[str] = "size"


class StorageApprovalConfig(BaseModel):
    """The project-layer config of both storage services: the size ceiling, nothing else.

    The mounts live on the component; the project layer carries only the verdict on the
    largest volume this service may have in this project. A model of its own rather than
    ``StorageConfig`` (the mount list), because a project that could write its own verdict
    has no approval: the field is ``PLATFORM_MANAGED``, and that marking only covers a
    top-level field of a config block.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    size_approval: dict[str, Any] | None = Field(
        default=None,
        alias=f"{SIZE_FIELD}-approval",
        description=(
            "Approval state of the largest volume this service may have in this project. Written by the platform."
        ),
        json_schema_extra={PLATFORM_MANAGED: True},
    )


class StorageDeploymentApprovalConfig(BaseModel):
    """The deployment-layer config of both storage services: this deployment's own ceiling.

    A deployment that needs larger volumes than the rest of the project asks for a ceiling of
    its own, and that verdict is this deployment's alone, so it keeps standing when the
    project's ceiling goes down later. Nothing else lives here: the override itself is a
    ``size`` on the volume's record under the component.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    size_approval: dict[str, Any] | None = Field(
        default=None,
        alias=f"{SIZE_FIELD}-approval",
        description=(
            "Approval state of the largest volume this deployment may have, above the project's "
            "ceiling. Written by the platform."
        ),
        json_schema_extra={PLATFORM_MANAGED: True},
    )


class StorageCloneState(CloneState):
    """Per-mount state a DEPLOYMENT carries, as a list of ``{reference, config}`` items.

    Two things live here: the PVC generation each mount points at (the shared clone state),
    and this deployment's own size for that mount. A deployment that sets no ``size`` follows
    the component. The verdict is not here: it is one ceiling per deployment, on the
    deployment layer.
    """

    size: str | None = Field(
        default=None,
        description=(
            "This deployment's own size for this volume, overriding the component's. "
            f"Same range as the component ({MIN_STORAGE_SIZE} up to and including {MAX_STORAGE_SIZE}); "
            f"above the project's ceiling (at least {FREE_MAX_STORAGE_SIZE}) this deployment needs a "
            "ceiling of its own. Absent means: follow the component."
        ),
    )

    @field_validator("size")
    @classmethod
    def _within_range(cls, value: str | None, info: ValidationInfo) -> str | None:
        """The same range as on the component, and the same exemption for stored data.

        See ``StorageEntry._within_offered_sizes``: a PVC cannot shrink, so a file that
        already carries a larger override has to stay saveable.
        """
        if value is None or (info.context and info.context.get(STORED_CONTEXT_KEY)):
            return value
        return check_storage_size(value)


class StorageSizeOverride(BaseModel):
    """What a caller may set on one volume of one component inside one deployment: the size.

    The body of the deployment-component config route, and deliberately not
    ``StorageCloneState``: that model also describes the PVC generation, its revisions and
    the verdict on the size, and none of those are a caller's to write. The shape IS the
    stored one, so what you send is what the file reads back.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    size: str | None = Field(
        default=None,
        description=(
            "This deployment's own size for this volume. Leave it out to drop the override and "
            "follow the component again."
        ),
    )

    @field_validator("size")
    @classmethod
    def _within_range(cls, value: str | None) -> str | None:
        return None if value is None else check_storage_size(value)


class StorageSizeOverrideEntry(BaseModel):
    """One volume's override, keyed by the name the component mounts it under."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    reference: str = Field(description="The volume as the component mounts it, named after its mount path.")
    config: StorageSizeOverride = Field(
        default_factory=StorageSizeOverride, description="What this deployment sets for that volume."
    )


class StorageSizeOverrides(RootModel[list[StorageSizeOverrideEntry]]):
    """The overrides this deployment sets on one component, one entry per volume.

    Entries are merged per ``reference`` rather than replacing the list: the platform keeps
    its own state in those same records, and a volume this write leaves out keeps what it had.
    """

    ITEM_KEY: ClassVar[str] = "reference"

    root: list[StorageSizeOverrideEntry] = Field(default_factory=list)
