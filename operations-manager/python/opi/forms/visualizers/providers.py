"""The ``OptionsProvider`` protocol and the providers that fill a select or radio field."""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final, Protocol

from opi.core.authz.actions import Action
from opi.core.authz.engine import ROLE_DESCRIPTIONS, Role, role_label
from opi.core.cluster_config import CLUSTER_CONFIG, get_selectable_clusters
from opi.core.config import settings
from opi.forms.editables.service_path import smart_get_value
from opi.services.catalog.base import ConfigLayer, config_path, offers_component_checkbox
from opi.services.catalog.cross_domain_access.config_model import WILDCARD_PROJECT
from opi.services.catalog.image_registries.resolution import component_registry_name
from opi.services.catalog.image_registries.rules import normalize_image, normalize_prefix
from opi.services.catalog.keycloak.account_link import account_link_mode
from opi.services.catalog.keycloak.templates import allows_local_accounts, allows_sso
from opi.services.catalog.postgresql_database.connection_limit import (
    CONNECTION_LIMIT,
    CONNECTION_LIMIT_STEPS,
    project_connection_limit,
)
from opi.services.catalog.shared.noncurrent_versions import (
    MAX_NONCURRENT_VERSIONS,
    MAX_NONCURRENT_VERSIONS_STEPS,
    NONCURRENT_EXPIRE_DAYS,
    NONCURRENT_EXPIRE_DAYS_STEPS,
    project_max_noncurrent_versions,
    project_noncurrent_expire_days,
)
from opi.services.catalog.shared.storage import STORAGE_SIZES
from opi.services.project import Project
from opi.services.project_environment import DEFAULT_ENVIRONMENT, ENVIRONMENT_LABELS, Omgeving
from opi.services.project_roles import actor_may_set
from opi.services.services import ServiceAdapter, service_entry_name
from opi.services.services_enums import ServiceKind, ServiceType
from opi.utils.k8s_quantity import parse_k8s_memory_to_mi

if TYPE_CHECKING:
    from opi.services.catalog.config_settings import IntegerSetting

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OptionsSource:
    """Where a field's choices come from when they differ per project.

    The options of a form field are also the answer to "which values may I send here", and
    that answer belongs in the API documentation. A fixed list can be enumerated there. A
    list that comes out of the project itself (the components, a peer's deployments, the
    attachments in the catalogue) cannot: an enumeration would be a snapshot that is untrue
    for every other project. The honest answer is then the SOURCE, which is what this holds:
    where a client fetches the list itself and, without an endpoint for it, what the list
    depends on.
    """

    description: str
    """Wat de lijst is, in het Nederlands. Dit is de tekst die een lezer krijgt."""
    endpoint: str | None = None
    """Methode en pad van het endpoint dat de opties levert, of None als er geen is."""
    path: str | None = None
    """Waar in het antwoord van dat endpoint de waarden staan, bijvoorbeeld
    ``components[].name``. None als er geen endpoint is."""

    def as_json(self) -> dict[str, str]:
        """The machine-readable form; an empty field is left out rather than sent as null."""
        data = {"description": self.description}
        if self.endpoint:
            data["endpoint"] = self.endpoint
        if self.path:
            data["path"] = self.path
        return data


#: The "I will type a domain myself" switch in the base-domain select.
#:
#: Not a value but a SWITCH: in the form it turns on a second, transient field
#: (``deployments[*]/base-domain:custom``) that holds the real domain, and on save it is
#: replaced by that domain. It can therefore never be stored, and a write that carries it
#: anyway is refused ("Een aangepast domein is geselecteerd maar niet ingevuld",
#: ``DomainConfigEnforcer``).
#:
#: That is why it is named here: what the API publishes (``GET .../clusters``, and through
#: ``options_source`` the ``x-choices-source`` of the field) has to leave it out, because a
#: list of choices naming a value the deployment refuses sends every client the wrong way. An
#: API client sets its own domain by writing the domain name into ``base-domain`` directly.
CUSTOM_DOMAIN_SENTINEL: Final = "__custom__"

#: A provider that has not yet said whether its list is fixed or differs per project.
#:
#: Distinct from ``options_source = None``, which means the list is fixed: a provider that
#: declares nothing gets no list of choices in the API documentation, because what a
#: project-dependent provider yields without project context is not empty but WRONG: it shows
#: the few options that survive without context as if those were the allowed values.
UNDECLARED_SOURCE: Final = object()


class OptionsProvider(Protocol):
    """Fills a select or radio field with options from outside the form."""

    options_source: ClassVar[OptionsSource | None]
    """None als de lijst vastligt, een ``OptionsSource`` als hij per project verschilt.

    Elke provider die een service-configveld vult declareert dit, want de API-documentatie
    leest hem hier af (``opi/api/openapi_choices.py``); ``tests/test_openapi_config_choices.py``
    houdt daar iedereen aan. Een provider die alleen buiten de service-config gebruikt wordt
    hoeft niets te zeggen.
    """

    def get_options(self) -> list[dict[str, Any]]:
        """The options, each a dict with ``value`` and ``label``.

        An option may also carry ``description``, ``disabled``, ``group`` and the like.
        """
        ...


class ClusterOptionsProvider:
    """The deployment targets this environment offers, from the cluster configuration."""

    def __init__(self, include_empty: bool = False, empty_label: str = "Selecteer een cluster") -> None:
        self.include_empty = include_empty
        self.empty_label = empty_label

    def get_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []

        if self.include_empty:
            options.append({"value": "", "label": self.empty_label})

        cluster_labels = {
            "local": "Lokaal (Kind)",
            "odcn-staging": "Staging Cluster (ODC-Noord)",
            "odcn-production": "Productie Cluster (ODC-Noord)",
        }

        # Only the clusters this environment offers as a deployment target, driven by the
        # managing cluster's config and not by every cluster that happens to be defined.
        for cluster_name in get_selectable_clusters():
            label = cluster_labels.get(cluster_name, cluster_name.title())
            options.append(
                {
                    "value": cluster_name,
                    "label": label,
                }
            )

        return options


class ServiceOptionsProvider:
    """The services a project can enable, from the ServiceAdapter definitions."""

    def __init__(self, include_empty: bool = False, project_services: list[str] | None = None) -> None:
        """``project_services`` are the services the project ALREADY has, from the project file.

        Only a replaced service reads it: its card stays for a project that has it and is
        left out for one that has not.
        """
        self.include_empty = include_empty
        self.project_services = project_services or []

    def get_options(self) -> list[dict[str, Any]]:
        # Imported here, not at module scope: the registry imports the catalog, whose
        # services import this forms module back. The catalog breaks the same cycle the same
        # way (docs/development/services.md, "Keep the catalog import-light").
        from opi.services.registry import get_service

        options: list[dict[str, Any]] = []

        if self.include_empty:
            options.append({"value": "", "label": "Selecteer een service"})

        for service_type in ServiceType:
            definition = ServiceAdapter.get_service_definition(service_type)

            if definition.hidden or definition.kind is ServiceKind.SYSTEM:
                continue

            # A service this cluster cannot deliver is left out. The service answers that
            # itself from the cluster configuration, so no cluster name appears here, and the
            # managing cluster is the measure because an OPI instance only provisions its
            # own. This is presentation only: the refusal that counts is at save time, in
            # validate_service_availability, since the API and a hand-written project file
            # never see a card.
            service = get_service(service_type)
            if not service.available_on_cluster(settings.CLUSTER_MANAGER):
                continue

            # A replaced service is no longer a card for a project that does not have it, and
            # stays for one that runs it, which has to remain editable and switchable. The
            # refusal that counts is in validate_service_availability.
            replaced = service.replaced_on_cluster(settings.CLUSTER_MANAGER)
            if replaced and service_type.value not in self.project_services:
                continue

            option: dict[str, Any] = {
                "value": service_type.value,
                "label": definition.name,
                "description": definition.description,
                "icon": definition.icon,
                "color": definition.color,
            }

            if definition.requires:
                option["requires"] = definition.requires

            if definition.help_template:
                option["help_template"] = definition.help_template

            # From the service's own declaration and not from a list here, so a service that
            # starts requiring approval carries the warning by itself, on the card and in the
            # explanation. The question is whether ENABLING it is a request, not whether the
            # service has approvals at all: a decision about a config value (redis without a
            # key prefix) only applies at that value. The import sits inside because the
            # registry loads the service packages, which pull this module back in through the
            # forms layer.
            from opi.services.registry import get_service

            if get_service(service_type).enabling_needs_approval():
                option["requires_approval"] = True

            if replaced and service.replaced_by is not None:
                # The service NAME and not its display name: the notice on the card is a
                # short pill, and this is also what goes into the project file or the API.
                option["replaced_by"] = service.replaced_by.value

            options.append(option)

        return options


class ComponentTypeOptionsProvider:
    """Provides component type options (single, frontend, backend)."""

    def __init__(self, include_empty: bool = False) -> None:
        self.include_empty = include_empty

    def get_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []

        if self.include_empty:
            options.append({"value": "", "label": "Selecteer type"})

        options.extend(
            [
                {
                    "value": "single",
                    "label": "Single (All-in-one)",
                    "description": "Complete applicatie in een component",
                },
                {
                    "value": "frontend",
                    "label": "Frontend",
                    "description": "User interface component",
                },
                {
                    "value": "backend",
                    "label": "Backend",
                    "description": "API of service component",
                },
            ]
        )

        return options


class UserRoleOptionsProvider:
    """The roles a member can have, from the authorization register.

    Not a list of its own: the label and the description come from ``ROLE_LABELS`` and
    ``ROLE_DESCRIPTIONS``, so another name on the screen is a change in one place and the
    options can never show a role that has no rules.
    """

    def __init__(self, include_empty: bool = False) -> None:
        self.include_empty = include_empty

    def get_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        if self.include_empty:
            options.append({"value": "", "label": "Selecteer rol"})
        options.extend(
            {"value": role.value, "label": role_label(role), "description": ROLE_DESCRIPTIONS[role]} for role in Role
        )
        return options


class CpuRequestOptionsProvider:
    """Provides CPU request options for components."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "50m", "label": "50m (minimaal)"},
            {"value": "100m", "label": "100m"},
            {"value": "250m", "label": "250m"},
            {"value": "500m", "label": "500m"},
        ]


class CpuLimitOptionsProvider:
    """Provides CPU limit options for components."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "500m", "label": "500m"},
            {"value": "1", "label": "1 CPU"},
        ]


ALL_MEMORY_STEPS: list[tuple[str, str, int]] = [
    ("25Mi", "25 Mi", 25),
    ("32Mi", "32 Mi", 32),
    ("64Mi", "64 Mi", 64),
    ("96Mi", "96 Mi", 96),
    ("128Mi", "128 Mi", 128),
    ("256Mi", "256 Mi", 256),
    ("512Mi", "512 Mi", 512),
    ("768Mi", "768 Mi", 768),
    ("1Gi", "1 Gi", 1024),
    ("1536Mi", "1.5 Gi", 1536),
    ("2Gi", "2 Gi", 2048),
    ("2560Mi", "2.5 Gi", 2560),
    ("3Gi", "3 Gi", 3072),
    ("3584Mi", "3.5 Gi", 3584),
    ("4Gi", "4 Gi", 4096),
]


