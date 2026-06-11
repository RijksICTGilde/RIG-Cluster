from __future__ import annotations

from typing import Any

from opi.connectors.subdomain import (
    BARE_DOMAIN_SUBDOMAIN,
    SubdomainConnector,
    get_project_allowed_domain_config,
    get_subdomain_status,
    get_supported_base_domains,
    is_domain_allowed_for_project,
    is_subdomain_allowed_for_project,
)
from opi.core import config as opi_config
from opi.core.cluster_config import get_domain_supports_dots
from opi.services.resource_analyzer import parse_k8s_memory_to_mi
from opi.utils.naming import DOMAIN_FORMAT_TEMPLATES


class FieldError(ValueError):
    """Validation error tied to a specific field path.

    When raised from an enforcer, the error message is attached to
    the given field path in the form errors dict instead of appearing
    as a global/section-level banner.
    """

    def __init__(self, field_path: str, message: str) -> None:
        self.field_path = field_path
        super().__init__(message)


class FieldWarning(Exception):
    """Non-blocking warning tied to a specific field path.

    When raised from an enforcer, the warning message is attached to
    the given field path in the form warnings dict. Unlike errors,
    warnings do not block form submission.
    """

    def __init__(self, field_path: str, message: str) -> None:
        self.field_path = field_path
        self.message = message
        super().__init__(message)


class AdminRequiredEnforcer:
    """Ensures at least one user has role='admin'."""

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        if not value or not isinstance(value, list):
            raise ValueError("Er moet minimaal één gebruiker zijn")
        has_admin = any(isinstance(user, dict) and user.get("role") == "admin" for user in value)
        if not has_admin:
            raise ValueError("Er moet minimaal één administrator zijn")
        return value


class UniqueNamesEnforcer:
    """Ensures all items in a sequence have unique values for a given field."""

    def __init__(self, field_name: str = "name") -> None:
        self.field_name = field_name

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        if not value or not isinstance(value, list):
            return value
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = item.get(self.field_name)
                if name:
                    names.append(str(name))
        duplicates = [name for name in set(names) if names.count(name) > 1]
        if duplicates:
            dup_str = ", ".join(sorted(duplicates))
            raise ValueError(f"Dubbele namen gevonden: {dup_str}")
        return value


class UniqueReferencesEnforcer:
    """Ensures deployment components have unique references."""

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        deployments = value.get("deployments", []) if isinstance(value, dict) else []
        for deployment in deployments:
            if not isinstance(deployment, dict):
                continue
            components = deployment.get("components", [])
            refs: list[str] = [c["reference"] for c in components if isinstance(c, dict) and c.get("reference")]
            duplicates = [r for r in set(refs) if refs.count(r) > 1]
            if duplicates:
                name = deployment.get("name", "onbekend")
                dup_str = ", ".join(sorted(duplicates))
                raise ValueError(f"Deployment '{name}' heeft dubbele componenten: {dup_str}")
        return value


def extract_service_names(services: list[Any]) -> list[str]:
    """Extract service names from the mixed services list format.

    Handles all formats in the services list:
      - strings: "keycloak"
      - service-keyed dicts: {"keycloak": {"config": ...}}
      - legacy name dicts: {"name": "keycloak"}
    """
    result: list[str] = []
    for svc in services:
        if isinstance(svc, str):
            result.append(svc)
        elif isinstance(svc, dict):
            if "name" in svc:
                result.append(svc["name"])
            else:
                result.extend(svc.keys())
    return result


def _validate_memory_request_limit(comp_index: int, request_val: str, limit_val: str) -> None:
    """Raise FieldError on the limit field if memory request exceeds the limit."""
    try:
        request_mi = parse_k8s_memory_to_mi(str(request_val))
        limit_mi = parse_k8s_memory_to_mi(str(limit_val))
    except ValueError:
        return  # Invalid values are caught by field-level validators

    if request_mi > limit_mi:
        raise FieldError(
            f"components[{comp_index}]/resources/limits/memory",
            f"Geheugen limiet ({limit_val}) mag niet lager zijn dan de request ({request_val})",
        )


