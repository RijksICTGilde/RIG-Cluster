"""
Options providers for dynamic form field population.

This module provides the OptionsProvider protocol and concrete implementations
for populating select/radio fields with dynamic data from OPI's domain.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Protocol

from opi.core.cluster_config import CLUSTER_CONFIG, get_selectable_clusters
from opi.core.config import settings
from opi.services.catalog.cross_domain_access.config_model import WILDCARD_PROJECT
from opi.services.catalog.shared.storage import STORAGE_SIZES
from opi.services.services import ServiceAdapter, service_entry_name
from opi.services.services_enums import ServiceKind, ServiceType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OptionsSource:
    """Waar de keuzes van een veld vandaan komen als ze per project verschillen.

    De keuzelijst van een formulierveld is ook het antwoord op "welke waarden mag ik
    hier sturen", en dat antwoord hoort in de API-documentatie te staan. Voor een vaste
    lijst kan dat als opsomming. Voor een lijst die uit het project zelf komt (de
    componenten, de deployments van een peer, de bijlagen in de catalogus) kan dat niet:
    een opsomming zou een momentopname zijn die voor elk ander project onwaar is. Dan is
    het eerlijke antwoord de BRON, en die staat hier: waar een client de lijst zelf
    ophaalt en, als er geen endpoint voor is, waarvan de lijst afhangt.
    """

    description: str
    """Wat de lijst is, in het Nederlands. Dit is de tekst die een lezer krijgt."""
    endpoint: str | None = None
    """Methode en pad van het endpoint dat de opties levert, of None als er geen is."""
    path: str | None = None
    """Waar in het antwoord van dat endpoint de waarden staan, bijvoorbeeld
    ``components[].name``. None als er geen endpoint is."""

    def as_json(self) -> dict[str, str]:
        """Machineleesbare vorm; lege velden blijven weg in plaats van als null."""
        data = {"description": self.description}
        if self.endpoint:
            data["endpoint"] = self.endpoint
        if self.path:
            data["path"] = self.path
        return data


#: De schakelaar "ik vul zelf een domein in" in de base-domain-select.
#:
#: Geen waarde maar een SCHAKELAAR: hij zet in het formulier een tweede, tijdelijk veld aan
#: (``deployments[*]/base-domain:custom``) waar het echte domein in gaat, en wordt bij het
#: opslaan door dat domein vervangen. Opgeslagen worden kan hij dus niet, en een schrijfactie
#: die hem toch draagt wordt geweigerd ("Een aangepast domein is geselecteerd maar niet
#: ingevuld", ``DomainConfigEnforcer``).
#:
#: Daarom staat hij hier bij naam: wat de API publiceert (``GET .../clusters``, en via
#: ``options_source`` de ``x-choices-source`` van het veld) moet hem eruit laten, want een
#: keuzelijst die een waarde noemt die de uitrol weigert stuurt elke client het bos in. Een
#: eigen domein zet een API-client door de domeinnaam zelf in ``base-domain`` te schrijven.
CUSTOM_DOMAIN_SENTINEL: Final = "__custom__"

#: Een provider die nog niet heeft gezegd of zijn lijst vastligt of per project verschilt.
#:
#: Onderscheiden van ``options_source = None`` (de lijst ligt vast): wie het niet declareert
#: krijgt geen keuzelijst in de API-documentatie, want een lijst die een projectafhankelijke
#: provider zonder projectcontext oplevert is niet leeg maar FOUT -- hij toont dan de paar
#: opties die zonder context overblijven alsof dat de toegestane waarden zijn.
UNDECLARED_SOURCE: Final = object()


class OptionsProvider(Protocol):
    """
    Protocol for dynamic options providers.

    Options providers are used to populate select/radio fields
    with options from external data sources (databases, configs, APIs).
    """

    options_source: ClassVar[OptionsSource | None]
    """None als de lijst vastligt, een ``OptionsSource`` als hij per project verschilt.

    Elke provider die een service-configveld vult declareert dit, want de API-documentatie
    leest hem hier af (``opi/api/openapi_choices.py``); ``tests/test_openapi_config_choices.py``
    houdt daar iedereen aan. Een provider die alleen buiten de service-config gebruikt wordt
    hoeft niets te zeggen.
    """

    def get_options(self) -> list[dict[str, Any]]:
        """
        Get options for a select/radio field.

        Returns:
            List of option dicts with 'value' and 'label' keys.
            May also include 'description', 'disabled', 'group' etc.
        """
        ...


class ClusterOptionsProvider:
    """
    Provides available Kubernetes cluster options.

    Reads from the cluster configuration to provide available
    deployment targets.
    """

    def __init__(self, include_empty: bool = False, empty_label: str = "Selecteer een cluster") -> None:
        """
        Initialize the cluster options provider.

        Args:
            include_empty: Whether to include an empty "select" option (for dropdowns)
            empty_label: Label for the empty option
        """
        self.include_empty = include_empty
        self.empty_label = empty_label

    def get_options(self) -> list[dict[str, Any]]:
        """Get available cluster options."""
        options: list[dict[str, Any]] = []

        if self.include_empty:
            options.append({"value": "", "label": self.empty_label})

        # Map cluster names to user-friendly labels
        cluster_labels = {
            "local": "Lokaal (Kind)",
            "odcn-staging": "Staging Cluster (ODC-Noord)",
            "odcn-production": "Productie Cluster (ODC-Noord)",
        }

        # Only the clusters this environment offers as a deployment target: production
        # shows just odcn-production, development a configurable set. Driven by the
        # managing cluster's config, not by every cluster that happens to be defined.
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
    """
    Provides available service options.

    Reads from the ServiceAdapter to provide available
    services that can be enabled for projects.
    """

    def __init__(
        self,
        include_empty: bool = False,
        filter_binding: str | None = None,
    ) -> None:
        """
        Initialize the service options provider.

        Args:
            include_empty: Whether to include an empty "select" option
            filter_binding: Filter services by binding ("component" or "deployment")
        """
        self.include_empty = include_empty
        self.filter_binding = filter_binding

    def get_options(self) -> list[dict[str, Any]]:
        """Get available service options from ServiceAdapter definitions."""
        # Imported here, not at module scope: the registry imports the catalog, whose
        # services import this forms module back. The catalog breaks the same cycle the
        # same way (instructions/services.md, "Keep the catalog import-light").
        from opi.services.registry import get_service

        options: list[dict[str, Any]] = []

        if self.include_empty:
            options.append({"value": "", "label": "Selecteer een service"})

        for service_type in ServiceType:
            definition = ServiceAdapter.get_service_definition(service_type)

            # Skip hidden services and system services (never user-selectable)
            if definition.hidden or definition.kind is ServiceKind.SYSTEM:
                continue

            # Skip a service this cluster cannot deliver. Asked of the service itself
            # (Service.available_on_cluster), which answers from the cluster
            # configuration -- no cluster name appears here. The managing cluster is the
            # measure because an OPI instance only ever provisions its own cluster. This
            # is presentation only: the refusal that counts is at save time, in
            # validate_service_availability, since the API and a hand-written project
            # file never see a card.
            if not get_service(service_type).available_on_cluster(settings.CLUSTER_MANAGER):
                continue

            # Filter by binding if specified (filter_binding is the plain string value)
            if self.filter_binding and definition.binding.value != self.filter_binding:
                continue

            option: dict[str, Any] = {
                "value": service_type.value,
                "label": definition.name,
                "description": definition.description,
                "icon": definition.icon,
                "color": definition.color,
                # .value so the view/JS gets "component", not "ServiceBinding.COMPONENT".
                "binding": definition.binding.value,
            }

            if definition.requires:
                option["requires"] = definition.requires

            if definition.help_template:
                option["help_template"] = definition.help_template

            # Uit de declaratie van de dienst zelf (approval_specs), niet uit een
            # lijstje hier: een dienst die goedkeuring gaat vereisen draagt de
            # waarschuwing dan vanzelf, op de kaart en in de uitleg allebei. De import
            # staat binnenin: de registry laadt de dienstpakketten en die trekken langs
            # de formulierlaag deze module weer binnen.
            from opi.services.registry import get_service

            if get_service(service_type).approval_specs():
                option["requires_approval"] = True

            options.append(option)

        return options


class ComponentTypeOptionsProvider:
    """Provides component type options (single, frontend, backend)."""

    def __init__(self, include_empty: bool = False) -> None:
        self.include_empty = include_empty

    def get_options(self) -> list[dict[str, Any]]:
        """Get available component type options."""
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
    """Provides user role options for project access."""

    def __init__(self, include_empty: bool = False) -> None:
        self.include_empty = include_empty

    def get_options(self) -> list[dict[str, Any]]:
        """Get available user role options."""
        options: list[dict[str, Any]] = []

        if self.include_empty:
            options.append({"value": "", "label": "Selecteer rol"})

        options.extend(
            [
                {
                    "value": "admin",
                    "label": "Administrator",
                    "description": "Volledige toegang tot alle resources en instellingen",
                },
                {
                    "value": "developer",
                    "label": "Developer",
                    "description": "Kan applicaties deployen en logs bekijken",
                },
                {
                    "value": "operator",
                    "label": "Operator",
                    "description": "Alleen-lezen toegang voor monitoring",
                },
            ]
        )

        return options


class CpuRequestOptionsProvider:
    """Provides CPU request options for components."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available CPU request options."""
        return [
            {"value": "50m", "label": "50m (minimaal)"},
            {"value": "100m", "label": "100m"},
            {"value": "250m", "label": "250m"},
            {"value": "500m", "label": "500m"},
        ]