def get_memory_steps(max_mi: int | None = None) -> list[tuple[str, str]]:
    """Return memory steps up to the cluster's max_memory_limit_mi."""
    if max_mi is None:
        from opi.core.cluster_config import get_max_memory_limit_mi
        from opi.core.config import settings

        max_mi = get_max_memory_limit_mi(settings.CLUSTER_MANAGER)
    return [(v, lbl) for v, lbl, mi in ALL_MEMORY_STEPS if mi <= max_mi]


class MemoryOptionsProvider:
    """Provides memory options for components (used for limit fields).

    If *current_value* is set and not in the standard steps, it is inserted
    at the correct sorted position so the dropdown always contains the
    value currently stored in the project file (e.g. tuner-assigned values).
    """

    def __init__(self, current_value: str | None = None) -> None:
        self.current_value = current_value

    def _get_max_mi(self) -> int:
        from opi.core.cluster_config import get_max_memory_limit_mi
        from opi.core.config import settings

        return get_max_memory_limit_mi(settings.CLUSTER_MANAGER)

    def get_options(self) -> list[dict[str, Any]]:
        steps = get_memory_steps(max_mi=self._get_max_mi())
        options = [{"value": v, "label": lbl} for v, lbl in steps]

        if self.current_value and not any(o["value"] == self.current_value for o in options):
            from opi.utils.k8s_quantity import parse_k8s_memory_to_mi

            try:
                current_mi = parse_k8s_memory_to_mi(self.current_value)
            except ValueError:
                return options

            label = f"{int(current_mi)} Mi" if current_mi == int(current_mi) else f"{current_mi:.1f} Mi"
            new_option = {"value": self.current_value, "label": label}

            step_mis = [parse_k8s_memory_to_mi(v) for v, _ in steps]
            insert_idx = len(options)
            for i, step_mi in enumerate(step_mis):
                if current_mi < step_mi:
                    insert_idx = i
                    break
            options.insert(insert_idx, new_option)

        return options


class MemoryRequestOptionsProvider(MemoryOptionsProvider):
    """Provides memory options for request fields (capped lower than limits)."""

    def _get_max_mi(self) -> int:
        from opi.core.cluster_config import get_max_memory_request_mi
        from opi.core.config import settings

        return get_max_memory_request_mi(settings.CLUSTER_MANAGER)


class StorageTypeOptionsProvider:
    """Provides storage type options for container volumes."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {
                "value": "persistent",
                "label": "Persistent",
                "description": "Data blijft bewaard bij herstart van de container",
            },
            {
                "value": "ephemeral",
                "label": "Tijdelijk (ephemeral)",
                "description": "Data wordt gewist bij herstart van de container",
            },
        ]


class StorageSizeOptionsProvider:
    """The sizes the platform offers, plus the stored value when that is not among them.

    The series is a convenience and not the rule: ``check_storage_size`` accepts every
    quantity in the range, and a volume may not shrink. Without the stored value the select
    would offer a volume on ``3Gi`` nothing but shrinking, and the save then refuses the
    form's own proposal.
    """

    # The list is fixed: every project gets these choices.
    options_source: ClassVar[OptionsSource | None] = None

    #: Only the label per size. The sizes themselves live in ``SIZES``, so a list of choices
    #: cannot start promising something the API does not accept.
    LABELS: ClassVar[dict[str, str]] = {
        "50Mi": "50 MB",
        "100Mi": "100 MB",
        "250Mi": "250 MB",
        "500Mi": "500 MB",
        "1Gi": "1 GB",
        "2Gi": "2 GB",
        "5Gi": "5 GB",
        "10Gi": "10 GB",
        "20Gi": "20 GB",
        "50Gi": "50 GB",
    }

    #: The series this select offers. A subclass narrows it where the field has bounds of
    #: its own (see ``PostgresStorageSizeOptionsProvider``).
    SIZES: ClassVar[tuple[str, ...]] = STORAGE_SIZES

    def __init__(self, current_value: str | None = None) -> None:
        self.current_value = current_value

    def get_options(self) -> list[dict[str, Any]]:
        options = [{"value": size, "label": self.LABELS.get(size, size)} for size in self.SIZES]
        stored = (self.current_value or "").strip()
        if not stored or any(option["value"] == stored for option in options):
            return options
        options.append({"value": stored, "label": f"{stored} (eigen waarde)"})
        try:
            return sorted(options, key=lambda option: parse_k8s_memory_to_mi(str(option["value"])))
        except ValueError:
            # A stored size that cannot be read has no place in the series, but it does have
            # to stay selectable: otherwise the form submits a shrink.
            return options


class PostgresStorageSizeOptionsProvider(StorageSizeOptionsProvider):
    """The same select for a PostgreSQL volume, on the series that field's bounds allow.

    ``POSTGRES_STORAGE`` runs to 20Gi while the storage services now run to 50Gi, so the
    series is a subclass rather than the shared one: a select must not offer a size the
    declaration refuses.
    """

    SIZES: ClassVar[tuple[str, ...]] = ("50Mi", "100Mi", "250Mi", "500Mi", "1Gi")


class StorageSizeOverrideOptionsProvider(StorageSizeOptionsProvider):
    """The same select for ONE deployment's override, led by the inherit option.

    Empty means: follow the component. The size the component says is the placeholder the
    form shows, so the list itself does not have to name it.
    """

    def get_options(self) -> list[dict[str, Any]]:
        return [{"value": "", "label": "Zelfde als het component"}, *super().get_options()]


class IntegerStepOptionsProvider:
    """The steps of an ``IntegerSetting``, plus the stored value when that is not among them.

    The empty choice stores nothing, and what that means depends on the layer: on the project
    the platform default, on a deployment the project's value.

    The steps are a convenience; the limit itself is in the declaration, and that is what
    validates.
    """

    options_source: ClassVar[OptionsSource | None] = None
    setting: ClassVar[IntegerSetting]
    steps: ClassVar[tuple[int, ...]]

    def __init__(self, current_value: str | None = None, yaml_data: dict[str, Any] | None = None) -> None:
        self.current_value = current_value
        self.yaml_data = yaml_data or {}

    def empty_label(self) -> str:
        default = self.setting.default
        return "Geen grens" if default is None else f"Standaard van het platform ({default})"

    def get_options(self) -> list[dict[str, Any]]:
        steps = list(self.steps)
        custom = int(self.current_value) if self.current_value and self.current_value.isdigit() else None
        if custom is not None and custom not in steps:
            steps = sorted([*steps, custom])
        options = [{"value": "", "label": self.empty_label()}]
        for step in steps:
            label = str(step) if step in self.steps else f"{step} (eigen waarde)"
            options.append({"value": str(step), "label": label})
        return options


class ConnectionLimitOptionsProvider(IntegerStepOptionsProvider):
    """The connection limit of the project database."""

    setting: ClassVar[IntegerSetting] = CONNECTION_LIMIT
    steps: ClassVar[tuple[int, ...]] = CONNECTION_LIMIT_STEPS


class DeploymentConnectionLimitOptionsProvider(ConnectionLimitOptionsProvider):
    """The same list on a deployment, where the empty choice follows the project."""

    def empty_label(self) -> str:
        return f"Volg het project ({project_connection_limit(self.yaml_data)})"


class MaxNoncurrentVersionsOptionsProvider(IntegerStepOptionsProvider):
    """How many old versions a bucket with versioning keeps.

    On the project layer the empty choice is the platform default, and both object-storage
    services share that declaration, so the service plays no part here.
    """

    setting: ClassVar[IntegerSetting] = MAX_NONCURRENT_VERSIONS
    steps: ClassVar[tuple[int, ...]] = MAX_NONCURRENT_VERSIONS_STEPS


class NoncurrentExpireDaysOptionsProvider(IntegerStepOptionsProvider):
    """The age limit on old versions, on top of their number."""

    setting: ClassVar[IntegerSetting] = NONCURRENT_EXPIRE_DAYS
    steps: ClassVar[tuple[int, ...]] = NONCURRENT_EXPIRE_DAYS_STEPS


class DeploymentMaxNoncurrentVersionsOptionsProvider(MaxNoncurrentVersionsOptionsProvider):
    """On a deployment the empty choice follows the project, which is per service."""

    service: ClassVar[ServiceType]

    def empty_label(self) -> str:
        return f"Volg het project ({project_max_noncurrent_versions(self.yaml_data, self.service)})"


class DeploymentNoncurrentExpireDaysOptionsProvider(NoncurrentExpireDaysOptionsProvider):
    service: ClassVar[ServiceType]

    def empty_label(self) -> str:
        days = project_noncurrent_expire_days(self.yaml_data, self.service)
        return "Volg het project (geen grens)" if days is None else f"Volg het project ({days} dagen)"


class MinioDeploymentMaxNoncurrentVersionsOptionsProvider(DeploymentMaxNoncurrentVersionsOptionsProvider):
    service: ClassVar[ServiceType] = ServiceType.MINIO_STORAGE


class MinioDeploymentNoncurrentExpireDaysOptionsProvider(DeploymentNoncurrentExpireDaysOptionsProvider):
    service: ClassVar[ServiceType] = ServiceType.MINIO_STORAGE


class SeaweedfsDeploymentMaxNoncurrentVersionsOptionsProvider(DeploymentMaxNoncurrentVersionsOptionsProvider):
    service: ClassVar[ServiceType] = ServiceType.SEAWEEDFS_STORAGE


class SeaweedfsDeploymentNoncurrentExpireDaysOptionsProvider(DeploymentNoncurrentExpireDaysOptionsProvider):
    service: ClassVar[ServiceType] = ServiceType.SEAWEEDFS_STORAGE


class KeycloakTemplateOptionsProvider:
    """The realm blueprints, named after what a user gets rather than after the file.

    What actually differs is the LOGIN SCREEN, and only that. ``sso-only`` points the realm's
    browser flow at "External IDP Redirector", so whoever opens the application travels
    straight on to SSO Rijk and never sees a screen of Keycloak's own. ``sso-support`` keeps
    the standard browser flow, so Keycloak asks the question: continue with SSO Rijk, or sign
    in with a local account of this realm.

    BOTH blueprints keep ``registrationAllowed``, ``loginWithEmailAllowed`` and
    ``resetPasswordAllowed`` off, so neither offers self-registration or a password reset.
    Local accounts do exist under ``sso-support``, created by OPI's invite flow; a user of
    ``sso-only`` never reaches a screen where one could be typed in, which is what really
    excludes them. ``InviteAuthMethodOptionsProvider`` reads the same difference for the
    invite auth methods.

    ``sso-support-otp`` extends ``sso-support`` with a second factor on the local password path:
    a password login requires an OTP code and configures the authenticator app on first login.
    SSO Rijk logins bypass the password step entirely and never see the OTP requirement.
    """

    # Options are fixed: every project gets the same choices.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {
                "value": "sso-only",
                "label": "Alleen SSO Rijk",
                "description": (
                    "Wie de applicatie opent gaat meteen door naar SSO Rijk. Keycloak toont geen eigen "
                    "inlogscherm, dus een lokaal account is geen weg naar binnen."
                ),
            },
            {
                "value": "sso-support",
                "label": "SSO Rijk of een lokaal account",
                "description": (
                    "Het inlogscherm van Keycloak laat de keuze: doorgaan met SSO Rijk, of inloggen met een "
                    "lokaal account uit het realm van dit project. Lokale accounts maak je met uitnodigingen; "
                    "aanmelden kan niemand zichzelf."
                ),
            },
            {
                "value": "sso-support-otp",
                "label": "SSO Rijk of een lokaal account, met verplichte OTP",
                "description": (
                    "Zelfde keuze als hierboven, maar een lokaal account geeft naast zijn wachtwoord een code "
                    "uit een authenticator-app. Bij de eerste login stelt het die app in. Wie met SSO Rijk "
                    "inlogt merkt er niets van."
                ),
            },
            {
                "value": "local-only",
                "label": "Alleen lokale accounts",
                "description": (
                    "Geen SSO Rijk. Alleen lokale accounts uit het realm van dit project kunnen inloggen. "
                    "Accounts worden aangeleverd of komen via een uitnodiging; aanmelden, een wachtwoord "
                    "herstellen of het eigen profiel beheren kan niemand zelf. Wie eerder via SSO Rijk "
                    "binnenkwam, heeft na deze keuze geen weg naar binnen tot hij een lokaal account krijgt."
                ),
            },
        ]


class PullPolicyOptionsProvider:
    """Provides Kubernetes image pull policy options."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "Always", "label": "Always"},
            {"value": "IfNotPresent", "label": "IfNotPresent"},
            {"value": "Never", "label": "Never"},
        ]