class ComponentServicesEnforcer:
    """Section-level enforcer: validates component services and memory request/limit."""

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        services = extract_service_names(value.get("services", []))

        components = value.get("components", [])
        for i, comp in enumerate(components):
            if not isinstance(comp, dict):
                continue

            # Validate services
            if services:
                comp_services = comp.get("services", [])
                if isinstance(comp_services, list):
                    uses = extract_service_names(comp_services)
                    invalid = [s for s in uses if s not in services]
                    if invalid:
                        comp_name = comp.get("name", "onbekend")
                        invalid_str = ", ".join(invalid)
                        raise ValueError(
                            f"Component '{comp_name}' gebruikt ongeldige services: {invalid_str}. "
                            f"Beschikbare services: {', '.join(services)}"
                        )

            # Validate memory request <= limit
            resources = comp.get("resources", {})
            if isinstance(resources, dict):
                requests = resources.get("requests", {}) or {}
                limits = resources.get("limits", {}) or {}
                request_val = requests.get("memory") if isinstance(requests, dict) else None
                limit_val = limits.get("memory") if isinstance(limits, dict) else None
                if request_val and limit_val:
                    _validate_memory_request_limit(i, request_val, limit_val)

        return value


class UniqueDeploymentNameEnforcer:
    """Ensures a new deployment name does not collide with existing deployments.

    Only checks the deployment at ``deployment_index``, not all deployments
    in the data (which includes pre-existing ones).
    """

    def __init__(self, deployment_index: int = 0) -> None:
        self.deployment_index = deployment_index

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        deployments = value.get("deployments", []) if isinstance(value, dict) else []
        if self.deployment_index >= len(deployments):
            return value
        dep = deployments[self.deployment_index]
        if not isinstance(dep, dict):
            return value
        name = dep.get("name")
        existing_names = context.get("existing_deployment_names", [])
        if name and name in existing_names:
            raise ValueError(f"Er bestaat al een deployment met de naam '{name}'")
        return value


