"""De grens op de oude versies in een bucket met versioning (RC-380).

Een bucket met versioning houdt zonder regel elke oude versie van elk object, dus een
applicatie die per bewerking een versie wegschrijft laat hem zonder grens groeien. De
grens is een platformbesluit met ruimte per project en per deployment, en staat daarom
hier en niet in het projectbestand. Een bucket hoort bij een deployment, dus de
deploymentwaarde is wat er op de bucket komt.

Beide object-opslagdiensten dragen dezelfde grens op dezelfde twee lagen, dus de
declaratie staat hier en niet twee keer: ``minio-storage`` en ``seaweedfs-storage`` geven
hun eigen ``ServiceType`` mee aan het oplossen.
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

    from opi.services.services_enums import ServiceType

MAX_NONCURRENT_VERSIONS: Final = IntegerSetting(
    path="max-noncurrent-versions",
    layers=(ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT),
    default=30,
    minimum=1,
    maximum=1000,
    label="Aantal oude versies",
)

#: Geen standaard: een leeftijdsgrens is een keuze per bucket, en in een reeks die bij 1
#: begint is er geen getal dat "geen grens" betekent. Zie ``default=None`` in
#: ``config_settings``.
NONCURRENT_EXPIRE_DAYS: Final = IntegerSetting(
    path="noncurrent-expire-days",
    layers=(ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT),
    default=None,
    minimum=1,
    maximum=3650,
    label="Leeftijd oude versies",
)

#: De stappen die de wizard aanbiedt. Een gemak, geen grens: elke waarde binnen de
#: declaratie is geldig, en een opgeslagen waarde buiten deze lijst blijft zichtbaar.
MAX_NONCURRENT_VERSIONS_STEPS: Final[tuple[int, ...]] = (5, 10, 20, 30, 50, 100, 250, 1000)
NONCURRENT_EXPIRE_DAYS_STEPS: Final[tuple[int, ...]] = (7, 30, 90, 180, 365, 730, 3650)

#: Wat een dienst die deze grens draagt declareert.
NONCURRENT_VERSION_SETTINGS: Final[tuple[IntegerSetting, ...]] = (MAX_NONCURRENT_VERSIONS, NONCURRENT_EXPIRE_DAYS)


@dataclass(frozen=True)
class NoncurrentVersionRule:
    """De effectieve grens voor een bucket: hoeveel oude versies, en hoe lang."""

    max_versions: int
    expire_days: int | None


def noncurrent_config_field(setting: IntegerSetting, description: str) -> Any:
    """Het pydantic-veld van een van deze twee instellingen, voor het config-model.

    De grenzen worden geoordeeld door de declaratie (``check_settings``) en niet door
    ``ge``/``le`` hier; het model noemt ze alleen in zijn beschrijving, zodat ze op een
    plek staan.
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
) -> Any:
    values = {
        layer: service_setting_value(entries, service, setting)
        for layer, entries in _layer_values(project_data, deployment_name).items()
    }
    return resolve_setting(setting, values)


def project_max_noncurrent_versions(project_data: dict[str, Any], service: ServiceType) -> int:
    """Wat een deployment zonder eigen waarde krijgt: die van het project, anders de standaard."""
    return _resolve(MAX_NONCURRENT_VERSIONS, project_data, None, service)


def project_noncurrent_expire_days(project_data: dict[str, Any], service: ServiceType) -> int | None:
    return _resolve(NONCURRENT_EXPIRE_DAYS, project_data, None, service)


def deployment_noncurrent_rule(
    project_data: dict[str, Any], deployment_name: str, service: ServiceType
) -> NoncurrentVersionRule:
    """De grens die op de bucket van ``deployment_name`` komt: deployment boven project boven standaard."""
    return NoncurrentVersionRule(
        max_versions=_resolve(MAX_NONCURRENT_VERSIONS, project_data, deployment_name, service),
        expire_days=_resolve(NONCURRENT_EXPIRE_DAYS, project_data, deployment_name, service),
    )


def noncurrent_deployment_section(section_prefix: str, fields: Sequence[Any], deployment_index: int | None) -> Any:
    """De sectie van de deploymentmodal: de grens op de bucket van een deployment.

    Zonder index beschrijft hij de laag (de velden dragen dan hun wildcard-pad), met index
    staat hij in de modal van dat deployment. Beide diensten die deze grens dragen krijgen
    dezelfde sectie; alleen hun eigen velden en sectie-id verschillen. De imports staan in
    de functie en niet bovenaan, zoals overal waar de catalogus het formulier aanroept.
    """
    from opi.forms.editables.reindex import materialize_wildcard_visualizer
    from opi.forms.visualizers.sections import FormSection

    index = 0 if deployment_index is None else deployment_index
    gematerialiseerd = [materialize_wildcard_visualizer(field, index) for field in fields]
    suffix = "" if deployment_index is None else f"-{deployment_index}"
    return FormSection(
        section_id=f"{section_prefix}{suffix}",
        title="Oude versies van deze bucket",
        icon="wolk",
        description="Geldt alleen voor een bucket met versiebeheer.",
        post_save_action="process_project",
        editables=gematerialiseerd,
        layout=[field.editable.yaml_path for field in gematerialiseerd],
    )