class EnvironmentOptionsProvider:
    """The OTAP marking of a deployment: the four fixed phases.

    Development is the default and carries the empty value, so a deployment without a marking
    is development and that default need not sit in every file. A file that does carry
    ``ontwikkeling`` explicitly falls back to that empty choice in the list.
    """

    # The values are fixed, so an enumeration is the right answer.
    options_source: ClassVar[OptionsSource | None] = None

    def __init__(self, yaml_data: dict[str, Any] | None = None, current_value: str | None = None) -> None:
        self.yaml_data = yaml_data or {}
        self.current_value = current_value

    def get_options(self) -> list[dict[str, Any]]:
        # Moving to or away from production is only for whoever may set the marking
        # (environment.set); otherwise the save refuses it only afterwards. The current value
        # always stays selectable.
        may_set = actor_may_set(Action.ENVIRONMENT_SET, self.yaml_data, environment=Omgeving.PRODUCTIE)
        in_production = self.current_value == Omgeving.PRODUCTIE.value

        def option(value: str, label: str, *, production: bool) -> dict[str, Any]:
            if may_set or production == in_production:
                return {"value": value, "label": label}
            return {"value": value, "label": f"{label} (alleen ops)", "disabled": True}

        # ``.value``, because every other provider yields a plain string and an enum member
        # that ends up as a value breaks the YAML dumper.
        return [
            option("", ENVIRONMENT_LABELS[DEFAULT_ENVIRONMENT], production=False),
            *(
                option(value.value, label, production=value is Omgeving.PRODUCTIE)
                for value, label in ENVIRONMENT_LABELS.items()
                if value is not DEFAULT_ENVIRONMENT
            ),
        ]


class BaseDomainOptionsProvider:
    """Provides base domain options for deployment URLs."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "", "label": "Standaard (clusternaam)"},
            {"value": "rijksapp.nl", "label": "rijksapp.nl"},
            {"value": CUSTOM_DOMAIN_SENTINEL, "label": "Eigen domein..."},
        ]


class ClusterBaseDomainOptionsProvider:
    """Provides the base domain options the selected cluster offers, from CLUSTER_CONFIG."""

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "De domeinen die het cluster van deze deployment aanbiedt. Leeg betekent het "
            "standaarddomein van het cluster. Dit is geen gesloten verzameling: een eigen "
            "domein zet je door de domeinnaam zelf in dit veld te schrijven, en "
            "'custom-domain-certificates' in hetzelfde antwoord zegt of "
            "dit cluster daar een certificaat voor kan uitgeven. Een adres op een eigen domein "
            "is de combinatie met een subdomein-format plus 'subdomain' (mijn.domein.nl = "
            "base-domain 'domein.nl' + subdomain 'mijn'); alleen het kale domein zelf gaat via "
            "'expose-component-on-bare-domain'."
        ),
        endpoint="GET /api/v2/projects/{project_name}/clusters",
        path="clusters[].base-domains[].value",
    )

    def __init__(self, cluster: str | None = None) -> None:
        self.cluster = cluster

    def get_options(self) -> list[dict[str, Any]]:
        """The base domain options of *cluster*, or of the cluster this OPI instance manages."""
        from opi.core.config import settings

        def _extract_domain(entry: str | dict[str, Any]) -> str:
            return entry["domain"] if isinstance(entry, dict) else entry

        cluster = self.cluster or settings.CLUSTER_MANAGER
        if cluster and cluster in CLUSTER_CONFIG:
            postfix = CLUSTER_CONFIG[cluster].get("ingress_postfix", "")
            default_label = f"Cluster standaard ({postfix.lstrip('.')})" if postfix else "Cluster standaard"
            options: list[dict[str, Any]] = [{"value": "", "label": default_label}]

            raw = CLUSTER_CONFIG[cluster].get("domains", {}).get("supported_domains", [])
            domains = [_extract_domain(d) for d in raw]
            options.extend({"value": d, "label": d} for d in domains)
            options.append({"value": CUSTOM_DOMAIN_SENTINEL, "label": "Eigen domein..."})
            return options

        return [
            {"value": "", "label": "Cluster standaard"},
            {"value": CUSTOM_DOMAIN_SENTINEL, "label": "Eigen domein..."},
        ]


class FilteredServiceOptionsProvider:
    """The services the project has enabled, for the component ``services`` checkbox group."""

    def __init__(self, project_services: list[str] | None = None) -> None:
        self.project_services = project_services or []

    def get_options(self) -> list[dict[str, Any]]:
        # Imported here: the registry loads the service catalog, which reads ``opi.forms``
        # and with it this module.
        from opi.services.registry import get_service

        options: list[dict[str, Any]] = []
        for service_type in ServiceType:
            if service_type.value not in self.project_services:
                continue
            if not offers_component_checkbox(get_service(service_type)):
                continue
            definition = ServiceAdapter.get_service_definition(service_type)
            options.append(
                {
                    "value": service_type.value,
                    "label": definition.name,
                    "description": definition.description,
                    "icon": definition.icon,
                    "color": definition.color,
                }
            )
        return options


class ComponentReferenceOptionsProvider:
    """The project's component names, for a deployment's component reference select."""

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description="De componenten van dit project.",
        endpoint="GET /api/v2/projects/{project_name}/components",
        path="components[].name",
    )

    def __init__(
        self,
        component_names: list[str] | None = None,
        include_empty: bool = False,
        empty_label: str = "Geen root component",
        exclude_references: list[str] | None = None,
    ) -> None:
        self.component_names = component_names or []
        self.include_empty = include_empty
        self.empty_label = empty_label
        self.exclude_references = set(exclude_references or [])

    def get_options(self) -> list[dict[str, Any]]:
        """The component names, minus the references the other rows already took."""
        options: list[dict[str, Any]] = []
        if self.include_empty:
            options.append({"value": "", "label": self.empty_label})
        options.extend(
            {"value": name, "label": name} for name in self.component_names if name not in self.exclude_references
        )
        return options


class RootComponentOptionsProvider(ComponentReferenceOptionsProvider):
    """ComponentReferenceOptionsProvider with an empty 'no root' option."""

    def __init__(self, component_names: list[str] | None = None) -> None:
        super().__init__(component_names=component_names, include_empty=True)


class BareDomainComponentOptionsProvider(ComponentReferenceOptionsProvider):
    """ComponentReferenceOptionsProvider for bare domain component selection.

    Shows component names with an empty 'not on bare domain' option.
    """

    def __init__(self, component_names: list[str] | None = None) -> None:
        super().__init__(
            component_names=component_names,
            include_empty=True,
            empty_label="Niet bereikbaar op kaal domein",
        )


class BackupScheduleFrequencyOptionsProvider:
    """Provides RRULE frequency options for the backup schedule select."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "", "label": "Geen"},
            {"value": "DAILY", "label": "Dagelijks"},
            {"value": "WEEKLY", "label": "Wekelijks"},
            {"value": "MONTHLY", "label": "Maandelijks"},
        ]


class BackupScheduleTimeOptionsProvider:
    """Provides half-hour time slots for the backup time indication."""

    def get_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        for hour in range(24):
            for minute in (0, 30):
                time_str = f"{hour:02d}:{minute:02d}"
                options.append({"value": time_str, "label": time_str})
        return options


