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

import re
from typing import TYPE_CHECKING, Any

from opi.services.catalog.aliases import AliasesService
from opi.services.catalog.attachments import AttachmentsService
from opi.services.catalog.authorization_wall import AuthorizationWallService
from opi.services.catalog.base import ConfigLayer, DeploymentPageContext, Service
from opi.services.catalog.cross_domain_access import CrossDomainAccessService
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
from opi.services.catalog.user_env_vars import UserEnvVarsService
from opi.services.services_enums import HookPoint, ServiceType

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.visualizer import EditableVisualizer
    from opi.services.services import DeploymentAction

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
    ServiceType.CROSS_DOMAIN_ACCESS: CrossDomainAccessService(),
    ServiceType.USER_ENV_VARS: UserEnvVarsService(),
    ServiceType.ALIASES: AliasesService(),
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


def deployment_manifest_services() -> list[Service]:
    """Services that contribute deployment-wide manifests, in ``manifest_order`` (RC-15).

    Only services that override ``contribute_deployment_manifests`` are included. The
    generic emitter in ``project_manager.create_application_manifests`` calls each once per
    deployment, after the component loop, and writes the returned specs.
    """
    overriding = [
        s
        for s in SERVICES.values()
        if type(s).contribute_deployment_manifests is not Service.contribute_deployment_manifests
    ]
    return sorted(overriding, key=lambda s: s.manifest_order)


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
    seen: set[tuple] = set()
    for service in SERVICES.values():
        provider = service.definition.actions_provider
        if provider is None:
            continue
        for action in provider(project_data, deployment_name):
            if not action.visible:
                continue
            # Services that share a provider (both PostgreSQL variants offer the same
            # console and job buttons) must not yield the button twice.
            key = (action.label, action.endpoint, action.modal_endpoint)
            if key in seen:
                continue
            seen.add(key)
            actions.append(action)
    return actions


def deployment_action_key(action: DeploymentAction) -> str:
    """A stable, URL-safe id for a deployment action, derived from its label.

    The confirmation dialog addresses an action by this key instead of by its endpoint:
    the endpoint is then never taken from the request, only from what a service really
    offered for this deployment (an endpoint in the URL would be an open POST target).
    """
    return re.sub(r"[^a-z0-9]+", "-", action.label.lower()).strip("-")


def find_deployment_action(project_data: dict, deployment_name: str, action_key: str) -> DeploymentAction | None:
    """The deployment action with this key, or None when no service offers it.

    Re-derives the actions from the project's own services, so an action that a service
    no longer offers (sleep-mode's wake once the deployment is awake) cannot be invoked.
    """
    for action in collect_deployment_actions(project_data, deployment_name):
        if deployment_action_key(action) == action_key:
            return action
    return None


def collect_detail_page_sections(project_data: dict, user_role: str) -> list:
    """Read-only detail-page sections contributed by the services a project uses (WP2).

    Only services the project actually selects (project-level or referenced by a
    component) contribute, in registry order, so the project-details template loops
    over data instead of hardcoding a per-service ``{% include %}``. ``project_data``
    must be the DECRYPTED project dict (a service may surface managed credentials).
    """
    from opi.services.catalog.base import DetailPageSection

    sections: list[DetailPageSection] = []
    for service in selected_services(project_data):
        sections.extend(service.detail_page_sections(project_data, user_role))
    return sections


def selected_services(project_data: dict) -> list[Service]:
    """The services a project actually uses, in registry order.

    "Uses" means selected at project level or referenced by a component -- the same
    reading for every collector that asks the project's own services what they
    contribute to a page. A component's list is read through
    ``extract_service_names_from_component``, so the v1 ``uses-services`` key counts too;
    reading ``services`` alone silently loses every service on an unmigrated component.
    """
    from opi.handlers.project_file_handler import extract_service_names_from_component
    from opi.services.services import service_entry_name

    selected: set[str | None] = {service_entry_name(entry) for entry in project_data.get("services", []) or []}
    for component in project_data.get("components", []) or []:
        selected.update(extract_service_names_from_component(component))
    return [service for service in SERVICES.values() if service.service_type.value in selected]


def collect_deployment_page_sections(ctx: DeploymentPageContext) -> list:
    """Read-only sections the project's services contribute for ONE deployment (RC-24).

    The per-deployment counterpart of ``collect_detail_page_sections``: same selection
    rule, same return type, asked once per deployment so a block about a single
    deployment (its backups, its metrics) lives with the service that owns it.

    A block owned jointly by several services -- backups belong to every service with a
    ``backup_label`` -- is returned by each of them; the same template renders once.
    """
    from opi.services.catalog.base import DetailPageSection

    sections: list[DetailPageSection] = []
    seen: set[str] = set()
    for service in selected_services(ctx.project_data):
        for section in service.deployment_page_sections(ctx):
            if section.template in seen:
                continue
            seen.add(section.template)
            sections.append(section)
    return sections


def collect_service_routers() -> list[Any]:
    """Every distinct ``APIRouter`` the services deliver, for mounting on the web app.

    A service that owns a page block owns its endpoints too (the backups fragment, the
    database-console and job modals). Routers shared by several services -- the backup
    fragment is owned by all backupable services -- are returned as the same object and
    mounted once, so a shared route is not registered per owner.
    """
    routers: list[Any] = []
    for service in SERVICES.values():
        for router in service.web_routers():
            if not any(existing is router for existing in routers):
                routers.append(router)
    return routers


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


def deployment_component_service_editables() -> list[Editable]:
    """Deployment-component editables every service contributes, in
    ``config_component_order`` (RC-25).

    The deployment-component counterpart of ``component_service_editables``. Until RC-25
    this layer had no service-owned hook and its one field (``user-env-vars``) was
    hand-authored in the forms layer; it is now declared by the service that owns it.
    """
    editables: list[Editable] = []
    for service in sorted(SERVICES.values(), key=lambda s: s.config_component_order):
        editables.extend(service.config_editables(ConfigLayer.DEPLOYMENT_COMPONENT))
    return editables


def deployment_component_service_visualizers() -> list[EditableVisualizer]:
    """As ``deployment_component_service_editables``, for the visualizers."""
    visualizers: list[EditableVisualizer] = []
    for service in sorted(SERVICES.values(), key=lambda s: s.config_component_order):
        visualizers.extend(service.config_deployment_component_visualizers())
    return visualizers


def property_owning_services() -> list[Service]:
    """System services that own a plain project-file property (RC-25).

    ``user-env-vars`` and ``aliases`` are services whose config is not a block in a
    ``services:`` list but a property of the component itself. Generic validation reads
    this list instead of naming those two, so a third one is a declaration, not an edit
    to the validator.
    """
    return [s for s in SERVICES.values() if s.owned_property is not None]
