"""The limit on the noncurrent versions in a bucket with versioning.

Without a rule a bucket with versioning keeps every old version of every object, so an
application that writes a version per edit lets it grow without bound. The limit is a platform
decision with room per project and per deployment, and therefore lives here and not in the
project file. A bucket belongs to a deployment, so the deployment value is what lands on the
bucket.

Both object storage services carry the same limit on the same two layers, so the declaration
lives here and not twice: ``minio-storage`` and ``seaweedfs-storage`` pass their own
``ServiceType`` to the resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from pydantic import Field

from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.config_settings import IntegerSetting, resolve_setting, service_setting_value
from opi.services.services import deployment_index

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opi.forms.visualizers.visualizer import EditableVisualizer
    from opi.services.services_enums import ServiceType

MAX_NONCURRENT_VERSIONS: Final = IntegerSetting(
    path="max-noncurrent-versions",
    layers=(ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT),
    default=30,
    minimum=1,
    maximum=1000,
    label="Aantal oude versies",
)

#: No default: an age limit is a choice per bucket, and in a range that starts at 1 there is no
#: number meaning "no limit". See ``default=None`` in ``config_settings``.
NONCURRENT_EXPIRE_DAYS: Final = IntegerSetting(
    path="noncurrent-expire-days",
    layers=(ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT),
    default=None,
    minimum=1,
    maximum=3650,
    label="Leeftijd oude versies",
)

#: The steps the wizard offers. A convenience, not a bound: every value within the declaration
#: is valid, and a stored value outside this list stays visible.
MAX_NONCURRENT_VERSIONS_STEPS: Final[tuple[int, ...]] = (5, 10, 20, 30, 50, 100, 250, 1000)
NONCURRENT_EXPIRE_DAYS_STEPS: Final[tuple[int, ...]] = (7, 30, 90, 180, 365, 730, 3650)

#: What a service carrying this limit declares.
NONCURRENT_VERSION_SETTINGS: Final[tuple[IntegerSetting, ...]] = (MAX_NONCURRENT_VERSIONS, NONCURRENT_EXPIRE_DAYS)


@dataclass(frozen=True)
class NoncurrentVersionRule:
    """The effective limit for a bucket: how many noncurrent versions, and for how long."""

    max_versions: int
    expire_days: int | None


def noncurrent_config_field(setting: IntegerSetting, description: str) -> Any:  # noqa: ANN401  # pydantic Field() fits any field
    """The pydantic field of one of these two settings, for the config model.

    The bounds are judged by the declaration (``check_settings``) and not by ``ge``/``le``
    here; the model only names them in its description, so they live in one place.
    """
    room = f"Between {setting.minimum} and {setting.maximum}"
    room += f", platform default {setting.default}." if setting.default is not None else ", no platform default."
    return Field(default=None, alias=setting.path, description=f"{description} {room}")


def _layer_values(project_data: dict[str, Any], deployment_name: str | None) -> dict[ConfigLayer, Any]:
    layers: dict[ConfigLayer, Any] = {
        ConfigLayer.PROJECT: project_data.get("services"),
    }
    if deployment_name is not None:
        index = deployment_index(project_data, deployment_name)
        deployment = project_data["deployments"][index] if index is not None else {}
        layers[ConfigLayer.DEPLOYMENT] = deployment.get("services")
    return layers


def _resolve(
    setting: IntegerSetting, project_data: dict[str, Any], deployment_name: str | None, service: ServiceType
) -> Any:  # noqa: ANN401  # the resolved setting value, an int or None
    values = {
        layer: service_setting_value(entries, service, setting)
        for layer, entries in _layer_values(project_data, deployment_name).items()
    }
    return resolve_setting(setting, values)


def project_max_noncurrent_versions(project_data: dict[str, Any], service: ServiceType) -> int:
    """What a deployment without a value of its own gets: the project's, else the default."""
    return _resolve(MAX_NONCURRENT_VERSIONS, project_data, None, service)


def project_noncurrent_expire_days(project_data: dict[str, Any], service: ServiceType) -> int | None:
    return _resolve(NONCURRENT_EXPIRE_DAYS, project_data, None, service)


def deployment_noncurrent_rule(
    project_data: dict[str, Any], deployment_name: str, service: ServiceType
) -> NoncurrentVersionRule:
    """The limit that lands on the bucket of ``deployment_name``: deployment over project over default."""
    return NoncurrentVersionRule(
        max_versions=_resolve(MAX_NONCURRENT_VERSIONS, project_data, deployment_name, service),
        expire_days=_resolve(NONCURRENT_EXPIRE_DAYS, project_data, deployment_name, service),
    )


def noncurrent_deployment_section(
    section_prefix: str, fields: Sequence[EditableVisualizer], deployment_index: int | None
) -> FormSection:
    """The section of the deployment modal: the limit on one deployment's bucket.

    Without an index it describes the layer (the fields then carry their wildcard path), with an
    index it sits in that deployment's modal. Both services carrying this limit get the same
    section; only their own fields and section id differ. The imports sit inside the function
    and not at the top, as everywhere the catalog calls the form.
    """
    from opi.forms.editables.reindex import materialize_wildcard_visualizer
    from opi.forms.visualizers.sections import FormSection

    index = 0 if deployment_index is None else deployment_index
    materialized = [materialize_wildcard_visualizer(field, index) for field in fields]
    suffix = "" if deployment_index is None else f"-{deployment_index}"
    return FormSection(
        section_id=f"{section_prefix}{suffix}",
        title="Oude versies van deze bucket",
        icon="wolk",
        description="Geldt alleen voor een bucket met versiebeheer.",
        post_save_action="process_project",
        editables=materialized,
        layout=[field.editable.yaml_path for field in materialized],
    )
