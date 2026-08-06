"""Service base class (RC-5 Phase 1).

The RC-5 migration ("Uniform, Declarative Platform Services") replaces the ~14
hand-maintained per-service edit sites with a single declarative unit per service:
a ``Service`` subclass, registered once in
``opi.services.registry.SERVICES``. Generic code then iterates the registry
instead of hand-synced wizard/flow/provisioning/cleanup/manifest lists.

This module is intentionally dependency-light -- it imports only the service
metadata (``ServiceDefinition``) and the ``ServiceType`` enum, never forms,
managers or connectors. That keeps the provider protocol free of the circular
imports the plan warns about (forms reference providers; providers must not, at
import time, reference forms or managers). Behaviour hooks (config shape,
provisioning, cleanup, manifest contribution) are added to this base class in later
phases, when generic code actually consumes them, so their context types can be
imported lazily / under ``TYPE_CHECKING`` at that point.

Each provider carries its existing ``ServiceDefinition`` (unchanged) plus, for
configurable services, a typed ``config_model`` (Phase 2). ``database_manager``
already validates namespace-postgres config through its provider; the remaining
config models are wired into the read path in Phase 3.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from opi.services.services import ServiceDefinition

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.sections import FormSection
    from opi.forms.visualizers.visualizer import EditableVisualizer
    from opi.services.catalog.approval import ApprovalSpec
    from opi.services.services_enums import HookPoint, ManagerKey, ServiceType
    from opi.utils.secrets import BaseSecret


class ConfigLayer(Enum):
    """The level of the project file at which a service contributes config.

    A single service can plug in at more than one layer, each with a different
    field set: the project-level ``services:`` definition, a component's
    ``services:`` reference, a deployment's services, or a per-component override
    inside a deployment. The layer is encoded in the editable ``yaml_path``
    (``services/X/…`` vs ``components[*]/services{X}/…`` vs ``deployments[*]/…``);
    this enum names it so a provider can answer "what do I contribute at layer L".
    """

    PROJECT = "project"
    COMPONENT = "component"
    DEPLOYMENT = "deployment"
    DEPLOYMENT_COMPONENT = "deployment-component"


class ConfigRole(Enum):
    """What a service's config at a layer *is*: a definition, a use, or a binding.

    "Service config" was one word for three different things, and attachments is the
    first service where they visibly come apart:

    * ``DEFINE`` -- put something into the project that is not used by itself. The
      attachments catalog (a file with an ``id``, a ``filename`` and its encrypted
      ``content``) is defined at project level and used nowhere until a component
      says so. A definition lives under ``data`` on the project-level service entry,
      not under ``config``.
    * ``USE`` -- "this component/deployment uses this service (this thing)". For most
      services the use is the bare service name in a ``services:`` list and there is
      nothing to define, which is why the distinction never had to be made before.
      For attachments the use names *which* definition: ``reference: my-cert``.
    * ``BIND`` -- *how* the used thing reaches the workload: ``provide-as``, ``path``,
      ``env-name``. A binding is meaningless without a use.

    A service answers per layer (``config_roles``), so generic code can ask "does this
    service define something here" without knowing which service it is talking to. The
    roles are documentation of the contract *and* the reason a layer does or does not
    deserve an endpoint: a DEFINE layer needs a way to put the thing in, which is not
    the same request as configuring how it is used.
    """

    DEFINE = "define"
    USE = "use"
    BIND = "bind"


#: The per-layer prefix of a service's config yaml_path. Encodes the layer shape once
#: (project ``services/X`` vs component ``components[*]/services{X}`` etc.) so no
#: service hardcodes it; ``{svc}`` is filled with the ServiceType value.
_LAYER_PATH_PREFIX: dict[ConfigLayer, str] = {
    ConfigLayer.PROJECT: "services/{svc}",
    ConfigLayer.COMPONENT: "components[*]/services{{{svc}}}",
    ConfigLayer.DEPLOYMENT: "deployments[*]/services{{{svc}}}",
    ConfigLayer.DEPLOYMENT_COMPONENT: "deployments[*]/components[*]/services{{{svc}}}",
}


def config_path(layer: ConfigLayer, service: ServiceType, *segments: str) -> str:
    """Build a service's config ``yaml_path`` from enums instead of a hardcoded string.

    ``config_path(ConfigLayer.PROJECT, ServiceType.AUTHORIZATION_WALL, "config", "banner")``
    -> ``"services/authorization-wall/config/banner"``. The layer determines the
    prefix (project / component / deployment), the ServiceType fills the service
    segment, and ``segments`` are the config keys. This keeps service identity and
    layer as enums (typed, greppable, documented) rather than scattered path literals.
    """
    prefix = _LAYER_PATH_PREFIX[layer].format(svc=service.value)
    return "/".join([prefix, *segments]) if segments else prefix


#: A service's raw config as it appears in the project file: a dict for most
#: services, or a list for sequence configs (e.g. storage mounts).
ServiceConfigData = dict[str, Any] | list[Any]


@dataclass
class ProvisionContext:
    """Inputs a provider needs to provision one deployment's resources (RC-5 Phase 4).

    Carries the *already-resolved* managers so a provider delegates to its manager
    without importing it -- keeping provider.py free of manager imports. The managers
    keep their own self-guards (e.g. ``_deployment_uses_postgresql``) and are
    replay-safe, so dispatching through providers stays byte-identical to the old
    fixed sequence.
    """

    project_data: dict[str, Any]
    deployment: dict[str, Any]
    force_clone: bool
    database_manager: Any
    minio_manager: Any
    keycloak_manager: Any
    redis_manager: Any


@dataclass
class RemovalContext:
    """Inputs for cleaning up one service removed from one deployment (RC-5 Phase 5).

    Carries the diff-driven removal args plus ``get_manager`` -- an async resolver
    (``DeleteProjectManager._get_manager_for_service``) so a provider reaches its
    manager lazily by key, exactly as the old ``_SERVICE_TYPE_MANAGER_ATTR`` dispatch
    did. All managers share the ``handle_service_removal`` signature, so the base
    provider dispatch stays byte-identical.
    """

    project_name: str
    deployment_name: str
    deployment_data: dict[str, Any] | None
    project_data: dict[str, Any]
    marked_for_deletion_service: Any
    get_manager: Any  # async callable: (manager_key: str) -> manager


@dataclass
class ManifestContext:
    """Inputs a provider needs to contribute to one component's manifests (RC-5 Phase 6).

    Grows per sub-phase rather than speculatively front-loading fields. Phase 6a needs
    only ``deployment_name`` (envFrom secret names). Phase 6b adds ``project_data`` +
    ``unique_name`` (auth-wall reads the banner from project services and names the
    cookie secret) and ``get_secret`` -- a resolver for an already-provisioned
    per-deployment secret (``ProjectManager._get_secret_from_map``), so the auth-wall
    provider reaches the keycloak secret without importing the manager, exactly like
    RemovalContext.get_manager.
    """

    deployment_name: str
    project_data: dict[str, Any]
    unique_name: str
    cluster: str
    get_secret: Any  # callable: (deployment_name: str, secret_type: str, secret_class) -> secret | None
    #: The component's resolved definition, or None when there is no component
    #: reference. Providers read their component-level config from it (e.g. the
    #: metrics-scraper scrape port/path).
    component_def: dict[str, Any] | None = None


#: Label key a service puts on a pod it owns but that is NOT the application workload.
#:
#: The application's own pods carry ``app=<component>`` and nothing else; a pod a service
#: runs alongside it carries the same ``app`` label (so it can take over the Service) plus
#: this key with a value naming its role. Anything asking "how is the application doing"
#: must therefore exclude pods that carry it.
#:
#: Sleep-mode is why this is named at platform level. Its waker answers to the sleeping
#: component's ``app`` label, so the health check found the waker, read its
#: ImagePullBackOff, and reported "frontend: image ophalen mislukt" for a component that
#: was not running and does not use that image -- while ArgoCD reported the application
#: Synced and Healthy. Worse, that path disables a component on an image-pull failure, so
#: a waker that briefly cannot pull could take the real component out.
SERVICE_ROLE_LABEL_KEY = "zad-role"


def application_pod_selector(app_name: str) -> str:
    """Label selector for the pods of the application itself, excluding service-owned ones.

    ``!zad-role`` means "does not carry that label at all", so a service marking its pod
    with any role is excluded without this having to know which roles exist.
    """
    return f"app={app_name},!{SERVICE_ROLE_LABEL_KEY}"


@dataclass
class SecretFileSpec:
    """A SOPS secret manifest a service needs written for a deployment (RC-5 Phase 6c).

    A service declares *what* secret it needs; the shared writer
    (``ProjectManager._write_secret_file``) does the actual write -- to_k8s aliases,
    ``create_manifest_file``, obsolete-prune bookkeeping. The provider builds
    ``secret_pairs`` from its typed secret (via ``ctx.get_secret`` /
    ``ctx.cluster`` / settings); the shared writer stays service-agnostic.
    """

    #: The Kubernetes Secret name (e.g. ``get_secret_name(deployment_name)``). The
    #: manifest file is ``<secret_name>-secret.to-sops.yaml``.
    secret_name: str
    #: Base secret data (``typed_secret.to_k8s_secret_data()``, or raw pairs for the
    #: auth-wall cookie).
    secret_pairs: dict[str, str]
    #: Alias/secret bucket for cross-component alias resolution + logs (e.g.
    #: ``"database"``); None disables alias resolution (metrics, cookie).
    secret_type: str | None = None
    #: When True, resolve this deployment's aliases of ``secret_type`` into the pairs.
    resolve_aliases: bool = False
    #: Typed secret to register in the deployment's secret map before writing
    #: (``_add_secret_to_create``); only postgres does this today. None = skip.
    register_secret: Any = None
    #: Extra labels on the Secret's metadata.
    secret_labels: dict[str, str] | None = None
    #: Whether this secret counts as application configuration.
    #:
    #: Set False for a secret only the service's own auxiliary pod reads. The platform
    #: then keeps it out of the config-hash that restarts application pods when their
    #: configuration changes; a service says what its secret IS, and never has to know
    #: that such a hash exists.
    #:
    #: Sleep-mode is why this is here. Its waker token Secret exists only while a
    #: deployment sleeps, so pruning it on wake changed the hash and restarted the
    #: application a second time right after it came back up -- while nothing the
    #: application reads had changed at all.
    include_in_config_hash: bool = True


@dataclass
class ManifestContribution:
    """What a provider adds to a component's manifests (RC-5 Phase 6).

    A declarative description the generic component loop merges into the template
    context -- the provider never touches the manifest generator itself. Two merge
    semantics, matching "a service may *add to* and *override* the base manifest":

    * ``env_from_secrets`` / ``sidecars`` are **additive** (the loop extends the base
      lists, in ``manifest_order``).
    * ``template_vars`` is an **override** (the loop ``update``s the base template
      context, so e.g. auth-wall replaces ``service_port`` 8080 -> 4180).

    With a single override provider today the order is moot; when several providers
    override the same key (e.g. two proxies chained in front) ``manifest_order`` will
    define precedence. Phase 6c adds the secret-file anchor, because a service's secret
    files conceptually belong to that service.
    """

    #: Secret names to append to the pod's ``envFrom`` list, in ``manifest_order``.
    env_from_secrets: list[str] = field(default_factory=list)
    #: Template-context keys to override (merged with ``dict.update``).
    template_vars: dict[str, Any] = field(default_factory=dict)
    #: Sidecar names to append to the pod's ``sidecars`` list.
    sidecars: list[str] = field(default_factory=list)
    #: SOPS secret manifests this service needs written (RC-5 Phase 6c).
    secret_files: list[SecretFileSpec] = field(default_factory=list)


@dataclass
class ComponentHealth:
    """Observed pod health for one component after a sync (task 8).

    A uniform, caller-agnostic shape: both the inline deploy path (from
    ``DeploymentHealthError.failures``) and the fire-and-forget watcher (from
    ``PodHealthResult``) build this, so an observation hook reads one thing.
    """

    oom_detected: bool = False
    crash_loop: bool = False
    image_pull_error: str | None = None


@dataclass
class DeploymentObservationContext:
    """Inputs an after-sync observation hook needs (task 8, ``HookPoint.AFTER_SYNC``).

    Mirrors ``ProvisionContext`` / ``ManifestContext``. The hook observes the running
    deployment's pod health and may mutate ``project_data`` in place, but it never
    commits: the generic runner does a single ``save_and_commit_project`` for all hook
    outcomes together, so two services on this hook cannot race to two commits.

    ``component_health`` is keyed by component reference. Fields are added when a hook
    needs them (as ``ManifestContext`` grew), which is why there is no ``get_manager``
    yet -- the one hook today (resource-tuning) reaches Prometheus/kubectl through its
    own business module, not a manager.
    """

    project_name: str
    deployment_name: str
    project_data: dict[str, Any]  # in-memory, mutable
    cluster: str
    namespace: str
    component_health: dict[str, ComponentHealth]


@dataclass
class ObservationOutcome:
    """What an observation hook reports back (task 8), declarative like
    ``ManifestContribution``.

    The runner aggregates these across services: it commits once if any hook changed
    ``project_data``, queues a refresh if any asked, and surfaces failures/notices.
    """

    #: The hook mutated ``ctx.project_data`` and it should be committed.
    project_data_changed: bool = False
    #: A refresh task should be queued for this deployment.
    requeue_refresh: bool = False
    #: Blocking messages surfaced as sync failures.
    failures: list[str] = field(default_factory=list)
    #: Non-blocking messages for the task progress.
    notices: list[str] = field(default_factory=list)


@dataclass
class DeploymentStateContext:
    """Inputs a service needs to answer "what do you know about this deployment"
    (RC-28, ``HookPoint.DEPLOYMENT_STATE``).

    The question is answered from the PROJECT FILE, not from the cluster: the project
    file is where a service records what it did (sleep-mode's ``deployments[].sleep``),
    and asking the cluster would mean deriving the answer from the very observation the
    asker is trying to interpret. That also keeps the hook synchronous and free of
    connectors, so a page render can ask it as cheaply as the health check does.
    """

    project_name: str
    project_data: dict[str, Any]
    #: The deployment being asked about.
    deployment: dict[str, Any]

    @property
    def deployment_name(self) -> str:
        return self.deployment.get("name", "")


@dataclass
class DeploymentStateFact:
    """One thing a service knows about a deployment (RC-28).

    A **fact**, deliberately not a health verdict. A service says "this deployment is
    asleep and therefore has no application pods"; whether that makes the deployment
    healthy is the health check's judgement, made from the fact. Without that split,
    "I am asleep" quietly becomes "so everything is fine" and a service with a stale
    state hides a real outage. There is therefore no ``healthy`` field here, and
    ``tests/test_deployment_state.py`` holds the shape to it.

    ``expects_no_application_pods`` is the one operational consequence a service may
    state, and it is narrow on purpose: it says the application's own pods are meant to
    be absent. It never excuses a problem observed on a pod that IS there.

    ``badge`` is the second and last thing a service may say about the display (RC-35):
    the one word that belongs on the deployment card. It is text only -- no colour, no
    icon -- because a card full of service-chosen styling stops reading as one platform.
    Together with ``expects_no_application_pods`` it decides where that word lands:

    * badge + ``expects_no_application_pods`` -- nothing of the application is supposed
      to run, so the green "Healthy" that zero pods produce is the untruth this word
      takes the place of.
    * badge without it -- part of the deployment is still serving traffic, so the health
      verdict is still the thing to report and the word stands next to it.

    Only the green Healthy is ever replaced: Degraded, Progressing and Unknown are
    something really observed, and a state that hid them would make switching a
    component off a way to make a failure disappear.
    """

    #: ``ServiceType.value`` of the service that knows this.
    service: str
    #: One line, in the user's language, describing the situation.
    summary: str
    #: True when this service has deliberately scaled the application to zero pods.
    expects_no_application_pods: bool = False
    #: One or two words for the deployment card, or None to stay off the card.
    badge: str | None = None
    #: Extra data for a service's own rendering of the fact (never read by generic code
    #: for a decision).
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RedeployContext:
    """Inputs a service needs when new content is rolled out onto a deployment (RC-37,
    ``HookPoint.REDEPLOY``).

    The writing counterpart of ``DeploymentStateContext``: that hook asks a service what
    state it put a deployment in, this one tells it that the state describes content that
    is no longer there. Deliberately about the ACTION, not about the image: an image
    update and a deployment upsert are the same event as far as recorded state is
    concerned, and a context named after the image would have made the second one an
    exception.

    ``project_data`` is the in-memory dict the caller is about to commit, and a hook
    mutates it in place -- exactly like ``DeploymentObservationContext``, and for the same
    reason: the caller does ONE commit for the rollout and every state cleanup together,
    so two services cannot race to two commits.
    """

    project_name: str
    project_data: dict[str, Any]  # in-memory, mutable
    #: The deployment being rolled out, as it appears in ``project_data``.
    deployment: dict[str, Any]
    #: ``reference`` of every component this rollout put new content on. Empty means the
    #: action touched no component in particular; a service whose state is per-component
    #: then has nothing to clear, while a per-deployment state (sleep) still does.
    component_names: list[str] = field(default_factory=list)

    @property
    def deployment_name(self) -> str:
        return self.deployment.get("name", "")

    @property
    def cluster(self) -> str:
        return self.deployment.get("cluster", "")


@dataclass
class DetailPageSection:
    """A read-only section a service renders on the project-details page (WP2).

    The service owns both the template and its data; the detail page loops over what
    the project's selected services return instead of hardcoding a per-service
    ``{% include %}``. This is the read-only counterpart of ``config_form_section``:
    without it a section (like the Keycloak realm block) drifts away from its config
    whenever that config moves, because the template still points at the old location.

    ``template`` is any path the app Jinja environment can resolve. Service-owned
    templates live next to the service under ``opi/services/catalog/<svc>/`` and are
    addressed as ``<svc>/<file>`` -- the catalog directory is on the template search
    path (see ``opi/core/templates.py``).
    """

    #: Template path resolvable by the app Jinja env (e.g. ``keycloak/section-detail.html.j2``).
    template: str
    #: Data the template reads, exposed to the include as ``section.context``.
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeploymentPageContext:
    """Inputs a service needs to render its per-deployment detail-page block (RC-24).

    ``detail_page_sections`` covers the project level; backups, metrics and the
    deployment action buttons belong to ONE deployment, so they need their own hook.
    Same shape on the way out (a list of ``DetailPageSection``), one context object on
    the way in, because a deployment block needs more than the project dict.

    ``project_data`` is the DECRYPTED project dict and ``deployment`` the deployment
    being rendered. ``backend_available`` carries the optional back-ends the view
    already probed for the page (``prometheus``, ``backups``): a service must not call
    a connector itself, so the one place that knows passes the answer in.
    """

    project_data: dict[str, Any]
    deployment: dict[str, Any]
    user_role: str
    #: The cluster this OPI instance manages (``settings.CLUSTER_MANAGER``).
    current_cluster: str
    #: Probed availability of optional platform back-ends, keyed by name.
    backend_available: dict[str, bool] = field(default_factory=dict)


@dataclass
class DeploymentManifestContext:
    """Inputs a service needs to contribute deployment-wide manifests (RC-15).

    Unlike ``ManifestContext`` (which runs once per component), this runs once per
    deployment, after the component loop. A service that needs a resource scoped to the
    whole deployment -- a NetworkPolicy that references the deployment's peers, say --
    contributes here. ``project_data`` is the full project dict so the service can read its
    own project- and deployment-level config; ``deployment`` is the current deployment dict;
    ``namespace`` is already cluster-prefixed.
    """

    project_name: str
    project_data: dict[str, Any]
    deployment: dict[str, Any]
    cluster: str
    namespace: str


@dataclass
class DeploymentManifestSpec:
    """One deployment-wide manifest a service asks the generic emitter to write (RC-15).

    The service returns a declarative spec; ``project_manager`` renders ``template_path``
    with ``values`` and writes ``<filename>.yaml``. ``filename`` MUST start with
    ``f"{deployment_name}-{service_type.value}-"`` -- the obsolete-manifest prune
    (``_prune_obsolete_service_manifests``) keys on that prefix to remove a service's files
    when it is switched off or stops contributing.
    """

    #: Basename without ``.yaml``. Must start with ``f"{deployment}-{service_type.value}-"``.
    filename: str
    #: Template path resolvable relative to the ``manifests/`` directory.
    template_path: str
    values: dict[str, Any]


class Service(ABC):
    """One subclass per ``ServiceType``; the single declarative home for a service.

    A subclass sets ``service_type`` AND its own ``definition`` (RC-36): the metadata
    of a service lives in that service's own package, next to its config model, its
    editables and its templates, so taking a service over is "copy the directory".
    ``ServiceAdapter.SERVICE_DEFINITIONS`` is derived from what the services declare
    here (see ``opi.services.registry``), not the other way around, so there is no
    shared list to keep in sync. ``__init_subclass__`` enforces the pairing.

    Config shape + versioning (RC-5 Phase 2)
    ----------------------------------------
    A configurable service owns its config as a self-contained, independently
    versioned unit, mirroring the Kubernetes CRD model (envelope + discriminator +
    hub-and-spoke conversion):

    * ``config_model`` -- a Pydantic model that is both the value guardrail and the
      source of the service's JSON-schema fragment. The service's config in the
      project file is validated against it; the global ``project_v2.json`` validates
      only the envelope and stays stable as service configs evolve.
    * ``config_schema_version`` -- the service's *current* ("hub"/storage) schema
      version as a ``major.minor`` string. Each service versions independently.
    * ``migrate_config`` -- forward-only conversion (spoke -> hub). ZAD never serves
      old versions to clients; it only ever reads a possibly-old file and writes the
      current version, so no down-conversion is needed. Convert-then-validate: an
      older config is migrated forward, then validated against ``config_model``.

    A service that takes no config (``namespace-redis``, ``platform``) leaves both
    ``config_model`` and ``config_schema_version`` as ``None`` and inherits the no-op
    defaults. Every service that does carry config declares both.
    """

    #: The service this provider handles. Set by each concrete subclass.
    service_type: ClassVar[ServiceType]
    #: This service's metadata (name, icon, binding, variables, ...). Declared by the
    #: concrete subclass in its own package, so nothing about a service lives outside
    #: its directory. ``__init_subclass__`` rejects a subclass that forgets it.
    definition: ClassVar[ServiceDefinition]

    #: Pydantic model for this service's config, or None if it takes no config.
    config_model: ClassVar[type[BaseModel] | None] = None
    #: Current config schema version (major.minor), or None for a service that takes no
    #: config. Inheriting a default "1.0" made every behaviour-only service look like it
    #: had a versioned contract; the pairing with config_model is enforced by
    #: tests/test_service_config_schema.py.
    config_schema_version: ClassVar[str | None] = None

    #: Wizard/edit config-section id for this service (RC-5 Phase 3), or None if the
    #: service has no config UI. The FormSection object itself lives in the forms
    #: layer (wizard_sections); this is only the declarative link, so provider.py
    #: stays free of forms imports. The forms layer derives SERVICE_CONFIG_SECTIONS /
    #: EDIT_SECTIONS by iterating the registry instead of a hand-synced dict.
    config_section_id: ClassVar[str | None] = None
    #: Modal-edit flow id for this service's config, or None. SERVICE_CONFIG_MODAL_FLOWS
    #: is derived from this.
    modal_flow_id: ClassVar[str | None] = None

    #: Display order of this service's ``config_component_layout()`` nodes within the
    #: per-component form; lower shows first. A static ordering for now -- a
    #: user-facing priority is a deferred future refinement.
    config_component_order: ClassVar[int] = 100

    #: Layers where this service carries config but deliberately offers no form, mapped
    #: to the reason. Clone state OPI writes itself is the obvious case; so is a layer
    #: that is API-only on purpose. The point is that the choice is written down: without
    #: it a missing form section is indistinguishable from a forgotten one, which is
    #: exactly how half the catalog ended up with config nobody could edit.
    #: ``tests/test_service_config_layers.py`` holds every service to this.
    form_exempt_layers: ClassVar[dict[ConfigLayer, str]] = {}

    #: For a SYSTEM service that owns a plain project-file property instead of a block
    #: in a ``services:`` list (``user-env-vars``, ``aliases``), the property key it
    #: owns. Generic validation walks the layers this service declares editables for and
    #: validates that key against ``config_model``; None means "config lives in the
    #: services list", which is the normal case.
    owned_property: ClassVar[str | None] = None

    #: Order in the generic provisioning loop (RC-5 Phase 4); lower runs first. Only
    #: meaningful for providers that override ``provision``. The defaults on the four
    #: provisioning providers reproduce today's fixed db -> minio -> keycloak -> redis
    #: sequence.
    provision_order: ClassVar[int] = 100

    #: Manager key for server-side cleanup on removal (RC-5 Phase 5), or None if the
    #: service has no server-side resources to clean up. Resolved via
    #: RemovalContext.get_manager; replaces the _SERVICE_TYPE_MANAGER_ATTR map.
    cleanup_manager_key: ClassVar[ManagerKey | None] = None

    #: Per-deployment secret whose name is added to the pod's ``envFrom`` when a
    #: component uses this service (RC-5 Phase 6a), or None. The base
    #: ``contribute_manifest_context`` derives the name via
    #: ``manifest_secret_class.get_secret_name(deployment_name)``.
    manifest_secret_class: ClassVar[type[BaseSecret] | None] = None
    #: Order of this provider's contribution in the generic manifest loop; lower runs
    #: first. Pins today's fixed envFrom append order (db 10, minio 20, keycloak 30,
    #: redis 40, metrics 50) so the golden renders stay byte-identical.
    manifest_order: ClassVar[int] = 100
    #: Service types that activate this provider's manifest contribution. Empty means
    #: "just my own service_type"; the shared postgres/redis providers override this to
    #: also fire for their namespace variant (mirroring the provisioning grouping), so
    #: exactly one provider contributes per manager.
    manifest_activated_by: ClassVar[tuple[ServiceType, ...]] = ()

    #: Order of this service at each hook point (task 8); lower runs first, default 100.
    #: A per-hook map so a service on two hook points does not share one order. Only
    #: meaningful for a hook the service overrides.
    hook_order: ClassVar[dict[HookPoint, int]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # A concrete provider must declare which service it is AND what that service
        # is (its definition), both in its own package. Abstract intermediate
        # subclasses (no service_type) are allowed and simply skipped.
        service_type = cls.__dict__.get("service_type")
        if service_type is None:
            return
        definition = cls.__dict__.get("definition")
        if definition is None:
            raise TypeError(
                f"{cls.__name__} declares service_type {service_type.value!r} but no 'definition'. "
                f"Every service carries its own ServiceDefinition in its own package."
            )
        if not isinstance(definition, ServiceDefinition):
            raise TypeError(f"{cls.__name__}.definition must be a ServiceDefinition, got {type(definition).__name__}")

    def config_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        """The model that validates this service's config *at ``layer``*.

        Almost always ``config_model``: one shape, wherever it appears. The exception is a
        service that carries genuinely different content per layer, and there is one today.
        persistent-storage and temp-storage hold mount specs on the component, but per-mount
        clone state on the deployment-component, so a single model cannot describe both.

        Returning None means "not validated at this layer", which is what a service without
        a model gets.
        """
        return self.config_model

    def data_model_for(self, layer: ConfigLayer) -> type[BaseModel] | None:
        """The model that validates this service's DEFINE-side payload at ``layer``.

        A definition does not live under ``config`` but under ``data`` on the service
        entry, because it is not configuration of a use -- it is the thing being used.
        Only a service with a ``ConfigRole.DEFINE`` layer has one; everything else
        returns None and is skipped by the validation walk.

        Separate from ``config_model_for`` on purpose: a service can both define
        something at project level and be configured at component level, with two
        different shapes, and one hook cannot answer both.
        """
        return None

    def config_roles(self, layer: ConfigLayer) -> tuple[ConfigRole, ...]:
        """What this service's config at ``layer`` is: define, use and/or bind.

        The default is ``(USE,)`` for every layer the service carries config on and
        ``()`` for the rest: config on a component says "this component uses this
        service, thus". That is the honest reading for nearly the whole catalog, where
        there is nothing to define and the binding is implied by the service itself.

        A service whose layers mean more than that says so -- attachments defines a
        catalog at project level and both uses and binds at component level.
        ``tests/test_service_config_roles.py`` holds every service to naming a role for
        each layer it carries config on.
        """
        return (ConfigRole.USE,) if layer in self.config_layers() else ()

    def migrate_config(self, config: ServiceConfigData, from_version: str) -> ServiceConfigData:
        """Convert an older config forward to ``config_schema_version`` (hub).

        Forward-only (spoke -> hub); the default is identity, correct for a service
        still at its first version. A service that bumps its version overrides this
        and applies the ordered steps ``from_version -> ... -> current``. Keep each
        step simple and lossless where possible (the Kubernetes conversion rule).

        ``config`` is a dict for most services, or a list for services whose config
        is a sequence (e.g. storage mounts).
        """
        return config

    def validate_config(
        self, raw_config: ServiceConfigData | None = None, from_version: str | None = None
    ) -> BaseModel:
        """Migrate (if needed) then validate this service's config; fail closed.

        ``from_version`` is the version stamped on the project-file entry; ``None``
        means the entry predates versioning and is treated as this service's current
        version (the config already matches the current model). Raises
        ``pydantic.ValidationError`` on bad values, or ``TypeError`` if the service
        takes no config.

        ``raw_config`` may be a dict (most services) or a list (sequence configs
        such as storage mounts). ``None`` defaults to an empty dict, which suits
        dict-config services; list-config services are always passed their list.
        """
        if self.config_model is None:
            raise TypeError(f"Service '{self.service_type.value}' takes no config")
        config: ServiceConfigData = {} if raw_config is None else raw_config
        migrated = self.migrate_config(config, from_version or self.config_schema_version)
        return self.config_model.model_validate(migrated)

    # --- config field ownership (RC-5 "service owns its fields") ----------------
    # A service owns the fields it needs and exposes them per layer + per consumer.
    # ``config_editables`` is the DATA (yaml_path + validator + default) for a service's
    # config at a layer; ``config_form_section`` is the wizard/embed UI view;
    # ``config_api_fields`` the field names the API/YAML accepts (derived from the model
    # via ``config_model_field_names`` for modelled services). ``config_api_fields`` and
    # ``config_editables`` are consumed by the config-validation chokepoint
    # (``manager/project_validation.py``) to report a service's accepted config fields
    # when its config fails validation. Defaults are empty, so a service with no config,
    # or one not yet migrated to owning its fields, keeps working. Concrete providers
    # import the forms building blocks lazily (inside the method) so provider.py /
    # registry.py stay free of forms imports at load time.

    def config_editables(self, layer: ConfigLayer) -> list[Editable]:
        """The DATA editables this service contributes at ``layer`` (default none)."""
        return []

    def config_form_section(self, layer: ConfigLayer) -> FormSection | None:
        """The wizard/edit config section this service contributes at ``layer``, or None.

        The forms layer sources its per-service sections from here instead of
        hand-authoring them. A PROJECT-level section is a standalone wizard step and the
        service builds it itself (keycloak, sleep-mode, ...).

        The component and deployment-component layers are different: those fields are
        *embedded* in the per-component form rather than shown as their own step, so
        there is nothing per-service to author. This default builds that section from
        what the service already declares for the layer (its visualizers + its layout
        nodes), which is why every component-level service answers the hook without a
        line of its own code. A service with no fields at the layer still returns None.

        Answering the hook at the layer where the config actually lives is what
        ``tests/test_service_config_layers.py`` locks: a service that carries config
        somewhere either has a section there or names the layer in ``form_exempt_layers``
        with the reason it has no form.
        """
        from opi.forms.visualizers.sections import FormSection

        if layer is ConfigLayer.COMPONENT:
            visualizers, layout = self.config_component_visualizers(), self.config_component_layout()
            title = f"{self.definition.name} (per component)"
        elif layer is ConfigLayer.DEPLOYMENT_COMPONENT:
            visualizers, layout = (
                self.config_deployment_component_visualizers(),
                self.config_deployment_component_layout(),
            )
            title = f"{self.definition.name} (per deployment-component)"
        else:
            return None

        if not visualizers:
            return None
        return FormSection(
            section_id=f"{self.service_type.value}-{layer.value}-config",
            title=title,
            editables=visualizers,
            layout=layout,
            post_save_action="process_project",
        )

    def config_layers(self) -> list[ConfigLayer]:
        """The layers at which this service carries config, measured from its own hooks.

        A layer counts when the service declares editables for it, accepts API fields
        for it, hooks layout nodes into that layer's form, or carries a DEFINE-side
        payload there. Derived rather than declared, so it cannot drift from the
        implementation -- the same trick ``registry.provisioning_services()`` uses.
        """
        layers = []
        for layer in ConfigLayer:
            if self.data_model_for(layer) is not None:
                layers.append(layer)
                continue
            has_layout = (
                bool(self.config_component_layout())
                if layer is ConfigLayer.COMPONENT
                else bool(self.config_deployment_component_layout())
                if layer is ConfigLayer.DEPLOYMENT_COMPONENT
                else False
            )
            if self.config_editables(layer) or self.config_api_fields(layer) or has_layout:
                layers.append(layer)
        return layers

    def detail_page_sections(self, project_data: dict[str, Any], user_role: str) -> list[DetailPageSection]:
        """Read-only project-details sections this service contributes (default none).

        The read-only counterpart of ``config_form_section``: the detail view
        (``collect_detail_page_sections``) gathers these across the services the
        project actually uses and renders each, so the presentation of a service's own
        data lives with the service instead of hardcoded in the general template.

        ``project_data`` is the DECRYPTED project dict, so a service can surface its
        managed credentials; ``user_role`` lets the service gate on the viewer's role
        (a section that returns nothing for a role simply omits itself). A service with
        nothing to show on the detail page returns ``[]``.
        """
        return []

    def deployment_page_sections(self, ctx: DeploymentPageContext) -> list[DetailPageSection]:
        """Read-only detail-page sections this service contributes for ONE deployment
        (default none, RC-24).

        The per-deployment counterpart of ``detail_page_sections``: same return type,
        collected the same way (``collect_deployment_page_sections``), but asked once
        per deployment on the Deployments tab. Blocks that describe a single deployment
        -- its backups, its metrics -- belong here; a block about the project as a whole
        belongs in ``detail_page_sections``.

        A block several services own jointly (backups: every service with a
        ``backup_label``) is returned by each of them and rendered once; the collector
        drops repeats of the same template.
        """
        return []

    def web_routers(self) -> list[Any]:
        """The ``APIRouter``s carrying this service's own web endpoints (default none).

        A service that delivers a section does not stop at the HTML: the backups block
        lazy-loads its rows over ``hx-get``, the database console and the job runner are
        modals with their own start/status/stop routes. Without this hook half the block
        stays behind in the general router. ``registry.collect_service_routers()``
        gathers them and ``opi/web/router.py`` mounts them.

        Return the SAME router object from every service that shares it (the backup
        fragment belongs to all backupable services); the collector mounts each distinct
        router once. Typed loosely so this module stays free of a FastAPI import.
        """
        return []

    def config_component_layout(self) -> list[Any]:
        """Layout node(s) this service HOOKS INTO the per-component form (default none).

        Component-level services (storage, metrics-scraper, ...) don't get a standalone
        wizard step -- their fields live inside the component definition. This is the
        hook point: the component form assembly collects each service's nodes and
        inserts them. (Display ordering across services is registry order for now; an
        explicit priority is a later refinement.)
        """
        return []

    def config_component_visualizers(self) -> list[EditableVisualizer]:
        """The visualizers this service contributes to the per-component form (default none).

        The visualizer counterpart of ``config_editables(ConfigLayer.COMPONENT)``: the
        component-form aggregation (``COMPONENTS_SEQUENCE``) collects each service's
        visualizers in ``config_component_order`` instead of a hand-synced list, so a
        component-level service owns the display of its own fields."""
        return []

    def config_deployment_component_layout(self) -> list[Any]:
        """Layout node(s) this service hooks into the per-deployment component form.

        The deployment-component counterpart of ``config_component_layout()``: fields a
        deployment overrides on one of its components (``user-env-vars`` today). Until
        RC-25 this layer had no service-owned hook at all and its fields were
        hand-authored in ``forms/editables/fields/deployments.py``; a service that owns
        config here now declares it, in ``config_component_order``.
        """
        return []

    def config_deployment_component_visualizers(self) -> list[EditableVisualizer]:
        """The visualizers this service contributes to the per-deployment component form."""
        return []

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        """The config field names the API accepts at ``layer`` (default none).

        Services with a ``config_model`` derive these from the model (see
        ``config_model_field_names``) rather than re-declaring them, so the API
        surface stays in lock-step with the schema."""
        return []

    def config_model_field_names(self) -> list[str]:
        """The field names of this service's ``config_model`` (alias-aware), or [].

        The single source for "which config keys does this service have" - reused by
        ``config_api_fields`` so the API surface is not a second hand-maintained copy."""
        if self.config_model is None:
            return []
        return [field.alias or name for name, field in self.config_model.model_fields.items()]

    # --- approval ownership (RC-5 "service owns what needs approving") -----------
    # A service declares, as data, which of the values it manages need approval before
    # they take effect, and supplies the rule that reads the stored approval state back.
    # Generic code (a catalog-driven approval interface, enforcers) consumes these
    # uniformly instead of hard-coding one subsystem per approvable thing. See
    # ``opi/services/catalog/approval.py``.

    def config_approvals(self, layer: ConfigLayer) -> list[ApprovalSpec]:
        """The approval declarations this service contributes at ``layer`` (default none)."""
        return []

    def approval_specs(self) -> list[ApprovalSpec]:
        """All of this service's ApprovalSpecs across every layer (deduped by key).

        The catalog-wide entry point for generic approval code (listing, recording):
        it does not need to know which layer a spec lives at."""
        seen: dict[str, ApprovalSpec] = {}
        for layer in ConfigLayer:
            for spec in self.config_approvals(layer):
                seen.setdefault(spec.key, spec)
        return list(seen.values())

    def get_approval(self, key: str) -> ApprovalSpec | None:
        """This service's ApprovalSpec with ``key`` (searched across layers), or None."""
        for spec in self.approval_specs():
            if spec.key == key:
                return spec
        return None

    async def provision(self, ctx: ProvisionContext) -> None:
        """Provision this service's deployment-level resources (RC-5 Phase 4).

        Default no-op -- for services with no deployment-level provisioning (storage,
        publish-on-web, ...). The four provisioning services override this to delegate
        to their manager's ``create_resources_for_deployment`` (self-guarded and
        replay-safe), so the generic loop is byte-identical to the old fixed sequence.
        """
        return

    def applies_to(self, project_data: dict[str, Any], deployment_name: str) -> bool:
        """Whether this service applies to this project/deployment (task 9).

        A ``SYSTEM`` service always runs; a ``USER`` service runs only when the project
        selected it (project-level or referenced by a component). Generic code filters
        the hook scan through this, so no caller names a specific service.
        """
        from opi.services.services import service_entry_name
        from opi.services.services_enums import ServiceKind

        if self.definition.kind is ServiceKind.SYSTEM:
            return True
        selected = {service_entry_name(entry) for entry in project_data.get("services", []) or []}
        for component in project_data.get("components", []) or []:
            for entry in component.get("services", []) or []:
                selected.add(service_entry_name(entry))
        return self.service_type.value in selected

    async def observe_deployment(self, ctx: DeploymentObservationContext) -> ObservationOutcome:
        """Observe a just-synced deployment's running state (task 8, ``AFTER_SYNC``).

        Default no-op, so only services that override it are scanned
        (``registry.services_for_hook``). A hook may mutate ``ctx.project_data`` but
        must not commit -- the generic runner does one commit for all outcomes.
        """
        return ObservationOutcome()

    def deployment_state(self, ctx: DeploymentStateContext) -> list[DeploymentStateFact]:
        """What this service knows about the state of one deployment (RC-28, default none).

        The read counterpart of ``observe_deployment``: that hook acts after a sync, this
        one answers a question anyone may ask at any moment. A service that put a
        deployment in a particular situation -- sleep-mode scaling it to zero -- reports it
        here, so generic code (the health check, the deployment page) learns the situation
        from the service that caused it instead of guessing from what the cluster shows.

        Return FACTS, never a health verdict: see ``DeploymentStateFact``. Synchronous and
        project-file-only; a service that needs the cluster to answer is answering a
        different question.
        """
        return []

    def on_redeploy(self, ctx: RedeployContext) -> list[str]:
        """Clear the state this service recorded about content that is now replaced
        (RC-37, ``HookPoint.REDEPLOY``, default none).

        Fires when a deliberate action puts new content on a deployment -- an image
        update, a deployment upsert. Everything this service recorded about the previous
        content stops holding at that moment: a component switched off because the old
        image OOM'd, a deployment put to sleep because the old content sat idle. The new
        content is the signal that the old situation no longer applies, so a service
        clears its state unconditionally rather than reasoning about whether the new
        content will hit the same problem. If it does, the watcher records it again --
        against the image that actually caused it.

        Return one line per thing cleared, in the user's language: clearing state without
        saying so leaves a component silently switched back on and nobody able to see why
        it was off. Mutate ``ctx.project_data`` in place and never commit -- the caller
        commits once for the rollout and every cleanup together.

        A service that recorded nothing about this deployment returns ``[]``; a service
        that has nothing to do with rollouts does not answer the hook at all.
        """
        return []

    async def handle_service_removal(self, ctx: RemovalContext) -> dict[str, Any]:
        """Clean up this service's server-side resources when it is removed from a
        deployment (RC-5 Phase 5).

        Generic by default: a service with a ``cleanup_manager_key`` resolves that
        manager via the context and delegates to its ``handle_service_removal`` (all
        managers share the signature), byte-identical to the old
        ``_SERVICE_TYPE_MANAGER_ATTR`` dispatch. A service with no server-side
        resources (``cleanup_manager_key is None``) returns an empty result.
        """
        if self.cleanup_manager_key is None:
            return {}
        manager = await ctx.get_manager(self.cleanup_manager_key)
        return await manager.handle_service_removal(
            project_name=ctx.project_name,
            deployment_name=ctx.deployment_name,
            deployment_data=ctx.deployment_data,
            project_data=ctx.project_data,
            marked_for_deletion_service=ctx.marked_for_deletion_service,
        )

    def manifest_activation_types(self) -> tuple[ServiceType, ...]:
        """Service types whose presence in a component activates this provider's
        manifest contribution (RC-5 Phase 6). Defaults to just this provider's own
        service_type; shared providers (postgres, redis) override ``manifest_activated_by``
        to also fire for their namespace variant.
        """
        return self.manifest_activated_by or (self.service_type,)

    @classmethod
    def contributes_to_manifests(cls) -> bool:
        """Whether this provider contributes anything to a component's manifests
        (RC-5 Phase 6): either a per-deployment envFrom secret (6a) or an overridden
        ``contribute_manifest_context`` (6b: auth-wall sidecar + service_port override).
        ``registry.manifest_services()`` uses this to build the contributor set.
        """
        return (
            cls.manifest_secret_class is not None
            or cls.contribute_manifest_context is not Service.contribute_manifest_context
        )

    def contribute_manifest_context(self, ctx: ManifestContext) -> ManifestContribution:
        """This service's contribution to a component's manifests (RC-5 Phase 6).

        Phase 6a: a service with a ``manifest_secret_class`` contributes that secret's
        name to the pod's ``envFrom`` -- byte-identical to the hand-written append
        block it replaces. Services with no manifest contribution inherit the empty
        default. Later sub-phases extend ``ManifestContribution`` (sidecars, template
        vars, secret files) and override this hook accordingly.
        """
        contribution = ManifestContribution()
        if self.manifest_secret_class is not None:
            contribution.env_from_secrets.append(self.manifest_secret_class.get_secret_name(ctx.deployment_name))
        contribution.secret_files = self.build_secret_files(ctx)
        return contribution

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        """SOPS secret manifests this service needs for the deployment (RC-5 Phase 6c).

        Default none. The credential services (postgres, minio, redis, metrics)
        override this to build their typed secret from ``ctx.get_secret`` /
        ``ctx.cluster`` / settings and return a spec; the shared writer
        (``ProjectManager._write_secret_file``) does the actual write. A service that
        cannot build its secret (no provisioned credentials) returns ``[]`` and logs,
        matching the old warn-and-skip branches.
        """
        return []

    def contribute_deployment_manifests(self, ctx: DeploymentManifestContext) -> list[DeploymentManifestSpec]:
        """Deployment-wide manifests this service contributes (RC-15, default none).

        Runs once per deployment (after the per-component loop), for resources that belong
        to the whole deployment rather than a single component. cross-domain-access is the
        first user: it returns one NetworkPolicy spec per own component that has rules. A
        service with nothing to add inherits the empty default; the generic emitter and the
        service-manifest prune both skip it.
        """
        return []
