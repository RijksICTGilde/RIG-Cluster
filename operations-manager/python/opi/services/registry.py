"""Service registry -- assembles the per-service modules into ``SERVICES``.

``SERVICES`` maps every ``ServiceType`` to its ``Service`` instance -- the single
place generic code looks up service behaviour. Each service lives in its own module
under ``opi.services.catalog``; adding one means adding a module there plus one line
here. The coverage guard (``tests/test_service_providers.py``) fails CI if a
``ServiceType`` has no service, which keeps this the single source of truth.

A *service* is a user-facing configuration-as-code building block (keycloak,
postgresql-database, ...), NOT a connector/provider ("how OPI talks to a system").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.services.catalog.attachments import AttachmentsService
from opi.services.catalog.authorization_wall import AuthorizationWallService
from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.health_check import HealthCheckService
from opi.services.catalog.invite import InviteService
from opi.services.catalog.keycloak import KeycloakService
from opi.services.catalog.metrics_scraper import MetricsScraperService
from opi.services.catalog.minio import MinioStorageService
from opi.services.catalog.namespace_postgres import NamespacePostgresqlDatabaseService
from opi.services.catalog.namespace_redis import NamespaceRedisService
from opi.services.catalog.persistent_storage import PersistentStorageService
from opi.services.catalog.platform import PlatformService
from opi.services.catalog.postgresql_database import PostgresqlDatabaseService
from opi.services.catalog.publish_on_web import PublishOnWebService
from opi.services.catalog.redis import RedisService
from opi.services.catalog.resource_tuning import ResourceTuningService
from opi.services.catalog.sleep_mode import SleepModeService
from opi.services.catalog.temp_storage import TempStorageService
from opi.services.services_enums import HookPoint, ServiceType

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.visualizer import EditableVisualizer

# One entry per ServiceType. The coverage guard asserts completeness.
SERVICES: dict[ServiceType, Service] = {
    ServiceType.PUBLISH_ON_WEB: PublishOnWebService(),
    ServiceType.KEYCLOAK: KeycloakService(),
    ServiceType.AUTHORIZATION_WALL: AuthorizationWallService(),
    ServiceType.METRICS_SCRAPER: MetricsScraperService(),
    ServiceType.HEALTH_CHECK: HealthCheckService(),
    ServiceType.PERSISTENT_STORAGE: PersistentStorageService(),
    ServiceType.TEMP_STORAGE: TempStorageService(),
    ServiceType.POSTGRESQL_DATABASE: PostgresqlDatabaseService(),
    ServiceType.NAMESPACE_POSTGRESQL_DATABASE: NamespacePostgresqlDatabaseService(),
    ServiceType.MINIO_STORAGE: MinioStorageService(),
    ServiceType.REDIS: RedisService(),
    ServiceType.NAMESPACE_REDIS: NamespaceRedisService(),
    ServiceType.PLATFORM: PlatformService(),
    ServiceType.ATTACHMENTS: AttachmentsService(),
    ServiceType.SLEEP_MODE: SleepModeService(),
    ServiceType.INVITE: InviteService(),
    ServiceType.RESOURCE_TUNING: ResourceTuningService(),
}


def get_service(service_type: ServiceType) -> Service:
    """Return the service for a service type.

    Raises ``KeyError`` if none is registered -- the coverage guard prevents that
    from happening for any ``ServiceType``.
    """
    return SERVICES[service_type]


def provisioning_services() -> list[Service]:
    """Services that provision deployment resources, in ``provision_order`` (RC-5
    Phase 4). Only services that override ``provision`` are included; the order
    reproduces today's fixed db -> minio -> keycloak -> redis sequence.
    """
    overriding = [s for s in SERVICES.values() if type(s).provision is not Service.provision]
    return sorted(overriding, key=lambda s: s.provision_order)


#: The default (no-op) method backing each hook point. A service participates at a hook
#: exactly when it overrides that method, so participation is derived, never a
#: separately-declared list that could drift from the implementation.
_HOOK_DEFAULTS: dict[HookPoint, Any] = {
    HookPoint.AFTER_SYNC: Service.observe_deployment,
}


def services_for_hook(hook: HookPoint) -> list[Service]:
    """Services participating at ``hook``, in their order for that point (task 8).

    Same override-detection pattern as ``provisioning_services()``, keyed by the hook's
    enum: a service is in only if it overrides the hook's default method. Order comes
    from ``hook_order[hook]`` (default 100), per-hook so a service on two points does
    not share one order.
    """
    default = _HOOK_DEFAULTS[hook]
    overriding = [s for s in SERVICES.values() if getattr(type(s), default.__name__) is not default]
    return sorted(overriding, key=lambda s: s.hook_order.get(hook, 100))


def manifest_secret_services() -> list[Service]:
    """Services that contribute a per-deployment ``envFrom`` secret, in
    ``manifest_order`` (RC-5 Phase 6a). The order reproduces today's fixed envFrom
    append sequence (db -> minio -> keycloak -> redis -> metrics), so the generic
    component loop stays byte-identical.
    """
    contributing = [s for s in SERVICES.values() if s.manifest_secret_class is not None]
    return sorted(contributing, key=lambda s: s.manifest_order)


def approval_services() -> list[Service]:
    """Services that declare at least one ApprovalSpec, in registry order (RC-5).

    The generic approver interface iterates these to list pending items + record
    verdicts, instead of hard-coding one subsystem (domains) per approvable thing.
    """
    return [s for s in SERVICES.values() if s.approval_specs()]


def manifest_services() -> list[Service]:
    """All services that contribute to a component's manifests, in ``manifest_order``
    (RC-5 Phase 6). Superset of ``manifest_secret_services()`` -- also includes
    override services (auth-wall). The generic component loop calls each once and
    applies its ``ManifestContribution`` (additive env_from/sidecars, override
    template_vars).
    """
    contributing = [s for s in SERVICES.values() if type(s).contributes_to_manifests()]
    return sorted(contributing, key=lambda s: s.manifest_order)


def collect_deployment_actions(project_data: dict, deployment_name: str) -> list:
    """All deployment-level action buttons the project's services contribute.

    Iterates every service whose ``ServiceDefinition`` declares an ``actions_provider``
    and flattens their visible ``DeploymentAction``s, so the deployment-actions template
    loops over data instead of hardcoding per-service conditions.
    """
    actions: list = []
    for service in SERVICES.values():
        provider = service.definition.actions_provider
        if provider is None:
            continue
        actions.extend(action for action in provider(project_data, deployment_name) if action.visible)
    return actions


def collect_detail_page_sections(project_data: dict, user_role: str) -> list:
    """Read-only detail-page sections contributed by the services a project uses (WP2).

    Only services the project actually selects (project-level or referenced by a
    component) contribute, in registry order, so the project-details template loops
    over data instead of hardcoding a per-service ``{% include %}``. ``project_data``
    must be the DECRYPTED project dict (a service may surface managed credentials).
    """
    from opi.services.catalog.base import DetailPageSection
    from opi.services.services import service_entry_name

    selected: set[str | None] = {service_entry_name(entry) for entry in project_data.get("services", []) or []}
    for component in project_data.get("components", []) or []:
        for entry in component.get("services", []) or []:
            selected.add(service_entry_name(entry))

    sections: list[DetailPageSection] = []
    for service in SERVICES.values():
        if service.service_type.value not in selected:
            continue
        sections.extend(service.detail_page_sections(project_data, user_role))
    return sections


def component_service_editables() -> list[Editable]:
    """Component-level editables every service contributes to the component form,
    flattened in ``config_component_order`` (RC-5). This replaces the hand-synced tail
    of ``COMPONENTS_SEQUENCE_EDITABLE`` so each service owns its own component fields;
    services that contribute nothing at the component layer add nothing.
    """
    editables: list[Editable] = []
    for service in sorted(SERVICES.values(), key=lambda s: s.config_component_order):
        editables.extend(service.config_editables(ConfigLayer.COMPONENT))
    return editables


def component_service_visualizers() -> list[EditableVisualizer]:
    """As ``component_service_editables``, for the component-form visualizers
    (the tail of ``COMPONENTS_SEQUENCE``)."""
    visualizers: list[EditableVisualizer] = []
    for service in sorted(SERVICES.values(), key=lambda s: s.config_component_order):
        visualizers.extend(service.config_component_visualizers())
    return visualizers