class BackupScheduleDayOptionsProvider:
    """Provides day-of-week options for weekly schedules."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "MO", "label": "Maandag"},
            {"value": "TU", "label": "Dinsdag"},
            {"value": "WE", "label": "Woensdag"},
            {"value": "TH", "label": "Donderdag"},
            {"value": "FR", "label": "Vrijdag"},
            {"value": "SA", "label": "Zaterdag"},
            {"value": "SU", "label": "Zondag"},
        ]


class BackupScheduleMonthDayOptionsProvider:
    """Provides day-of-month options for monthly schedules."""

    def get_options(self) -> list[dict[str, Any]]:
        return [{"value": str(day), "label": str(day)} for day in range(1, 29)]


class BackupResourceTypesOptionsProvider:
    """Provides resource type options filtered by what the deployment actually uses.

    Uses ``yaml_data`` and ``yaml_path`` (passed generically by the bridge) to
    determine which deployment is being edited, then checks which backup-capable
    services that deployment uses via ``deployment_uses_service``.

    Works for both:
    - Schedule modal: path ``deployments[N]/backup/resource_types`` → deployment at index N
    - Manual backup: path ``resource_types`` → uses ``_cluster_deployments`` context
    """

    def __init__(
        self,
        yaml_data: dict[str, Any] | None = None,
        yaml_path: str | None = None,
    ) -> None:
        self._yaml_data = yaml_data or {}
        self._yaml_path = yaml_path or ""

    def get_options(self) -> list[dict[str, Any]]:
        from opi.services import ServiceAdapter
        from opi.services.project_file_handler import create_project_file_handler

        all_labels = ServiceAdapter.get_backupable_labels()
        deployment_name = self._resolve_deployment_name()
        if not deployment_name:
            return [{"value": bl["label"], "label": bl["name"]} for bl in all_labels]

        pfh = create_project_file_handler()
        filtered = pfh.get_deployment_backup_labels(self._yaml_data, deployment_name)
        return [{"value": bl["label"], "label": bl["name"]} for bl in filtered]

    def _resolve_deployment_name(self) -> str:
        """Determine the deployment name from the path or form data."""
        # Schedule modal: deployments[N]/backup/resource_types
        match = re.match(r"deployments\[(\d+)]", self._yaml_path)
        if match:
            idx = int(match.group(1))
            deployments = self._yaml_data.get("deployments", [])
            if isinstance(deployments, list) and idx < len(deployments):
                dep = deployments[idx]
                if isinstance(dep, dict):
                    return dep.get("name", "")
            return ""

        # Manual backup: selected deployment_name in form data
        selected = self._yaml_data.get("deployment_name", "")
        if selected:
            return str(selected)

        return ""


class BackupDeploymentOptionsProvider:
    """Provides deployment options for the manual backup modal.

    Reads ``_cluster_deployments`` from ``yaml_data`` (set by
    ``_build_backup_restore_context_async`` via base_data merge).
    """

    def __init__(self, yaml_data: dict[str, Any] | None = None) -> None:
        data = yaml_data or {}
        self._deployments: list[dict[str, Any]] = data.get("_cluster_deployments", [])

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": dep["name"], "label": f"{dep['name']} ({dep.get('namespace', '')})"}
            for dep in self._deployments
            if dep.get("name")
        ]


class DeploymentCloneFromOptionsProvider:
    """Provides existing deployment names as clone-from options.

    Used when adding a new deployment to select a source deployment
    to clone data (databases, storage) from.
    """

    def __init__(self, deployment_names: list[str] | None = None) -> None:
        self.deployment_names = deployment_names or []

    def get_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = [{"value": "", "label": "Niet klonen"}]
        options.extend({"value": name, "label": name} for name in self.deployment_names)
        return options


class RestoreConfigSourceOptionsProvider(DeploymentCloneFromOptionsProvider):
    """Provides the deployments a restore can take its configuration from.

    Same list as the clone-from picker, with an empty option that says what empty means
    here: a restore always clones the backup, so "not cloning" is not the choice.
    """

    def get_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = [{"value": "", "label": "Geen (lege deployment)"}]
        options.extend({"value": name, "label": name} for name in self.deployment_names)
        return options


class RepositoryOptionsProvider:
    """The project's repository names, for a deployment's repository select."""

    def __init__(self, repository_names: list[str] | None = None) -> None:
        self.repository_names = repository_names or []

    def get_options(self) -> list[dict[str, Any]]:
        return [{"value": name, "label": name} for name in self.repository_names]


class DomainFormatOptionsProvider:
    """Provides domain-format template options filtered by base_domain capabilities.

    Always shows dash-separated formats. When the selected base_domain supports
    dot-separated hostnames, the dot variants are shown as well.
    Options are sorted alphabetically by value.
    """

    _DASH_FORMATS: ClassVar[list[str]] = [
        "component-deployment-project",
        "component-deployment-subdomain",
        "component-subdomain",
        "deployment-project",
        "deployment-subdomain",
        "subdomain",
    ]

    _DOT_FORMATS: ClassVar[list[str]] = [
        "component.deployment.project",
        "component.deployment.subdomain",
        "component.subdomain",
        "deployment.project",
        "deployment.subdomain",
    ]

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "Hangt af van het gekozen base-domain: de streepjes-varianten kunnen altijd, de "
            "punt-varianten alleen als dat domein losse subdomeinen met punten ondersteunt. "
            "Welk domein dat kan staat als 'supports-dots' bij het domein in de clusterlijst."
        ),
        endpoint="GET /api/v2/projects/{project_name}/clusters",
        path="clusters[].base-domains[].supports-dots",
    )

    def __init__(self, base_domain: str | None = None, cluster: str | None = None) -> None:
        self.base_domain = base_domain
        self.cluster = cluster

    def get_options(self) -> list[dict[str, Any]]:
        import logging

        from opi.core.cluster_config import get_domain_supports_dots
        from opi.core.config import settings

        logger = logging.getLogger(__name__)
        cluster = self.cluster or settings.CLUSTER_MANAGER
        supports_dots = False

        logger.debug(f"DomainFormatOptionsProvider.get_options(): base_domain={self.base_domain!r}, cluster={cluster}")

        if self.base_domain == "__custom__":
            supports_dots = True
            logger.debug("Custom domain selected, supports_dots=True")
        elif self.base_domain and cluster:
            supports_dots = get_domain_supports_dots(cluster, self.base_domain)
            logger.debug(f"Domain {self.base_domain!r} supports_dots={supports_dots}")
        else:
            logger.debug(f"base_domain={self.base_domain!r}, cluster={cluster}, no supports_dots check")

        format_ids = list(self._DASH_FORMATS)
        if supports_dots:
            format_ids.extend(self._DOT_FORMATS)

        format_ids.sort()
        logger.debug(f"DomainFormatOptionsProvider returning {len(format_ids)} formats: {format_ids}")
        return [{"value": f, "label": f"{f}.domein"} for f in format_ids]


class DeploymentSelectOptionsProvider:
    """Provides deployment names as checkbox options.

    Used when adding a component to select which deployments should
    receive a reference to the new component.
    """

    def __init__(self, deployment_names: list[str] | None = None) -> None:
        self.deployment_names = deployment_names or []

    def get_options(self) -> list[dict[str, Any]]:
        return [{"value": name, "label": name} for name in self.deployment_names]


class ApprovalStatusOptionsProvider:
    """Provides status options for the admin domain/subdomain approval flow."""

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "skip", "label": "Niet wijzigen"},
            {"value": "approved", "label": "Goedkeuren"},
            {"value": "denied", "label": "Afwijzen"},
        ]


class AttachmentOptionsProvider:
    """Provides the ids of attachments in the project-level attachments catalog as options."""

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "De ids van de bijlagen in de catalogus van dit project, dus wat er op het "
            "project-niveau van de attachments-service staat."
        ),
        endpoint="GET /api/v2/projects/{project_name}/services/attachments/config",
        path="[target=project].config",
    )

    def __init__(self, yaml_data: dict[str, Any] | None = None) -> None:
        self._yaml_data = yaml_data or {}

    def get_options(self) -> list[dict[str, Any]]:
        from opi.services.project_file_handler import extract_attachment_catalog

        catalog = extract_attachment_catalog(self._yaml_data)
        if not catalog:
            return [{"value": "", "label": "Geen bijlagen geüpload: upload eerst op de Bijlagen-sectie"}]
        # Lead with an empty placeholder so a freshly added row does not silently
        # default to the first catalog entry (an untouched select otherwise submits
        # the first option, producing a duplicate coupling with an empty path).
        return [
            {"value": "", "label": "-- Kies een bijlage --"},
            *(
                {
                    "value": entry["id"],
                    "label": f"{entry['id']} ({entry['filename']})" if entry.get("filename") else entry["id"],
                }
                for entry in catalog.values()
            ),
        ]


class AttachmentProvideAsOptionsProvider:
    """Static options for how an attachment is delivered into the pod."""

    # The list is fixed: every project gets these choices.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "file", "label": "Als bestand (gemount op een pad)"},
            {"value": "env-var", "label": "Als waarde van een env-var (alleen tekst)"},
        ]


_PUBLISH_TLS_MODE_OPTIONS = [
    {"value": "standard", "label": "Standaard certificaat (platform regelt het)"},
    {"value": "passthrough", "label": "Eigen certificaat op de pod (passthrough)"},
    {"value": "provided", "label": "Eigen certificaat op de ingress (aangeleverd)"},
]

#: What 'provided' is called while the project has no attachment to provide.
#: ``PublishOnWebComponentConfig`` refuses ``tls: provided`` without an ``attachment``, and
#: with an empty catalogue the attachment field next to it has nothing to pick, so choosing
#: the mode leads to a screen that can neither be saved nor undone. The option STAYS and is
#: disabled: whoever looks for it finds it with the reason, where an option that silently
#: disappears is only a second riddle.
_PROVIDED_WITHOUT_CERTIFICATE = "Eigen certificaat op de ingress - upload eerst een certificaat bij Bijlagen"


def publish_tls_mode_options(yaml_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The three TLS modes, with 'provided' disabled while there is no attachment.

    One helper for both layers, the component and the per-deployment override: a mode that
    cannot be chosen on the component may not be choosable on the override either.

    Without a project (a bare render, an example) everything stays, the same choice as the
    inherit label of ``PublishTlsOverrideOptionsProvider`` makes: no data is no reason to
    guess, and concluding an empty catalogue from a missing context would disable the mode on
    a screen that knows nothing about it.
    """
    # Imported here, as in AttachmentOptionsProvider below: project_file_handler imports this
    # module back through the forms layer.
    from opi.services.project_file_handler import extract_attachment_catalog

    can_provide = not yaml_data or bool(extract_attachment_catalog(yaml_data))
    return [
        {**option, "label": _PROVIDED_WITHOUT_CERTIFICATE, "disabled": True}
        if option["value"] == "provided" and not can_provide
        else dict(option)
        for option in _PUBLISH_TLS_MODE_OPTIONS
    ]


class PublishTlsModeOptionsProvider:
    """Options for how TLS is handled on a published component.

    ``yaml_data`` is handed to every provider that accepts it (see
    ``bridge._resolve_options``); without a project the list falls back to all three modes.
    """

    # The VALUES are fixed: every project gets these three. Only whether 'provided' can be
    # chosen depends on the attachment catalogue, which changes nothing about what the API
    # accepts.
    options_source: ClassVar[OptionsSource | None] = None

    def __init__(self, yaml_data: dict[str, Any] | None = None) -> None:
        self._yaml_data = yaml_data or {}

    def get_options(self) -> list[dict[str, Any]]:
        return publish_tls_mode_options(self._yaml_data)


class PublishTlsOverrideOptionsProvider:
    """TLS mode options for a per-deployment override. The empty value means
    'inherit' (no override): fall back to the component/root setting.

    The inherit option NAMES the mode it would fall back to, resolved from the project the
    form is editing. Without it an empty select is ambiguous in the one way that matters
    here -- "this deployment has no TLS" reads the same as "this deployment uses whatever
    the component uses" -- and the reader cannot tell whether they are looking at a setting
    or at an inheritance before they change it. ``yaml_data`` and ``yaml_path`` are handed
    to every provider that accepts them (see ``bridge._resolve_options``); without them the
    option falls back to the plain wording rather than guessing.
    """

    # No source: the values are fixed, the same three modes as on the component plus empty
    # for inheriting. Only the LABEL of the empty choice is derived from the project, and a
    # label changes nothing about what may be sent.
    options_source: ClassVar[OptionsSource | None] = None

    def __init__(self, yaml_data: dict[str, Any] | None = None, yaml_path: str | None = None) -> None:
        self._yaml_data = yaml_data or {}
        self._yaml_path = yaml_path or ""

    def _inherited_label(self) -> str:
        """What the component (or the project default) says, as an option label."""
        from opi.forms.editables.path import get_value
        from opi.services.project_file_handler import ProjectFileHandler

        # ".../components[0]/services/publish-on-web/config/tls" -> the row's component.
        row, _, _ = self._yaml_path.partition("/services/")
        component_name = get_value(self._yaml_data, f"{row}/reference") if row else None
        if not isinstance(component_name, str) or not component_name:
            return "Erven (geen override)"

        # Deliberately resolved WITHOUT a deployment name: the question is what this
        # override would fall back to, which is the component level and below.
        mode = ProjectFileHandler().extract_component_publish_on_web_tls(self._yaml_data, component_name)
        labels = {opt["value"]: opt["label"] for opt in _PUBLISH_TLS_MODE_OPTIONS}
        return f"Erven van het component: {labels.get(mode, mode)}"

    def get_options(self) -> list[dict[str, Any]]:
        return [{"value": "", "label": self._inherited_label()}, *publish_tls_mode_options(self._yaml_data)]


