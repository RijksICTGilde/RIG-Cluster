from __future__ import annotations

from typing import Any


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


class ComponentServicesEnforcer:
    """Section-level enforcer: validates component services against project services."""

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        services = extract_service_names(value.get("services", []))
        if not services:
            return value

        components = value.get("components", [])
        for comp in components:
            if not isinstance(comp, dict):
                continue
            comp_services = comp.get("services", [])
            if comp_services == "__all__" or not isinstance(comp_services, list):
                continue
            uses = extract_service_names(comp_services)
            invalid = [s for s in uses if s not in services]
            if invalid:
                comp_name = comp.get("name", "onbekend")
                invalid_str = ", ".join(invalid)
                raise ValueError(
                    f"Component '{comp_name}' gebruikt ongeldige services: {invalid_str}. "
                    f"Beschikbare services: {', '.join(services)}"
                )

        return value


class DomainConfigEnforcer:
    """Section-level enforcer: validates cross-field domain configuration.

    Checks that dependent fields are set when the chosen domain-format requires them:
    - subdomain is required for formats containing '{subdomain}'
    - custom domain is set when base-domain is the sentinel value
    - subdomain + base-domain combination is available (async DB check)
    """

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        from opi.utils.naming import DOMAIN_FORMAT_TEMPLATES

        deployments = value.get("deployments", [])
        if not deployments or not isinstance(deployments[0], dict):
            return value

        dep = deployments[0]
        domain_format = dep.get("domain-format")
        if not domain_format:
            return value

        base_domain = dep.get("base-domain")
        subdomain = dep.get("subdomain")

        if base_domain == "__custom__":
            raise ValueError("Een aangepast domein is geselecteerd maar niet ingevuld")

        template = DOMAIN_FORMAT_TEMPLATES.get(domain_format, "")
        if "{subdomain}" in template and not subdomain:
            raise ValueError("Een subdomein is vereist voor het gekozen URL-formaat")

        # Check subdomain availability for nice-URL formats
        if subdomain and base_domain and "{subdomain}" in template:
            await self._check_subdomain_availability(subdomain, base_domain, context)

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
        from opi.connectors.subdomain import SubdomainConnector

        connector = SubdomainConnector()
        registration = await connector.get_by_subdomain(subdomain.lower(), base_domain.lower())

        if registration is None:
            return  # Available

        # On edit: allow if same project owns it
        project_name = context.get("project_name")
        if project_name and registration.get("project_name") == project_name:
            return  # Owned by this project

        raise ValueError(f"Het subdomein '{subdomain}.{base_domain}' is niet beschikbaar")


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