class CpuLimitOptionsProvider:
    """Provides CPU limit options for components."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available CPU limit options."""
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
            from opi.services.resource_analyzer import parse_k8s_memory_to_mi

            try:
                current_mi = parse_k8s_memory_to_mi(self.current_value)
            except ValueError:
                return options

            label = f"{int(current_mi)} Mi" if current_mi == int(current_mi) else f"{current_mi:.1f} Mi"
            new_option = {"value": self.current_value, "label": label}

            # Insert at sorted position
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


class DomainModeOptionsProvider:
    """Provides domain mode options for URL configuration."""

    # De lijst ligt vast: elk project krijgt deze keuzes.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        """Get available domain mode options."""
        return [
            {
                "value": "component-specific",
                "label": "Component-specifiek (standaard)",
                "description": "Elk component krijgt zijn eigen unieke URL",
            },
            {
                "value": "deployment-name",
                "label": "Deployment-naam (gedeeld domein)",
                "description": "Alle componenten delen dezelfde domeinnaam met verschillende paden",
            },
            {
                "value": "custom",
                "label": "Aangepast subdomein",
                "description": "Specificeer een custom subdomein voor alle componenten",
            },
            {
                "value": "nice-url",
                "label": "Eigen subdomein (nice URL)",
                "description": "Punt-gescheiden URLs zoals frontend.mijnapp.rijks.app",
            },
        ]