class YesNoOptionsProvider:
    """Ja/Nee options for boolean config fields (stored as an explicit YAML boolean)."""

    # The list is fixed: every project gets these choices.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "true", "label": "Ja"},
            {"value": "false", "label": "Nee"},
        ]


class WakeModeOptionsProvider:
    """The three sleep-mode wake modes: how a sleeping deployment is woken."""

    # The list is fixed: every project gets these choices.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {
                "value": "auto",
                "label": "Automatisch",
                "description": "Wekt bij het eerste bezoek; de bezoeker ziet een laadpagina",
            },
            {
                "value": "confirm",
                "label": "Met bevestiging",
                "description": "De bezoeker ziet een knop om de applicatie zelf te starten",
            },
            {
                "value": "manual",
                "label": "Alleen handmatig",
                "description": "Uitlegpagina zonder knop; alleen een beheerder wekt via de UI of API",
            },
        ]


class SleepAfterDeployOptionsProvider:
    """How long after a deploy a deployment may go to sleep.

    The list itself belongs to sleep-mode, which also decides which extra choices a
    cluster offers (the sandbox has a five-minute one so a sleep/wake cycle fits inside
    a test run). This only asks.
    """

    # The list is fixed per cluster: it does not depend on the project, only on the cluster
    # this OPI manages, which is also the cluster serving this documentation.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        from opi.core.config import settings
        from opi.services.catalog.sleep_mode.options import sleep_after_deploy_options

        return sleep_after_deploy_options(settings.CLUSTER_MANAGER)


class SleepAfterWakeOptionsProvider:
    """How long a woken deployment stays awake before its deadline is set again."""

    # The list is fixed per cluster: it does not depend on the project, only on the cluster
    # this OPI manages, which is also the cluster serving this documentation.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        from opi.core.config import settings
        from opi.services.catalog.sleep_mode.options import sleep_after_wake_options

        return sleep_after_wake_options(settings.CLUSTER_MANAGER)


class WakerComponentOptionsProvider:
    """The project's components, for picking which one serves the waker page.

    Reads the components from the surrounding form data (``yaml_data``), so it is
    populated in the edit flow and empty (only the auto option) in the create wizard,
    where components are not defined yet. Empty = let sleep-mode pick automatically.
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "De componenten van dit project. Laat het veld weg om sleep-mode zelf te laten "
            "kiezen; dan bedient de wekker het root-component van de deployment."
        ),
        endpoint="GET /api/v2/projects/{project_name}/components",
        path="components[].name",
    )

    def __init__(self, yaml_data: dict[str, Any] | None = None) -> None:
        self.yaml_data = yaml_data or {}

    def get_options(self) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = [{"value": "", "label": "Automatisch"}]
        for component in self.yaml_data.get("components", []) or []:
            name = component.get("name") if isinstance(component, dict) else None
            if name:
                options.append({"value": name, "label": name})
        return options


#: The non-value on a component: nothing in the file.
AUTOMATIC_REGISTRY_LABEL = "Automatisch: je eigen registry als de image eronder valt, anders publiek"

#: The same non-value on a DEPLOYMENT component, where empty means something else: not
#: "automatic" but "whatever the component itself chose".
INHERIT_REGISTRY_LABEL = "Zoals het component (geen afwijking)"


class ImageRegistryOptionsProvider:
    """The registries this project declared itself, to point a component at.

    Through ``smart_get_value``, because in the wizard the config sits under the virtual
    ``_services-config`` root. A stored value that no longer exists stays as a flagged option,
    or the next save falls back to the first option.

    An empty list makes the field disappear (``hidden_without_options``).
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description="De registries uit de projectconfig van de dienst image-registries.",
        endpoint="GET /api/v2/projects/{project_name}/services/image-registries/config",
        path="[target=project].config.registries[].name",
    )

    def __init__(
        self,
        yaml_data: dict[str, Any] | None = None,
        row_data: dict[str, Any] | None = None,
        yaml_path: str | None = None,
        current_value: Any = None,  # noqa: ANN401  # a project-file value
    ) -> None:
        self._yaml_data = yaml_data or {}
        self._row_data = row_data or {}
        self._yaml_path = yaml_path
        self._current_value = current_value

    def get_options(self) -> list[dict[str, Any]]:
        entries = self._entries()

        # Pre-selected as soon as the image prefix matches exactly ONE registry; with two
        # matches the choice is the point.
        image = self._component_image()
        if image:
            normalized = normalize_image(image)
            matching = [
                entry
                for entry in entries
                if (prefix := normalize_prefix(str(entry.get("upstream", ""))))
                and (normalized == prefix or normalized.startswith(prefix + "/"))
            ]
            if len(matching) == 1:
                entries = [matching[0], *(e for e in entries if e is not matching[0])]

        names = [entry["name"] for entry in entries]
        if not names and not self._current_value:
            return []

        # The LABEL on the screen and the slug as the value: a team that called its registry
        # "Code Overheid" should see that back, even though the file refers to it by slug.
        options = [{"value": "", "label": self._empty_label()}]
        options.extend({"value": name, "label": self._label_for(name)} for name in names)
        if self._current_value and self._current_value not in names:
            options.append({"value": self._current_value, "label": f"{self._current_value} (bestaat niet meer)"})
        return options

    def _entries(self) -> list[dict[str, Any]]:
        """The registries this project declared, in file order."""
        registries = (
            smart_get_value(
                self._yaml_data,
                config_path(ConfigLayer.PROJECT, ServiceType.IMAGE_REGISTRIES, "config", "registries"),
            )
            or []
        )
        return [r for r in registries if isinstance(r, dict) and r.get("name")]

    def _empty_label(self) -> str:
        """What "nothing chosen" means on this layer, derived from the rendered path."""
        if (self._yaml_path or "").startswith("deployments"):
            return INHERIT_REGISTRY_LABEL
        deviations = self._deployment_deviations()
        if not deviations:
            return AUTOMATIC_REGISTRY_LABEL
        plural = "deployments" if len(deviations) > 1 else "deployment"
        return f"{AUTOMATIC_REGISTRY_LABEL}. Let op: {', '.join(deviations)}. Pas die aan in de betreffende {plural}"

    def _deployment_deviations(self) -> list[str]:
        """Which deployments pick a registry for THIS component themselves.

        The component form does not show the deployment layer, so a choice made there leaves
        this field on "Automatisch" while the image does come from a registry, and the
        consumer sees nothing to change here.
        """
        component = self._component_name()
        if self._current_value or not component:
            return []
        deviations: list[str] = []
        for deployment in self._yaml_data.get("deployments") or []:
            if not isinstance(deployment, dict):
                continue
            for entry in deployment.get("components") or []:
                if not isinstance(entry, dict) or entry.get("reference") != component:
                    continue
                name = component_registry_name(entry)
                if name:
                    deviations.append(
                        f"deployment '{deployment.get('name', 'onbekend')}' gebruikt voor dit "
                        f"component registry '{self._label_for(name)}'"
                    )
        return deviations

    def _label_for(self, name: str) -> str:
        """The registry as the team named it, or its slug when there is no label."""
        for entry in self._entries():
            if entry.get("name") == name:
                return str(entry.get("display-name") or name)
        return name

    def _component_name(self) -> str:
        """The component this field sits on, or "", the same two ways as the image."""
        name = self._row_data.get("name")
        if isinstance(name, str) and name:
            return name
        match = re.search(r"components\[(\d+)\]", self._yaml_path or "")
        if not match:
            return ""
        components = self._yaml_data.get("components") or []
        index = int(match.group(1))
        if index >= len(components) or not isinstance(components[index], dict):
            return ""
        found = components[index].get("name")
        return found if isinstance(found, str) else ""

    def _component_image(self) -> str:
        """The image this field's registry is about, or "".

        From ``row_data``, else through the index in ``yaml_path``, else from the ``image``
        next to this field in the same config block (the dedicated database).
        """
        image = self._row_data.get("image")
        if isinstance(image, str) and image:
            return image
        match = re.search(r"components\[(\d+)\]", self._yaml_path or "")
        if match:
            components = self._yaml_data.get("components") or []
            index = int(match.group(1))
            if index >= len(components) or not isinstance(components[index], dict):
                return ""
            found = components[index].get("image")
            return found if isinstance(found, str) else ""
        if not self._yaml_path:
            return ""
        sibling = smart_get_value(self._yaml_data, self._yaml_path.rsplit("/", 1)[0] + "/image")
        return sibling if isinstance(sibling, str) else ""


def _cross_domain_peer_side(yaml_path: str | None) -> str:
    """Which side of the rule (``from``/``to``) the field being rendered sits on.

    The peer fields are DEFINED on the peer side already (``from`` for inbound, ``to`` for
    outbound), so the side the path names is the peer side -- the provider needs no separate
    notion of direction. ``.../inbound[0]/from/deployment`` -> ``from``.
    """
    segments = (yaml_path or "").split("/")
    return segments[-2] if len(segments) >= 2 else ""


def _cross_domain_direction(yaml_path: str | None) -> str:
    """``inbound`` or ``outbound``, read from the rule list the field sits in."""
    for segment in (yaml_path or "").split("/"):
        base = segment.split("[")[0]
        if base in ("inbound", "outbound"):
            return base
    return ""


def cross_domain_project_rules(yaml_data: dict[str, Any], direction: str) -> list[dict[str, Any]]:
    """The PROJECT-level rules of one direction, as stored on the project's service entry."""
    for entry in yaml_data.get("services") or []:
        if service_entry_name(entry) != ServiceType.CROSS_DOMAIN_ACCESS.value:
            continue
        config = entry.get("config") if isinstance(entry, dict) else None
        if isinstance(config, dict):
            return [rule for rule in config.get(direction) or [] if isinstance(rule, dict)]
    return []


