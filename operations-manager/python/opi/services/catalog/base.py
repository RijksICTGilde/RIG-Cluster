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

from opi.services.services import ServiceAdapter, ServiceDefinition

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.forms.editables.editable import Editable
    from opi.forms.visualizers.sections import FormSection
    from opi.services.catalog.approval import ApprovalSpec
    from opi.services.services_enums import ServiceType
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


class Service(ABC):
    """One subclass per ``ServiceType``; the single declarative home for a service.

    A subclass sets ``service_type``; the matching ``ServiceDefinition`` is bound
    automatically from ``ServiceAdapter.SERVICE_DEFINITIONS`` (see
    ``__init_subclass__``) so the definition can never drift from the provider.
    ``SERVICE_DEFINITIONS`` remains the metadata source of truth during the
    migration -- the provider composes with it, it does not replace it.

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

    Services without config (publish-on-web, minio, redis, platform, shared
    postgresql-database, ...) leave ``config_model`` as ``None`` and inherit the
    no-op defaults.
    """

    #: The service this provider handles. Set by each concrete subclass.
    service_type: ClassVar[ServiceType]
    #: The existing metadata dataclass, bound automatically from service_type.
    definition: ClassVar[ServiceDefinition]

    #: Pydantic model for this service's config, or None if it takes no config.
    config_model: ClassVar[type[BaseModel] | None] = None
    #: Current config schema version (major.minor). Only meaningful with a config_model.
    config_schema_version: ClassVar[str] = "1.0"

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

    #: Order in the generic provisioning loop (RC-5 Phase 4); lower runs first. Only
    #: meaningful for providers that override ``provision``. The defaults on the four
    #: provisioning providers reproduce today's fixed db -> minio -> keycloak -> redis
    #: sequence.
    provision_order: ClassVar[int] = 100

    #: Manager key for server-side cleanup on removal (RC-5 Phase 5), or None if the
    #: service has no server-side resources to clean up. Resolved via
    #: RemovalContext.get_manager; replaces the _SERVICE_TYPE_MANAGER_ATTR map.
    cleanup_manager_key: ClassVar[str | None] = None

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

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # A concrete provider must declare which service it is; the definition is
        # then derived, not duplicated. Abstract intermediate subclasses (no
        # service_type) are allowed and simply skipped.
        service_type = cls.__dict__.get("service_type")
        if service_type is not None:
            cls.definition = ServiceAdapter.SERVICE_DEFINITIONS[service_type]

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
    # ``config_editables`` is the shared DATA (yaml_path + validator + default), used
    # by both the wizard and the API; ``config_form_section`` is the wizard/embed UI
    # view; ``config_api_fields`` the field names the API accepts. Defaults are empty,
    # so a service with no config, or one not yet migrated to owning its fields, keeps
    # working. Concrete providers import the forms building blocks lazily (inside the
    # method) so provider.py / registry.py stay free of forms imports at load time.

    def config_editables(self, layer: ConfigLayer) -> list[Editable]:
        """The DATA editables this service contributes at ``layer`` (default none)."""
        return []

    def config_form_section(self, layer: ConfigLayer) -> FormSection | None:
        """The wizard/edit config section this service contributes at ``layer``, or
        None. The forms layer sources its per-service sections from here instead of
        hand-authoring them."""
        return None

    def config_component_layout(self) -> list[Any]:
        """Layout node(s) this service HOOKS INTO the per-component form (default none).

        Component-level services (storage, metrics-scraper, ...) don't get a standalone
        wizard step -- their fields live inside the component definition. This is the
        hook point: the component form assembly collects each service's nodes and
        inserts them. (Display ordering across services is registry order for now; an
        explicit priority is a later refinement.)
        """
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
        ``registry.manifest_providers()`` uses this to build the contributor set.
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