class StorageTypeOptionsProvider:
    """Provides storage type options for container volumes."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available storage type options."""
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
    """Provides storage size options for persistent volumes."""

    # De lijst ligt vast: elk project krijgt deze keuzes.
    options_source: ClassVar[OptionsSource | None] = None

    #: Alleen het label per maat. De maten zelf staan in ``STORAGE_SIZES``, want daar
    #: hangt ook de bovengrens aan die het configmodel afdwingt; twee lijsten die uit
    #: elkaar lopen is precies hoe een keuzelijst iets anders gaat beloven dan de API
    #: accepteert.
    LABELS: ClassVar[dict[str, str]] = {
        "50Mi": "50 MB",
        "100Mi": "100 MB",
        "250Mi": "250 MB",
        "500Mi": "500 MB",
        "1Gi": "1 GB",
    }

    def get_options(self) -> list[dict[str, Any]]:
        """Get available storage size options."""
        return [{"value": size, "label": self.LABELS.get(size, size)} for size in STORAGE_SIZES]


class KeycloakTemplateOptionsProvider:
    """The two realm blueprints, named after what a user gets rather than after the file.

    The difference is who can log in, and the blueprints say it plainly:
    ``sso-only`` sets ``registrationAllowed`` and ``loginWithEmailAllowed`` to false, so
    SSO Rijk is the only way in; ``sso-support`` sets both to true and adds
    ``resetPasswordAllowed``, so local Keycloak accounts exist alongside it.

    The old labels did not say that. "SSO met ondersteuning voor applicatie-specifieke
    configuratie" describes something else entirely, and someone picking it had no way
    to know they were also turning on local accounts.
    """

    # De lijst ligt vast: elk project krijgt deze keuzes.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        """Get available Keycloak template options."""
        return [
            {
                "value": "sso-only",
                "label": "Alleen SSO Rijk",
                "description": "Inloggen kan uitsluitend via SSO Rijk. Geen lokale accounts, geen gebruikersbeheer.",
            },
            {
                "value": "sso-support",
                "label": "SSO Rijk en lokale Keycloak-accounts",
                "description": "Naast SSO Rijk kunnen er accounts in het Keycloak-realm van dit project bestaan.",
            },
        ]