def _cross_domain_peer_ref(
    row_data: dict[str, Any] | None, yaml_path: str | None, yaml_data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The row's peer block (``{project, deployment, component}``), or an empty dict.

    A DEPLOYMENT-layer row is a patch keyed on the rule's ``name``: it carries only the field
    it overrides, so the peer project sits on the project-level rule of the same name. Falling
    back to that rule is what makes the peer-deployment select work at the patch layer, where
    it is the whole point of the form.
    """
    side = _cross_domain_peer_side(yaml_path)
    peer = (row_data or {}).get(side)
    if isinstance(peer, dict) and peer.get("project"):
        return peer
    name = (row_data or {}).get("name")
    if yaml_data and name:
        for rule in cross_domain_project_rules(yaml_data, _cross_domain_direction(yaml_path)):
            if rule.get("name") == name:
                inherited = rule.get(side)
                if isinstance(inherited, dict):
                    return {**inherited, **(peer if isinstance(peer, dict) else {})}
    return peer if isinstance(peer, dict) else {}


def _cross_domain_peer_project_data(yaml_data: dict[str, Any], project_name: str | None) -> dict[str, Any] | None:
    """The peer project's stored data, or None when it may not or cannot be read.

    Reading another project's file while rendering a form is a real dependency, so it is kept
    narrow and lazy: only a project on the authorized ``_cross_domain_projects`` list is looked
    up, and the lookup goes to the ProjectStore's in-memory cache. A peer that was deleted, or
    that this user may no longer see, yields None and the field keeps whatever is stored.
    """
    if not project_name:
        return None
    if project_name not in (yaml_data.get("_cross_domain_projects") or []):
        return None
    from opi.services.project_store import get_project_store

    summary = get_project_store().get(project_name)
    return summary.data if summary is not None and isinstance(summary.data, dict) else None


def _cross_domain_options(
    names: list[str],
    current_value: str | None,
    *,
    empty_label: str,
    choose_label: str,
    stale_suffix: str,
    labels: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The shared option shape of every cross-domain select.

    Nothing to offer and nothing stored -> one explanatory option instead of a blank select.
    A stored value that is not (or no longer) in the list stays selectable, so saving the form
    never silently drops a rule someone set deliberately.
    """
    if not names and not current_value:
        return [{"value": "", "label": empty_label}]
    options = [{"value": "", "label": choose_label}]
    options.extend({"value": name, "label": (labels or {}).get(name, name)} for name in names)
    if current_value and current_value not in names:
        options.append({"value": current_value, "label": f"{current_value} {stale_suffix}"})
    return options


class CrossDomainProjectOptionsProvider:
    """Peer projects a cross-domain rule may reference.

    Reads ``_cross_domain_projects`` from ``yaml_data`` -- a precomputed list of project
    names the logged-in user is authorized for (set by ``build_cross_domain_context``), the
    own project included. Empty (no context at all) shows an explanatory option instead of a
    blank select. A stored value that is no longer in the list is kept selectable so a save
    does not silently drop it.

    This list is deliberately limited to projects the user is authorized for: a peer you
    cannot see is a peer you cannot name here. That does narrow cross-domain access to
    projects you are a member of; widening it would disclose the platform's project names to
    every user and is a separate decision.

    The label shows the display name with the code between brackets, from
    ``_cross_domain_project_labels``; a project without a display name shows its code alone.
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "De projecten waar je zelf toegang op hebt, zonder dit project zelf. Een peer die "
            "je niet mag zien kun je hier niet noemen."
        ),
        endpoint="GET /api/v2/projects",
        path="projects[].name",
    )

    def __init__(self, yaml_data: dict[str, Any] | None = None, current_value: str | None = None) -> None:
        self.yaml_data = yaml_data or {}
        self.current_value = current_value

    def get_options(self) -> list[dict[str, Any]]:
        names = [n for n in (self.yaml_data.get("_cross_domain_projects") or []) if n]
        labels = dict(self.yaml_data.get("_cross_domain_project_labels") or {})
        # The wildcard is deliberately NOT offered: opening a port to every source is a
        # decision for the owner of a shared facility, made through the API or the project
        # file, not a menu item next to the peer projects. A rule that already carries it is
        # kept and NAMED, because a select that quietly drops a value it does not recognise
        # changes the configuration with nobody touching it.
        if self.current_value == WILDCARD_PROJECT:
            labels[WILDCARD_PROJECT] = "Geen projectlimiet (elke bron)"
            return _cross_domain_options(
                [WILDCARD_PROJECT, *names],
                self.current_value,
                empty_label="Geen andere projecten beschikbaar waar je toegang op hebt",
                choose_label="-- Kies een project --",
                stale_suffix="(niet meer beschikbaar)",
                labels=labels,
            )
        return _cross_domain_options(
            names,
            self.current_value,
            empty_label="Geen andere projecten beschikbaar waar je toegang op hebt",
            choose_label="-- Kies een project --",
            stale_suffix="(niet meer beschikbaar)",
            labels=labels or None,
        )


class CrossDomainPeerDeploymentOptionsProvider:
    """The deployments of the peer project chosen in THIS row.

    A dependent select: the row's peer ``project`` decides the list, so the field reads
    ``row_data`` (the sequence renderer's per-row context) rather than a precomputed union.
    Only deployments on the cluster this instance manages are offered -- a rule pointing at a
    deployment elsewhere resolves to nothing (``resolve.py`` skips it), so offering it would
    be offering a rule that silently never applies.

    Left empty deliberately on a project-level rule: the peer deployment may stay open there
    and be filled per deployment (that is what the deployment layer is for), hence no
    ``required`` on this field.
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "De deployments van het peer-project uit deze regel, alleen die op dit cluster "
            "draaien. Leeg laten mag op projectniveau: dan vul je hem per deployment in."
        ),
        endpoint="GET /api/v2/projects/{peer_project}/deployments",
        path="deployments[].name",
    )

    def __init__(
        self,
        yaml_data: dict[str, Any] | None = None,
        row_data: dict[str, Any] | None = None,
        yaml_path: str | None = None,
        current_value: str | None = None,
    ) -> None:
        self.yaml_data = yaml_data or {}
        self.row_data = row_data or {}
        self.yaml_path = yaml_path
        self.current_value = current_value

    def get_options(self) -> list[dict[str, Any]]:
        from opi.core.config import settings

        peer = _cross_domain_peer_ref(self.row_data, self.yaml_path, self.yaml_data)
        project_data = _cross_domain_peer_project_data(self.yaml_data, peer.get("project"))
        names = [
            str(deployment["name"])
            for deployment in (project_data or {}).get("deployments") or []
            if isinstance(deployment, dict)
            and deployment.get("name")
            and deployment.get("cluster") == settings.CLUSTER_MANAGER
        ]
        empty_label = (
            "Kies eerst een project" if not peer.get("project") else "Dit project heeft geen deployments op dit cluster"
        )
        return _cross_domain_options(
            names,
            self.current_value,
            empty_label=empty_label,
            choose_label="-- Elke deployment (per deployment invullen) --",
            stale_suffix="(niet gevonden)",
        )


class CrossDomainPeerComponentOptionsProvider:
    """The components of the peer deployment chosen in THIS row.

    The next link in the same cascade: project -> deployment -> component. Without a peer
    deployment chosen -- which is a legitimate state, a project-level rule may leave it open --
    the list is every component the peer runs on this cluster, deduped. That is not a guess:
    a component name is a project-level definition, deployments only reference it, so the
    union is exactly the set of names that could be valid once a deployment is filled in.
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description="De componenten van het peer-project uit deze regel.",
        endpoint="GET /api/v2/projects/{peer_project}/components",
        path="components[].name",
    )

    def __init__(
        self,
        yaml_data: dict[str, Any] | None = None,
        row_data: dict[str, Any] | None = None,
        yaml_path: str | None = None,
        current_value: str | None = None,
    ) -> None:
        self.yaml_data = yaml_data or {}
        self.row_data = row_data or {}
        self.yaml_path = yaml_path
        self.current_value = current_value

    def get_options(self) -> list[dict[str, Any]]:
        from opi.core.config import settings

        peer = _cross_domain_peer_ref(self.row_data, self.yaml_path, self.yaml_data)
        project_data = _cross_domain_peer_project_data(self.yaml_data, peer.get("project"))
        deployments = [
            deployment
            for deployment in (project_data or {}).get("deployments") or []
            if isinstance(deployment, dict) and deployment.get("cluster") == settings.CLUSTER_MANAGER
        ]
        chosen = Project(project_data or {}).find("deployments", name=peer.get("deployment")) or {}
        if chosen:
            deployments = [chosen]
        names: list[str] = []
        for deployment in deployments:
            for component in deployment.get("components") or []:
                reference = component.get("reference") if isinstance(component, dict) else None
                if reference and reference not in names:
                    names.append(reference)
        names.sort()
        empty_label = (
            "Kies eerst een project" if not peer.get("project") else "Dit project heeft geen componenten op dit cluster"
        )
        return _cross_domain_options(
            names,
            self.current_value,
            empty_label=empty_label,
            choose_label="-- Kies een component --",
            stale_suffix="(niet gevonden)",
        )


class CrossDomainRuleNameOptionsProvider:
    """The names of the PROJECT-level rules a deployment patch can address.

    At the deployment layer the name is not a free-text label but a REFERENCE: a patch with
    the same name overrides that project rule (``merge.py``). Offering the existing names is
    what makes the difference between patching a rule and silently creating a second one.
    A name that is not among them is still kept -- that is how a deployment adds a rule of its
    own, which the merge explicitly allows.
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "De namen van de regels op projectniveau: dezelfde naam gebruiken betekent die "
            "regel aanpassen, een nieuwe naam betekent een eigen regel voor deze deployment."
        ),
        endpoint="GET /api/v2/projects/{project_name}/services/cross-domain-access/config",
        path="[target=project].config.inbound[].name",
    )

    def __init__(
        self,
        yaml_data: dict[str, Any] | None = None,
        yaml_path: str | None = None,
        current_value: str | None = None,
    ) -> None:
        self.yaml_data = yaml_data or {}
        self.yaml_path = yaml_path
        self.current_value = current_value

    def get_options(self) -> list[dict[str, Any]]:
        direction = _cross_domain_direction(self.yaml_path)
        names = [
            rule["name"]
            for rule in cross_domain_project_rules(self.yaml_data, direction)
            if isinstance(rule.get("name"), str)
        ]
        return _cross_domain_options(
            names,
            self.current_value,
            empty_label="Dit project heeft nog geen regels op projectniveau",
            choose_label="-- Kies een regel --",
            stale_suffix="(alleen in deze deployment)",
        )