class DomainConfigEnforcer:
    """Section-level enforcer: validates cross-field domain configuration.

    Checks that dependent fields are set when the chosen domain-format requires them:
    - subdomain is required for formats containing '{subdomain}'
    - custom domain is set when base-domain is the sentinel value
    - subdomain + base-domain combination is available (async DB check)
    """

    def __init__(self, deployment_index: int = 0) -> None:
        self.deployment_index = deployment_index

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        deployments = value.get("deployments", [])
        if len(deployments) <= self.deployment_index:
            return value
        dep = deployments[self.deployment_index]
        if not isinstance(dep, dict):
            return value
        domain_format = dep.get("domain-format")
        if not domain_format:
            return value

        base_domain = dep.get("base-domain")
        custom_domain = dep.get("base-domain:custom")
        subdomain = dep.get("subdomain")

        cluster = opi_config.settings.CLUSTER_MANAGER
        supported = get_supported_base_domains(cluster)

        # When base-domain is "__custom__", user selected custom domain input
        # Validate that they actually filled it in
        if base_domain == "__custom__":
            if not custom_domain:
                raise ValueError("Een aangepast domein is geselecteerd maar niet ingevuld")
            # Use custom domain for further validation
            actual_domain = custom_domain
        elif base_domain:
            actual_domain = base_domain
        else:
            # Resolve default domain when not explicitly selected
            actual_domain = next(iter(supported)) if supported else None

        template = DOMAIN_FORMAT_TEMPLATES.get(domain_format, "")
        if "{subdomain}" in template and not subdomain:
            # Field-level required validation only fires when the field is
            # rendered. If the user changed domain-format to one that needs a
            # subdomain but never filled it (or the field was hidden when
            # processed), surface the error against the subdomain input so it
            # is visible — not against the parent group path.
            raise FieldError(
                f"deployments[{self.deployment_index}]/subdomain",
                "Een subdomein is vereist voor het gekozen URL-formaat",
            )

        # Check if domain format (with dots) is compatible with the selected domain
        if actual_domain and "." in domain_format:
            supports_dots = get_domain_supports_dots(cluster, actual_domain)
            # For custom domains not in cluster config, check project-level config
            if not supports_dots and actual_domain.lower() not in supported:
                custom_config = get_project_allowed_domain_config(value, actual_domain)
                if custom_config:
                    supports_dots = custom_config.get("supports-dots", False)
            if not supports_dots:
                raise ValueError(
                    f"Het gekozen URL-formaat ondersteunt geen punten in de domeinnaam. "
                    f"Dit domein ({actual_domain}) ondersteunt punten niet. "
                    f"Kies een ander URL-formaat of een ander domein."
                )

        # Check domain approval for any non-platform domain (a domain not in the
        # cluster's supported set), whether it arrived via the wizard's custom
        # input ("__custom__") or as a literal base-domain on an API upsert.
        # Mirrors the subdomain pattern below: a ticked "Domein aanvragen" checkbox lets
        # the submission through, an existing "requested" status lets it
        # through, "denied" hard-fails, and any other unapproved state
        # surfaces as a non-blocking warning prompting the user to tick the
        # request checkbox.
        if actual_domain and actual_domain.lower() not in supported:
            domain_field = (
                f"deployments[{self.deployment_index}]/base-domain:custom"
                if base_domain == "__custom__"
                else f"deployments[{self.deployment_index}]/base-domain"
            )
            is_allowed, error_msg = is_domain_allowed_for_project(actual_domain, value)
            if not is_allowed:
                domain_config = get_project_allowed_domain_config(value, actual_domain)
                status = domain_config.get("status") if isinstance(domain_config, dict) else None
                request_checked = bool(dep.get("_request-domain"))
                if status is None and request_checked:
                    pass  # User is requesting this domain — allow through
                elif status == "requested":
                    pass  # Already requested — allow through
                elif status == "denied":
                    msg = error_msg or f"Het domein '{actual_domain}' is afgewezen."
                    raise FieldError(domain_field, msg)
                else:
                    raise FieldWarning(
                        domain_field,
                        f"Gebruik van het domein '{actual_domain}' is op aanvraag.",
                    )

        # Check subdomain restrictions for restricted domains
        if subdomain and actual_domain and "{subdomain}" in template:
            subdomain_field = f"deployments[{self.deployment_index}]/subdomain"
            is_allowed, error_msg = is_subdomain_allowed_for_project(subdomain, actual_domain, value, cluster)
            if not is_allowed:
                status = get_subdomain_status(value, actual_domain, subdomain)
                request_checked = dep.get("_request-subdomain")
                if status is None and request_checked:
                    pass  # User is requesting this subdomain — allow through
                elif status == "requested":
                    pass  # Already requested — allow through
                elif status == "denied":
                    raise FieldError(subdomain_field, error_msg)
                else:
                    raise FieldWarning(
                        subdomain_field,
                        f"Gebruik van een subdomein '{subdomain}' voor het domein '{actual_domain}' is op aanvraag.",
                    )

        # Check subdomain availability for nice-URL formats
        if subdomain and actual_domain and "{subdomain}" in template:
            await self._check_subdomain_availability(subdomain, actual_domain, context)

        # Validate bare domain component: only valid with custom domains
        bare_domain_component = dep.get("expose-component-on-bare-domain")
        if bare_domain_component and actual_domain:
            if actual_domain.lower() in supported:
                raise ValueError("Kaal domein is alleen beschikbaar voor eigen domeinen, niet voor platformdomeinen")
            await self._check_bare_domain_availability(actual_domain, context)

        return value

    @staticmethod
    async def _check_subdomain_availability(
        subdomain: str,
        base_domain: str,
        context: dict[str, Any],
    ) -> None:
        """Check if the subdomain + base_domain pair is available.

        Skips the check when the current project already owns the registration
        (edit mode).
        """
        connector = SubdomainConnector()
        registration = await connector.get_by_subdomain(subdomain.lower(), base_domain.lower())

        if registration is None:
            return  # Available

        # On edit: allow if same project owns it
        project_name = context.get("project_name")
        if project_name and registration.get("project_name") == project_name:
            return  # Owned by this project

        raise ValueError(f"Het subdomein '{subdomain}.{base_domain}' is niet beschikbaar")

    @staticmethod
    async def _check_bare_domain_availability(
        base_domain: str,
        context: dict[str, Any],
    ) -> None:
        """Check if the bare domain is available for registration.

        Skips the check when the current project already owns the registration
        (edit mode).
        """
        connector = SubdomainConnector()
        registration = await connector.get_by_subdomain(BARE_DOMAIN_SUBDOMAIN, base_domain.lower())

        if registration is None:
            return  # Available

        # On edit: allow if same project owns it
        project_name = context.get("project_name")
        if project_name and registration.get("project_name") == project_name:
            return  # Owned by this project

        raise ValueError(f"Het kale domein '{base_domain}' is niet beschikbaar")


class ServiceDependencyEnforcer:
    """Ensures component services are valid project-level services."""

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        if not value or not isinstance(value, list):
            return value
        project_services = context.get("project_services", [])
        if not project_services:
            return value
        invalid = [s for s in value if s not in project_services]
        if invalid:
            invalid_str = ", ".join(invalid)
            raise ValueError(f"Ongeldige services: {invalid_str}. Beschikbare services: {', '.join(project_services)}")
        return value