class PullPolicyOptionsProvider:
    """Provides Kubernetes image pull policy options."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available image pull policy options."""
        return [
            {"value": "Always", "label": "Always"},
            {"value": "IfNotPresent", "label": "IfNotPresent"},
            {"value": "Never", "label": "Never"},
        ]


class BaseDomainOptionsProvider:
    """Provides base domain options for deployment URLs."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get available base domain options."""
        return [
            {"value": "", "label": "Standaard (clusternaam)"},
            {"value": "rijksapp.nl", "label": "rijksapp.nl"},
            {"value": CUSTOM_DOMAIN_SENTINEL, "label": "Eigen domein..."},
        ]


class ClusterBaseDomainOptionsProvider:
    """Provides base domain options based on the selected cluster.

    Reads supported nice-URL domains from CLUSTER_CONFIG. When no cluster
    is specified, returns all known domains across all clusters.
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "De domeinen die het cluster van deze deployment aanbiedt (nice_url in de "
            "clusterconfiguratie). Leeg betekent het standaarddomein van het cluster. Dit is "
            "geen gesloten verzameling: een eigen domein zet je door de domeinnaam zelf in dit "
            "veld te schrijven, en 'custom-domain-certificates' in hetzelfde antwoord zegt of "
            "dit cluster daar een certificaat voor kan uitgeven."
        ),
        endpoint="GET /api/v2/projects/{project_name}/clusters",
        path="clusters[].base-domains[].value",
    )

    def __init__(self, cluster: str | None = None) -> None:
        self.cluster = cluster

    def get_options(self) -> list[dict[str, Any]]:
        """Get base domain options, filtered by cluster.

        When no cluster is explicitly provided, falls back to the
        CLUSTER_MANAGER setting (the cluster this OPI instance manages).
        """
        from opi.core.config import settings

        def _extract_domain(entry: str | dict[str, Any]) -> str:
            return entry["domain"] if isinstance(entry, dict) else entry

        cluster = self.cluster or settings.CLUSTER_MANAGER
        if cluster and cluster in CLUSTER_CONFIG:
            postfix = CLUSTER_CONFIG[cluster].get("ingress_postfix", "")
            default_label = f"Cluster standaard ({postfix.lstrip('.')})" if postfix else "Cluster standaard"
            options: list[dict[str, Any]] = [{"value": "", "label": default_label}]

            raw = CLUSTER_CONFIG[cluster].get("nice_url", {}).get("supported_domains", [])
            domains = [_extract_domain(d) for d in raw]
            options.extend({"value": d, "label": d} for d in domains)
            options.append({"value": CUSTOM_DOMAIN_SENTINEL, "label": "Eigen domein..."})
            return options

        # Fallback: no matching cluster config - return empty with custom option
        return [
            {"value": "", "label": "Cluster standaard"},
            {"value": CUSTOM_DOMAIN_SENTINEL, "label": "Eigen domein..."},
        ]


class FilteredServiceOptionsProvider:
    """
    Provides service options filtered to project-level enabled services.

    Used by component `services` checkbox group. Only shows services
    that the project has enabled (cross-part dependency).
    """

    def __init__(self, project_services: list[str] | None = None) -> None:
        self.project_services = project_services or []

    def get_options(self) -> list[dict[str, Any]]:
        """Get service options filtered to project-enabled services."""
        options: list[dict[str, Any]] = []
        for service_type in ServiceType:
            if service_type.value not in self.project_services:
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
    """
    Provides component names from the project as select options.

    Used by deployment component reference selects (cross-part dependency).
    """

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
        """Get component name options, excluding already-used references."""
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
        from opi.handlers.project_file_handler import create_project_file_handler
        from opi.services import ServiceAdapter

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


class RepositoryOptionsProvider:
    """
    Provides repository names from the project as select options.

    Used by deployment repository selects (cross-part dependency).
    """

    def __init__(self, repository_names: list[str] | None = None) -> None:
        self.repository_names = repository_names or []

    def get_options(self) -> list[dict[str, Any]]:
        """Get repository name options."""
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


# Registry of all available providers
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
        from opi.handlers.project_file_handler import extract_attachment_catalog

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

    # De lijst ligt vast: elk project krijgt deze keuzes.
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

#: Wat 'aangeleverd' heet zolang het project geen bijlage heeft om aan te leveren.
#: ``PublishOnWebComponentConfig`` weigert ``tls: provided`` zonder ``attachment``, en het
#: bijlageveld dat ernaast verschijnt heeft bij een lege catalogus geen enkele waarde om te
#: kiezen -- dus wie de modus toch koos kwam in een scherm dat hij niet kon opslaan en niet
#: kon herstellen. De optie blijft STAAN en wordt uitgeschakeld: wie ernaar zoekt vindt hem
#: met de reden erbij, waar een optie die stil verdwijnt alleen een tweede raadsel geeft.
_PROVIDED_WITHOUT_CERTIFICATE = "Eigen certificaat op de ingress - upload eerst een certificaat bij Bijlagen"


def publish_tls_mode_options(yaml_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """De drie TLS-modi, met 'provided' uitgeschakeld zolang er geen bijlage is.

    Eén helper voor beide lagen (component en de per-deployment override): een modus die
    op het component niet te kiezen is, moet dat op de override ook niet zijn.

    Zonder project (een kale render, een voorbeeld) blijft alles staan. Dat is dezelfde
    keuze als bij het erf-label van ``PublishTlsOverrideOptionsProvider``: geen gegevens is
    geen reden om te gokken, en een lege catalogus concluderen uit een ontbrekende context
    zou de modus uitschakelen op een scherm dat er niets over weet.
    """
    # Lokaal, zoals bij AttachmentOptionsProvider hieronder: project_file_handler importeert
    # deze module langs de vormenlaag terug.
    from opi.handlers.project_file_handler import extract_attachment_catalog

    kan_aangeleverd = not yaml_data or bool(extract_attachment_catalog(yaml_data))
    return [
        {**option, "label": _PROVIDED_WITHOUT_CERTIFICATE, "disabled": True}
        if option["value"] == "provided" and not kan_aangeleverd
        else dict(option)
        for option in _PUBLISH_TLS_MODE_OPTIONS
    ]


class PublishTlsModeOptionsProvider:
    """Options for how TLS is handled on a published component.

    ``yaml_data`` is handed to every provider that accepts it (see
    ``bridge._resolve_options``); zonder project valt de lijst terug op alle drie de modi.
    """

    # De WAARDEN liggen vast: elk project krijgt deze drie. Alleen of 'provided' te kiezen
    # is hangt van de bijlagencatalogus af, en dat verandert niets aan wat de API accepteert.
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

    # Geen bron: de waarden liggen vast (dezelfde drie modi als op het component, plus leeg
    # voor erven). Alleen het LABEL van de lege keuze wordt uit het project afgeleid, en een
    # label verandert niets aan wat je mag sturen.
    options_source: ClassVar[OptionsSource | None] = None

    def __init__(self, yaml_data: dict[str, Any] | None = None, yaml_path: str | None = None) -> None:
        self._yaml_data = yaml_data or {}
        self._yaml_path = yaml_path or ""

    def _inherited_label(self) -> str:
        """What the component (or the project default) says, as an option label."""
        from opi.forms.editables.path import get_value
        from opi.handlers.project_file_handler import ProjectFileHandler

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

    # De lijst ligt vast: elk project krijgt deze keuzes.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "true", "label": "Ja"},
            {"value": "false", "label": "Nee"},
        ]


class WakeModeOptionsProvider:
    """The three sleep-mode wake modes: how a sleeping deployment is woken."""

    # De lijst ligt vast: elk project krijgt deze keuzes.
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

    # De lijst ligt vast per cluster: hij hangt niet van het project af, alleen van het
    # cluster dat deze OPI beheert, en dat is ook het cluster dat deze documentatie serveert.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        from opi.core.config import settings
        from opi.services.catalog.sleep_mode.options import sleep_after_deploy_options

        return sleep_after_deploy_options(settings.CLUSTER_MANAGER)


class SleepAfterWakeOptionsProvider:
    """How long a woken deployment stays awake before its deadline is set again."""

    # De lijst ligt vast per cluster: hij hangt niet van het project af, alleen van het
    # cluster dat deze OPI beheert, en dat is ook het cluster dat deze documentatie serveert.
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

    Reading another project's file while rendering a form is a real dependency, so it is
    kept narrow and lazy: only a project on the authorized ``_cross_domain_projects`` list is
    looked up, and the lookup is the ProjectStore's in-memory cache (no I/O). A peer that was
    deleted, or that this user may no longer see, simply yields None and the field falls back
    to keeping whatever is stored.
    """
    if not project_name:
        return None
    if project_name not in (yaml_data.get("_cross_domain_projects") or []):
        return None
    from opi.services.project_store import get_project_store

    summary = get_project_store().get(project_name)
    return summary.data if summary is not None and isinstance(summary.data, dict) else None


