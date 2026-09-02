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
from opi.services.catalog.base import ConfigLayer, DeploymentPageContext, ProjectPageContext, Service
from opi.services.catalog.cross_domain_access import CrossDomainAccessService
from opi.services.catalog.deployment_health import DeploymentHealthService
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
from opi.services.catalog.send_email import SendEmailService
from opi.services.catalog.sleep_mode import SleepModeService
from opi.services.catalog.temp_storage import TempStorageService
from opi.services.catalog.user_env_vars import UserEnvVarsService
from opi.services.catalog.vlam import VlamService
from opi.services.services_enums import ActionEvent, ServiceEvent, ServiceType, UIEvent

if TYPE_CHECKING:
    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.visualizer import EditableVisualizer
    from opi.services.services import DeploymentAction, ServiceDefinition

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
    ServiceType.SEND_EMAIL: SendEmailService(),
    ServiceType.REDIS: RedisService(),
    ServiceType.NAMESPACE_REDIS: NamespaceRedisService(),
    ServiceType.PLATFORM: PlatformService(),
    ServiceType.ATTACHMENTS: AttachmentsService(),
    ServiceType.SLEEP_MODE: SleepModeService(),
    ServiceType.INVITE: InviteService(),
    ServiceType.RESOURCE_TUNING: ResourceTuningService(),
    ServiceType.CROSS_DOMAIN_ACCESS: CrossDomainAccessService(),
    ServiceType.VLAM: VlamService(),
    ServiceType.DEPLOYMENT_HEALTH: DeploymentHealthService(),
    ServiceType.USER_ENV_VARS: UserEnvVarsService(),
    ServiceType.ALIASES: AliasesService(),
}

#: Every service's metadata, keyed by service type -- assembled from what the services
#: themselves declare (RC-36), so adding a service is one package plus its registration
#: above, never an edit to a shared metadata list.
#:
#: The iteration order is visible (``get_backupable_labels`` documents that it follows
#: this dict, and the service picker orders by the enum), so it is pinned explicitly to
#: ``ServiceType`` order instead of inheriting whatever order ``SERVICES`` happens to
#: have. That reproduces the order of the hand-written list this replaced.
SERVICE_DEFINITIONS: dict[ServiceType, ServiceDefinition] = {
    service_type: SERVICES[service_type].definition for service_type in ServiceType
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


def _build_listener_index() -> dict[ServiceEvent, list[Service]]:
    """Who listens to what (RC-39): the single index of service events.

    Built once from the ``@on(...)`` declarations in the catalog. This is the payoff of
    the event mechanism -- "which services care about X" is a lookup, not a scan written
    again at every place that fires an event, and adding an event needs no line in this
    module at all.

    Order within an event is the handler's ``order`` (default 100); services with equal
    order keep registry (``ServiceType``) order, so a listener list never depends on
    import order.
    """
    index: dict[ServiceEvent, list[Service]] = {}
    for event in (*ActionEvent, *UIEvent):
        listeners = [service for service in SERVICES.values() if service.listens_to(event)]
        index[event] = sorted(listeners, key=lambda service: _listener_order(service, event))
    return index


def _listener_order(service: Service, event: ServiceEvent) -> int:
    """A service's position among an event's listeners: its earliest handler's order.

    Read from the index, not off the bound method: a service may override an inherited
    handler by name without repeating the decorator, and then the method carries no marker.
    """
    return min((order for _name, order in service.event_handlers.get(event, ())), default=100)


_LISTENERS: dict[ServiceEvent, list[Service]] = _build_listener_index()


def listeners(event: ServiceEvent, project_data: dict[str, Any] | None = None) -> list[Service]:
    """The services that listen to ``event``, in order.

    ``project_data`` narrows the list to the services the project actually uses, which is
    what a page wants: a block only belongs on a project that has the service. Leave it
    out to ask everyone, which is what state-clearing events want -- a service's record in
    the project file has to be dealt with even when the project no longer lists the
    service today (sleep-mode can be switched on per cluster without a project selecting
    it), and a service that recorded nothing returns nothing anyway.
    """
    found = _LISTENERS.get(event, [])
    if project_data is None:
        return found
    selected = selected_services(project_data)
    return [service for service in found if service in selected]


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


def generate_missing_values(project_data: dict[str, Any]) -> dict[str, str]:
    """Let every service fill in the values it generates, and report what it made.

    The catalog walk behind ``Service.generate_missing_values``, called by the API write
    paths right after the config is written -- the same moment the wizard runs its
    ``post_merge``. Mutates ``project_data`` in place and returns ``{yaml path: value}``
    merged across the catalog, so the write route can tell the caller which value it did
    not choose itself. Empty when nothing was generated, which is the normal case.
    """
    generated: dict[str, str] = {}
    for service in SERVICES.values():
        generated.update(service.generate_missing_values(project_data))
    return generated


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

    payload = ProjectPageContext(project_data=project_data, user_role=user_role)
    sections: list[DetailPageSection] = []
    for service in listeners(UIEvent.PROJECT_SECTIONS, project_data):
        sections.extend(service.handle_ui(UIEvent.PROJECT_SECTIONS, payload))
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
    for service in listeners(UIEvent.DEPLOYMENT_SECTIONS, ctx.project_data):
        for section in service.handle_ui(UIEvent.DEPLOYMENT_SECTIONS, ctx):
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


def component_service_notices() -> list[EditableVisualizer]:
    """De informatieblokken die diensten aan het componentformulier meegeven.

    Naast ``component_service_visualizers``, en bewust apart: die gaan over velden die je
    invult, deze over iets wat je moet weten. Zie ``Service.component_form_notices`` voor
    waarom een mededeling geen configlaag mag claimen.
    """
    notices: list[EditableVisualizer] = []
    for service in sorted(SERVICES.values(), key=lambda s: s.config_component_order):
        notices.extend(service.component_form_notices())
    return notices


def deployment_service_editables() -> list[Editable]:
    """Deployment-level editables every service contributes, in ``config_component_order``.

    The missing sibling of ``component_service_editables`` (RC-60). Until publish-on-web
    took its web-address fields back there was nothing at this layer for a service to
    contribute, so the deployment form hand-authored them -- twice, in two flows, for one
    yaml_path. Services that contribute nothing at the deployment layer add nothing.
    """
    editables: list[Editable] = []
    for service in sorted(SERVICES.values(), key=lambda s: s.config_component_order):
        editables.extend(service.config_editables(ConfigLayer.DEPLOYMENT))
    return editables


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


def deployment_runtime_keys() -> tuple[str, ...]:
    """Every ``deployments[]`` key any service uses for its own runtime state.

    The catalog walk behind ``Service.deployment_runtime_keys``. Asked of every service,
    not only of the ones a project selected: a service can be switched on cluster-wide
    (sleep-mode is), so the state can be there without the project listing the service.
    A service that records nothing contributes nothing, so asking everyone costs nothing.
    """
    keys: list[str] = []
    for service in SERVICES.values():
        keys.extend(service.deployment_runtime_keys())
    return tuple(keys)


def property_owning_services() -> list[Service]:
    """System services that own a plain project-file property (RC-25).

    ``user-env-vars`` and ``aliases`` are services whose config is not a block in a
    ``services:`` list but a property of the component itself. Generic validation reads
    this list instead of naming those two, so a third one is a declaration, not an edit
    to the validator.
    """
    return [s for s in SERVICES.values() if s.owned_property is not None]