class CrossDomainLocalComponentOptionsProvider:
    """The project's OWN components, for the own side of a cross-domain rule.

    Reads ``components`` from the surrounding form data, like ``WakerComponentOptionsProvider``.
    Empty in the create wizard (no components yet), populated in the edit flow.
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description="De componenten van dit project.",
        endpoint="GET /api/v2/projects/{project_name}/components",
        path="components[].name",
    )

    def __init__(self, yaml_data: dict[str, Any] | None = None, current_value: str | None = None) -> None:
        self.yaml_data = yaml_data or {}
        self.current_value = current_value

    def get_options(self) -> list[dict[str, Any]]:
        names = [
            name
            for component in (self.yaml_data.get("components") or [])
            if isinstance(component, dict) and (name := component.get("name"))
        ]
        return _cross_domain_options(
            names,
            self.current_value,
            empty_label="Nog geen componenten: voeg eerst een component toe",
            choose_label="-- Kies een component --",
            stale_suffix="(bestaat niet meer)",
        )


WALL_PORT = 4180

#: The label of the wall port in a list of options. Why 4180 is there belongs in the option
#: itself and not in a help text under the field: it holds for one option and not for the
#: field, and it is an edge case you only need to understand if you pick it.
WALL_PORT_LABEL = f"{WALL_PORT} (via authorization wall)"


def _component_has_wall(component: dict[str, Any]) -> bool:
    """Whether an authorization wall fronts this component."""
    return any(
        service_entry_name(entry) == ServiceType.AUTHORIZATION_WALL.value for entry in component.get("services") or []
    )


def _cross_domain_component_ports(component: dict[str, Any]) -> list[int]:
    """The ports a component is reachable on: its inbound ports, plus 4180 behind the wall.

    Same rule the precomputed union used, now per component: an authorization-wall fronts the
    component with an oauth2-proxy on 4180, which is then the port the other side actually
    reaches it on.
    """
    ports = [port for port in (component.get("ports") or {}).get("inbound") or [] if isinstance(port, int)]
    for entry in component.get("services") or []:
        if service_entry_name(entry) == ServiceType.AUTHORIZATION_WALL.value and WALL_PORT not in ports:
            ports.append(WALL_PORT)
    return ports


class CrossDomainPortOptionsProvider:
    """The ports of the RECEIVING side of this rule.

    A rule's port always sits on ``to``, but who ``to`` is differs per direction, and that is
    exactly what this field must decide for the user instead of leaving it to them:

    * inbound  -- ``to`` is my own component, so the list is that component's inbound ports.
    * outbound -- ``to`` is the peer's component, so the list is read from the peer project.

    Both are per-row questions (which component did THIS row pick), answered from ``row_data``.
    With no component chosen yet the list falls back to the union of the own project's ports
    (``_cross_domain_ports``) for inbound, and stays empty with an explanation for outbound.
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "De inkomende poorten van het ontvangende component, plus 4180 als daar een authorization-wall voor staat."
        ),
        endpoint="GET /api/v2/projects/{project_name}/components",
        path="components[].ports",
    )

    def __init__(
        self,
        yaml_data: dict[str, Any] | None = None,
        row_data: dict[str, Any] | None = None,
        yaml_path: str | None = None,
        current_value: str | None = None,
    ) -> None:
        self.yaml_data = yaml_data or {}
        self.row_data = row_data or {}
        self.yaml_path = yaml_path
        self.current_value = current_value

    def _own_ports(self) -> tuple[list[int], str]:
        """Ports of my own component named on the ``to`` side of this inbound row."""
        own_side = self.row_data.get("to")
        component_name = own_side.get("component") if isinstance(own_side, dict) else None
        for component in self.yaml_data.get("components") or []:
            if isinstance(component, dict) and component.get("name") == component_name:
                return _cross_domain_component_ports(component), "Dit component heeft geen inbound-poorten"
        # No own component picked yet (or it vanished): the union over my components, so the
        # field is usable before the rest of the row is filled in. Derived from the form's own
        # data rather than precomputed per flow; ``_cross_domain_ports`` is the older
        # precomputed union and is still read, so a wizard session in flight keeps working.
        union: list[int] = [port for port in (self.yaml_data.get("_cross_domain_ports") or []) if isinstance(port, int)]
        for component in self.yaml_data.get("components") or []:
            if not isinstance(component, dict):
                continue
            for port in _cross_domain_component_ports(component):
                if port not in union:
                    union.append(port)
        return union, "Geen poorten bekend: stel eerst inbound-poorten in op een component"

    def _peer_ports(self) -> tuple[list[int], str]:
        """Ports of the peer component named on the ``to`` side of this outbound row.

        Read from the peer's project-level ``components`` definition, not from its deployment:
        ports are a property of the component, and a rule may legitimately leave the peer
        deployment open.
        """
        # The port always lives on ``to``, which for an outbound rule is the peer.
        peer = _cross_domain_peer_ref(self.row_data, self.yaml_path, self.yaml_data)
        project_data = _cross_domain_peer_project_data(self.yaml_data, peer.get("project"))
        if not peer.get("project"):
            return [], "Kies eerst een project"
        if not peer.get("component"):
            return [], "Kies eerst een component"
        return _peer_component_ports(project_data, peer.get("component")), ("Dit component heeft geen inbound-poorten")

    def get_options(self) -> list[dict[str, Any]]:
        ports, empty_label = (
            self._peer_ports() if _cross_domain_direction(self.yaml_path) == "outbound" else self._own_ports()
        )
        return _cross_domain_options(
            [str(port) for port in ports],
            str(self.current_value) if self.current_value else None,
            empty_label=empty_label,
            choose_label="-- Kies een poort --",
            stale_suffix="(niet in de lijst)",
            labels={str(WALL_PORT): WALL_PORT_LABEL} if self._wall_port_is_the_walls(ports) else None,
        )

    def _wall_port_is_the_walls(self, ports: list[int]) -> bool:
        """Whether 4180 is in this list because of the authorization wall.

        A component may simply carry 4180 as an inbound port of its own, and then the label
        "via authorization wall" is wrong, so only label it when a wall really fronts it.
        """
        if WALL_PORT not in ports:
            return False
        source = self.yaml_data
        if _cross_domain_direction(self.yaml_path) == "outbound":
            peer = _cross_domain_peer_ref(self.row_data, self.yaml_path, self.yaml_data)
            source = _cross_domain_peer_project_data(self.yaml_data, peer.get("project")) or {}
        return any(
            _component_has_wall(component)
            for component in source.get("components") or []
            if isinstance(component, dict)
        )


def _peer_component_ports(project_data: dict[str, Any] | None, component_name: str | None) -> list[int]:
    """A peer component's reachable ports.

    A deployment lists components by ``reference``; the ports live on the project's own
    ``components`` definition, so the name is resolved there.
    """
    for component in (project_data or {}).get("components") or []:
        if isinstance(component, dict) and component.get("name") == component_name:
            return _cross_domain_component_ports(component)
    return []


class KeycloakAccountLinkOptionsProvider:
    """How an existing account is linked when someone signs in through an identity provider.

    The values come from ``AccountLink`` in the config model, and
    ``build_project_realm_context`` passes them to the realm template as ``account_link``.
    Nothing filled in is the Keycloak default: the user confirms the link by email.
    """

    # The list is fixed: every project gets these choices.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {
                "value": "",
                "label": "Standaard (verificatie via e-mail)",
                "description": "De gebruiker bevestigt de koppeling via een e-mail. De keuze van Keycloak zelf.",
            },
            {
                "value": "automatic",
                "label": "Automatisch koppelen",
                "description": (
                    "Het bestaande account wordt zonder tussenstap gekoppeld. Kies dit alleen wanneer "
                    "de identity provider het e-mailadres al heeft geverifieerd."
                ),
            },
            {
                "value": "confirm",
                "label": "Bevestigen op het scherm",
                "description": "De gebruiker bevestigt de koppeling in de browser, zonder e-mail.",
            },
        ]


class InviteLanguageOptionsProvider:
    """The two languages an invite's default-language can take."""

    # The list is fixed: every project gets these choices.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "nl", "label": "Nederlands"},
            {"value": "en", "label": "Engels"},
        ]


class InviteAuthMethodOptionsProvider:
    """The auth methods an invite may allow, read from the project's keycloak template.

    An invite can only narrow what the realm offers, never widen it (``invite_routes``
    computes ``realm_auth[x] and invite_auth_config[x]``). The realm follows the keycloak
    template, and the blueprints differ exactly here:

    * ``sso-only``    -- the browser flow redirects straight to SSO Rijk, so there is no
      screen on which a local account could ever be typed in;
    * ``sso-support`` and ``sso-support-otp``: Keycloak's own login screen, which offers
      both. Which blueprint allows a local account is decided in one place,
      ``allows_local_accounts``;
    * ``local-only``  -- no identity provider, so SSO is not offered (``allows_sso``).

    Offering "Lokaal account" under sso-only would therefore be a choice that silently does
    nothing. Empty selection still means "fall back to whatever the realm allows".

    When the realm links an SSO identity by email address (``account-link``), "Lokaal account"
    drops out too: an account with a self-typed address is not allowed there. The invite path
    already refuses that method and the save refuses it as well, so offering it here would be
    a choice that bounces on save. Unlike under sso-only the field does stay, with SSO only;
    the field's help text says why "Lokaal account" is missing.
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "sso tenzij het keycloak-template local-only is; local alleen als het template "
            "lokale accounts toestaat (sso-support, sso-support-otp of local-only) en het "
            "realm niet automatisch koppelt op e-mailadres (account-link)."
        ),
        endpoint="GET /api/v2/projects/{project_name}/services/keycloak/config",
        path="configurations[].config.template",
    )

    def __init__(self, yaml_data: dict[str, Any] | None = None) -> None:
        self.yaml_data = yaml_data or {}

    def get_options(self) -> list[dict[str, Any]]:
        from opi.forms.editables.service_path import smart_get_value

        # Absent template means sso-only (the default on KeycloakConfig.template),
        # which is what the realm gets. Treating it as "unknown, allow both" would
        # conflict with the field's show_when rule, which hides the field in that case.
        template = smart_get_value(self.yaml_data, "services/keycloak/config/template")
        options = [{"value": "sso", "label": "Single sign-on (SSO)"}] if allows_sso(template) else []
        # Through smart_get_value and not through the helper's project entrance: in the
        # wizard the keycloak config sits under the virtual root, where a read on "services"
        # finds nothing.
        links_on_email = account_link_mode(smart_get_value(self.yaml_data, "services/keycloak/config")) is not None
        if allows_local_accounts(template) and not links_on_email:
            options.append({"value": "local", "label": "Lokaal account"})
        return options


class InviteRealmRoleOptionsProvider:
    """Keycloak realm roles an invite can assign, gathered from the surrounding form data.

    Two sources, deduped in order:
    1. ``services/keycloak/config/realm-roles[*]/name`` -- custom realm roles.
    2. ``services/keycloak/config/restrict-access/realm-role`` -- the authorization-wall role
       (default ``allowed-user``). This matters: every live project uses ``allowed-user`` and
       it is NOT listed under ``realm-roles``; a provider reading only ``realm-roles`` would be
       empty in practice.

    A first, explicit "no role" option lets an invite deliberately grant only a bare account
    (a first-class choice, not an omission). The currently stored value is always kept as an
    option -- marked "(bestaat niet meer)" when it is no longer in the sources -- so a select
    never silently drops a role that was removed from the keycloak config on the next save.
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "De realm-rollen van dit project: de rollen onder de keycloak-config plus de rol "
            "van de authorization-wall. Leeg betekent geen rol toekennen."
        ),
        endpoint="GET /api/v2/projects/{project_name}/services/keycloak/config",
        path="[target=project].config.realm-roles[].name",
    )

    def __init__(self, yaml_data: dict[str, Any] | None = None, current_value: str | None = None) -> None:
        self.yaml_data = yaml_data or {}
        self.current_value = current_value

    def get_options(self) -> list[dict[str, Any]]:
        from opi.forms.editables.service_path import smart_get_value

        roles: list[str] = []
        realm_roles = smart_get_value(self.yaml_data, "services/keycloak/config/realm-roles") or []
        if isinstance(realm_roles, list):
            for entry in realm_roles:
                name = entry.get("name") if isinstance(entry, dict) else None
                if name and name not in roles:
                    roles.append(str(name))
        wall_role = smart_get_value(self.yaml_data, "services/keycloak/config/restrict-access/realm-role")
        # A client role replaces the realm role at the gate, so the realm role grants nothing.
        if smart_get_value(self.yaml_data, "services/keycloak/config/restrict-access/role"):
            wall_role = None
        if wall_role and str(wall_role) not in roles:
            roles.append(str(wall_role))

        options: list[dict[str, Any]] = [{"value": "", "label": "Geen rol toekennen"}]
        options.extend({"value": role, "label": role} for role in roles)

        # Keep a stored-but-now-unknown value selectable, flagged, so saving does not drop it.
        if self.current_value and self.current_value not in roles:
            options.append({"value": self.current_value, "label": f"{self.current_value} (bestaat niet meer)"})
        return options