def _cross_domain_deployment(project_data: dict[str, Any] | None, deployment_name: str | None) -> dict[str, Any]:
    for deployment in (project_data or {}).get("deployments") or []:
        if isinstance(deployment, dict) and deployment.get("name") == deployment_name:
            return deployment
    return {}


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
        # decision for the owner of a shared facility, taken through the API or the project
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
        chosen = _cross_domain_deployment(project_data, peer.get("deployment"))
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

#: Het label van de wall-poort in een keuzelijst. Waarom 4180 erbij staat hoort in de optie
#: zelf, niet in een hulptekst onder het veld: het geldt voor een van de opties en niet voor
#: het veld, en het is een randgeval dat je alleen hoeft te snappen als je het kiest.
WALL_PORT_LABEL = f"{WALL_PORT} (via authorization wall)"


def _component_has_wall(component: dict[str, Any]) -> bool:
    """Of er een authorization-wall voor dit component staat."""
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
    It used to be one precomputed union of the project's OWN ports for both directions, on the
    stated grounds that the framework cannot filter options per row. It can: the sequence
    renderer builds an ``item_context`` PER ROW and providers receive exactly the kwargs they
    declare in ``__init__`` (``_filter_provider_kwargs``). ``exclude_references`` has always
    travelled that way, and ``row_data`` carries the row's own stored values.

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
        # data rather than precomputed per flow, which is what left it empty in the create
        # wizard; ``_cross_domain_ports`` is the pre-RC-42 precomputed union and is still read
        # so an older wizard session in flight keeps working.
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
        """Of 4180 in deze lijst van de authorization-wall komt.

        Een component mag 4180 ook gewoon zelf als inbound-poort hebben; dan is het label
        "via authorization wall" onjuist. Dus alleen labelen als er echt een wall voor staat.
        """
        if WALL_PORT not in ports:
            return False
        bron = self.yaml_data
        if _cross_domain_direction(self.yaml_path) == "outbound":
            peer = _cross_domain_peer_ref(self.row_data, self.yaml_path, self.yaml_data)
            bron = _cross_domain_peer_project_data(self.yaml_data, peer.get("project")) or {}
        return any(
            _component_has_wall(component) for component in bron.get("components") or [] if isinstance(component, dict)
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
    """Hoe een bestaand account gekoppeld wordt als iemand via een identity provider inlogt.

    De waarden komen uit ``AccountLink`` in het configmodel; ``build_project_realm_context``
    geeft ze als ``account_link`` door aan de realm-template. Niets ingevuld is de
    Keycloak-standaard: de gebruiker bevestigt de koppeling via een e-mail.

    Hier stond een derde keuze, ``verify``, met het label "Verificatie via e-mail" en de
    omschrijving "Expliciet dezelfde weg als de standaard". Dat was letterlijk waar: geen
    enkele code deed iets anders met ``verify`` dan met niets. Een keuze aanbieden die de
    uitkomst niet verandert is geen extra vrijheid maar een leugen over de werking, dus hij
    is weg -- uit de enum, uit het schema en hier.
    """

    # De lijst ligt vast: elk project krijgt deze keuzes.
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

    # De lijst ligt vast: elk project krijgt deze keuzes.
    options_source: ClassVar[OptionsSource | None] = None

    def get_options(self) -> list[dict[str, Any]]:
        return [
            {"value": "nl", "label": "Nederlands"},
            {"value": "en", "label": "Engels"},
        ]


class InviteAuthMethodOptionsProvider:
    """The auth methods an invite may allow, taken from the project's keycloak template.

    An invite can only narrow what the realm offers, never widen it (``invite_routes``
    computes ``realm_auth[x] and invite_auth_config[x]``). The realm follows the keycloak
    template, and the two blueprints differ exactly here:

    * ``sso-only``    -- registrationAllowed / loginWithEmailAllowed false: SSO only
    * ``sso-support`` -- both true: SSO and local accounts

    Offering "Lokaal account" under sso-only would therefore be a choice that silently does
    nothing. Empty selection still means "fall back to whatever the realm allows".
    """

    options_source: ClassVar[OptionsSource | None] = OptionsSource(
        description=(
            "sso kan altijd; local alleen als het keycloak-template van dit project lokale "
            "accounts toestaat (het template is dan niet sso-only)."
        ),
        endpoint="GET /api/v2/projects/{project_name}/services/keycloak/config",
        path="configurations[].config.template",
    )

    def __init__(self, yaml_data: dict[str, Any] | None = None) -> None:
        self.yaml_data = yaml_data or {}

    def get_options(self) -> list[dict[str, Any]]:
        from opi.forms.editables.service_path import smart_get_value

        options = [{"value": "sso", "label": "Single sign-on (SSO)"}]
        # An absent template means sso-only: that is the default on KeycloakConfig.template,
        # so it is what the realm actually gets. Reading it as "unknown, allow both" would
        # also disagree with the field's show_when, which hides the field in that same case.
        template = smart_get_value(self.yaml_data, "services/keycloak/config/template") or "sso-only"
        if template != "sso-only":
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

    # De lijst ligt vast: elk project krijgt deze keuzes.
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
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.services.catalog.publish_on_web.urls import public_urls_for_project

        options: list[dict[str, Any]] = [{"value": "", "label": "Geen knop tonen"}]
        try:
            urls = public_urls_for_project(self.yaml_data, ProjectFileHandler())
        except KeyError, ValueError, AttributeError, TypeError:
            # A half-configured project (no cluster yet, no domain chosen) must still
            # render the form: the picker then simply offers no destination.
            logger.debug("Could not derive public URLs for the invite destination", exc_info=True)
            urls = []

        # Een component MAG meerdere paden publiceren, en dat zijn dan evenzoveel adressen.
        # Het label noemde alleen deployment en component, dus die adressen kwamen als twee
        # regels "production / frontend" in de lijst: niet te onderscheiden, terwijl je er
        # wel een van moet kiezen. De ontdubbeling hieronder pakt ze niet, en terecht, want
        # de URL's verschillen echt. Het pad komt er dus bij, maar alleen waar het iets
        # oplost: bij een component met een enkel pad is "/" achter de naam alleen ruis.
        seen: set[str] = set()
        gekozen: list[dict[str, str]] = []
        for entry in urls:
            url = entry.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            gekozen.append(entry)

        meervoudig: set[tuple[str, str]] = set()
        geteld: set[tuple[str, str]] = set()
        for entry in gekozen:
            sleutel = (entry["deployment_name"], entry["component_name"])
            if sleutel in geteld:
                meervoudig.add(sleutel)
            geteld.add(sleutel)

        for entry in gekozen:
            label = f"{entry['deployment_name']} / {entry['component_name']}"
            if (entry["deployment_name"], entry["component_name"]) in meervoudig:
                label = f"{label} ({entry.get('path') or '/'})"
            options.append({"value": entry["url"], "label": label})

        if self.current_value and self.current_value not in seen:
            options.append({"value": self.current_value, "label": f"{self.current_value} (niet meer afleidbaar)"})
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
    "DomainModeOptionsProvider": DomainModeOptionsProvider,
    "StorageTypeOptionsProvider": StorageTypeOptionsProvider,
    "StorageSizeOptionsProvider": StorageSizeOptionsProvider,
    "KeycloakTemplateOptionsProvider": KeycloakTemplateOptionsProvider,
    "KeycloakAccountLinkOptionsProvider": KeycloakAccountLinkOptionsProvider,
    "PullPolicyOptionsProvider": PullPolicyOptionsProvider,
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
    """
    Get a provider instance by name.

    Args:
        name: Provider class name
        **kwargs: Arguments to pass to provider constructor

    Returns:
        Provider instance

    Raises:
        KeyError: If provider not found in registry
    """
    if name not in PROVIDER_REGISTRY:
        raise KeyError(f"Unknown provider: {name}")
    return PROVIDER_REGISTRY[name](**kwargs)


def get_all_providers() -> dict[str, OptionsProvider]:
    """
    Get instances of all registered providers.

    Returns:
        Dict mapping provider names to instances
    """
    return {name: cls() for name, cls in PROVIDER_REGISTRY.items()}