class HealthCheckSchemeOptionsProvider:
    """Probe scheme options for the health-check service. The empty value means
    'default': fall back to a plain TCP probe on the first inbound port."""

    # The list is fixed: every project gets these choices.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "", "label": "Standaard (tcp op eerste poort)"},
            {"value": "tcp", "label": "TCP (socket open)"},
            {"value": "http", "label": "HTTP (httpGet op pad)"},
            {"value": "https", "label": "HTTPS (httpGet op pad)"},
            {"value": "none", "label": "Geen (alle probes uit)"},
        ]


class InviteApplicationUrlOptionsProvider:
    """Where the success button of an invitation points: a public URL of this project.

    Someone setting up an invitation knows which deployment and component they want people
    to land on; they do not know the hostname, which is derived from the domain format,
    the subdomain and the cluster. So the choice is offered in those terms and the URL is
    filled in behind it.

    An empty first option is deliberate: an invitation without a destination is valid and
    simply shows no button, which is better than a button pointing somewhere wrong.
    Anything already stored that is no longer derivable stays selectable, flagged, so
    saving the form does not silently drop it.
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "De publieke URL's van dit project, afgeleid uit de deployments en hun "
            "publish-on-web-instellingen. Leeg betekent geen knop tonen."
        ),
        endpoint="GET /api/v2/projects/{project_name}/deployments",
        path="deployments[].components[].url",
    )

    def __init__(self, yaml_data: dict[str, Any] | None = None, current_value: str | None = None) -> None:
        self.yaml_data = yaml_data or {}
        self.current_value = current_value

    def get_options(self) -> list[dict[str, Any]]:
        from opi.services.catalog.publish_on_web.urls import public_urls_for_project
        from opi.services.project_file_handler import ProjectFileHandler

        options: list[dict[str, Any]] = [{"value": "", "label": "Geen knop tonen"}]
        try:
            urls = public_urls_for_project(self.yaml_data, ProjectFileHandler())
        except KeyError, ValueError, AttributeError, TypeError:
            # A half-configured project (no cluster yet, no domain chosen) must still
            # render the form: the picker then simply offers no destination.
            logger.debug("Could not derive public URLs for the invite destination", exc_info=True)
            urls = []

        # A component MAY publish several paths, and those are as many addresses. A label of
        # deployment and component alone then lists them as two identical lines, while one of
        # them has to be picked; the dedupe below does not catch them, and rightly so, because
        # the URLs really differ. So the path is added, but only where it resolves something:
        # on a component with a single path a "/" behind the name is noise.
        seen: set[str] = set()
        chosen: list[dict[str, str]] = []
        for entry in urls:
            url = entry.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            chosen.append(entry)

        multiple: set[tuple[str, str]] = set()
        counted: set[tuple[str, str]] = set()
        for entry in chosen:
            key = (entry["deployment_name"], entry["component_name"])
            if key in counted:
                multiple.add(key)
            counted.add(key)

        for entry in chosen:
            label = f"{entry['deployment_name']} / {entry['component_name']}"
            if (entry["deployment_name"], entry["component_name"]) in multiple:
                label = f"{label} ({entry.get('path') or '/'})"
            options.append({"value": entry["url"], "label": label})

        # Only an ADDRESS may be added here. The stored value is the CHOICE
        # ("frontend:production"), and the form passes the RAW storage as the current value,
        # so without this test a line reading "frontend:production (niet meer afleidbaar)"
        # lands in the list and can be picked. A choice that still resolves is already in the
        # list above; one that does not has no address to show, and then "no button" is the
        # honest answer, which is exactly what the success page does.
        current = self.current_value if isinstance(self.current_value, str) else None
        if current and current.startswith(("http://", "https://")) and current not in seen:
            options.append({"value": current, "label": f"{current} (niet meer afleidbaar)"})
        return options


PROVIDER_REGISTRY: dict[str, type[OptionsProvider]] = {
    "ClusterOptionsProvider": ClusterOptionsProvider,
    "ServiceOptionsProvider": ServiceOptionsProvider,
    "ComponentTypeOptionsProvider": ComponentTypeOptionsProvider,
    "UserRoleOptionsProvider": UserRoleOptionsProvider,
    "CpuRequestOptionsProvider": CpuRequestOptionsProvider,
    "CpuLimitOptionsProvider": CpuLimitOptionsProvider,
    "MemoryOptionsProvider": MemoryOptionsProvider,
    "MemoryRequestOptionsProvider": MemoryRequestOptionsProvider,
    "StorageTypeOptionsProvider": StorageTypeOptionsProvider,
    "StorageSizeOptionsProvider": StorageSizeOptionsProvider,
    "PostgresStorageSizeOptionsProvider": PostgresStorageSizeOptionsProvider,
    "StorageSizeOverrideOptionsProvider": StorageSizeOverrideOptionsProvider,
    "ConnectionLimitOptionsProvider": ConnectionLimitOptionsProvider,
    "DeploymentConnectionLimitOptionsProvider": DeploymentConnectionLimitOptionsProvider,
    "MaxNoncurrentVersionsOptionsProvider": MaxNoncurrentVersionsOptionsProvider,
    "NoncurrentExpireDaysOptionsProvider": NoncurrentExpireDaysOptionsProvider,
    "MinioDeploymentMaxNoncurrentVersionsOptionsProvider": MinioDeploymentMaxNoncurrentVersionsOptionsProvider,
    "MinioDeploymentNoncurrentExpireDaysOptionsProvider": MinioDeploymentNoncurrentExpireDaysOptionsProvider,
    "SeaweedfsDeploymentMaxNoncurrentVersionsOptionsProvider": SeaweedfsDeploymentMaxNoncurrentVersionsOptionsProvider,
    "SeaweedfsDeploymentNoncurrentExpireDaysOptionsProvider": SeaweedfsDeploymentNoncurrentExpireDaysOptionsProvider,
    "KeycloakTemplateOptionsProvider": KeycloakTemplateOptionsProvider,
    "KeycloakAccountLinkOptionsProvider": KeycloakAccountLinkOptionsProvider,
    "PullPolicyOptionsProvider": PullPolicyOptionsProvider,
    "EnvironmentOptionsProvider": EnvironmentOptionsProvider,
    "BaseDomainOptionsProvider": BaseDomainOptionsProvider,
    "ClusterBaseDomainOptionsProvider": ClusterBaseDomainOptionsProvider,
    "FilteredServiceOptionsProvider": FilteredServiceOptionsProvider,
    "ComponentReferenceOptionsProvider": ComponentReferenceOptionsProvider,
    "BackupScheduleFrequencyOptionsProvider": BackupScheduleFrequencyOptionsProvider,
    "BackupScheduleTimeOptionsProvider": BackupScheduleTimeOptionsProvider,
    "BackupScheduleDayOptionsProvider": BackupScheduleDayOptionsProvider,
    "BackupScheduleMonthDayOptionsProvider": BackupScheduleMonthDayOptionsProvider,
    "BackupResourceTypesOptionsProvider": BackupResourceTypesOptionsProvider,
    "BackupDeploymentOptionsProvider": BackupDeploymentOptionsProvider,
    "DeploymentCloneFromOptionsProvider": DeploymentCloneFromOptionsProvider,
    "RestoreConfigSourceOptionsProvider": RestoreConfigSourceOptionsProvider,
    "RootComponentOptionsProvider": RootComponentOptionsProvider,
    "BareDomainComponentOptionsProvider": BareDomainComponentOptionsProvider,
    "RepositoryOptionsProvider": RepositoryOptionsProvider,
    "DomainFormatOptionsProvider": DomainFormatOptionsProvider,
    "DeploymentSelectOptionsProvider": DeploymentSelectOptionsProvider,
    "ApprovalStatusOptionsProvider": ApprovalStatusOptionsProvider,
    "AttachmentOptionsProvider": AttachmentOptionsProvider,
    "AttachmentProvideAsOptionsProvider": AttachmentProvideAsOptionsProvider,
    "PublishTlsModeOptionsProvider": PublishTlsModeOptionsProvider,
    "PublishTlsOverrideOptionsProvider": PublishTlsOverrideOptionsProvider,
    "YesNoOptionsProvider": YesNoOptionsProvider,
    "WakeModeOptionsProvider": WakeModeOptionsProvider,
    "SleepAfterDeployOptionsProvider": SleepAfterDeployOptionsProvider,
    "SleepAfterWakeOptionsProvider": SleepAfterWakeOptionsProvider,
    "WakerComponentOptionsProvider": WakerComponentOptionsProvider,
    "ImageRegistryOptionsProvider": ImageRegistryOptionsProvider,
    "HealthCheckSchemeOptionsProvider": HealthCheckSchemeOptionsProvider,
    "InviteLanguageOptionsProvider": InviteLanguageOptionsProvider,
    "InviteAuthMethodOptionsProvider": InviteAuthMethodOptionsProvider,
    "InviteApplicationUrlOptionsProvider": InviteApplicationUrlOptionsProvider,
    "InviteRealmRoleOptionsProvider": InviteRealmRoleOptionsProvider,
    "CrossDomainProjectOptionsProvider": CrossDomainProjectOptionsProvider,
    "CrossDomainPeerDeploymentOptionsProvider": CrossDomainPeerDeploymentOptionsProvider,
    "CrossDomainRuleNameOptionsProvider": CrossDomainRuleNameOptionsProvider,
    "CrossDomainPeerComponentOptionsProvider": CrossDomainPeerComponentOptionsProvider,
    "CrossDomainLocalComponentOptionsProvider": CrossDomainLocalComponentOptionsProvider,
    "CrossDomainPortOptionsProvider": CrossDomainPortOptionsProvider,
}


def get_provider(name: str, **kwargs: str | bool | int) -> OptionsProvider:
    """The provider registered under *name*, built with *kwargs*; raises ``KeyError``."""
    if name not in PROVIDER_REGISTRY:
        raise KeyError(f"Unknown provider: {name}")
    return PROVIDER_REGISTRY[name](**kwargs)


def get_all_providers() -> dict[str, OptionsProvider]:
    """An instance of every registered provider, by name."""
    return {name: cls() for name, cls in PROVIDER_REGISTRY.items()}
