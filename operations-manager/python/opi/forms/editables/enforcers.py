from __future__ import annotations

from typing import Any

from opi.connectors.subdomain import (
    BARE_DOMAIN_SUBDOMAIN,
    get_project_allowed_domain_config,
    get_subdomain_status,
    get_supported_base_domains,
    is_domain_allowed_for_project,
    is_subdomain_allowed_for_project,
    validate_bare_domain_allowed,
)
from opi.core import config as opi_config
from opi.core.cluster_config import get_domain_supports_dots
from opi.services.catalog.publish_on_web.domain_config import (
    DomainSetting,
    custom_domain_certificate_note,
    domain_setting_path,
    get_domain_setting,
)
from opi.services.persistence.subdomain_registry import SubdomainConnector
from opi.services.resource_analyzer import parse_k8s_memory_to_mi
from opi.services.services import service_entry_name
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

    Delegates to ``service_entry_name``, which resolves every on-disk format: a bare
    string, the legacy service-keyed dict ({"keycloak": {...}}), and the uniform record
    keyed by ``name`` **or** ``reference``.

    Component entries use ``reference``. Falling back to the raw dict keys returned the
    literal strings "reference" and "config" as service names, which then failed
    validation as unknown services -- so a component with any configured service could
    not be saved at all.
    """
    return [name for name in (service_entry_name(svc) for svc in services) if name is not None]


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

    ``denied_blocks`` says whether a ``denied`` (sub)domain is a hard failure. It is,
    for the form: a user picking a rejected domain must be stopped at the field. It is
    not for the save gate, where the same state also arises the other way round -- an
    admin revoking an approval on a domain a deployment already uses. Blocking there
    made the revocation unsaveable, which left the approver unable to act on their own
    decision. Publication is what enforces it: ``apply_domain_approval_fallback`` moves
    an unapproved deployment back to the cluster domain on the next process run.
    """

    def __init__(self, deployment_index: int = 0, denied_blocks: bool = True) -> None:
        self.deployment_index = deployment_index
        self.denied_blocks = denied_blocks

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        deployments = value.get("deployments", [])
        if len(deployments) <= self.deployment_index:
            return value
        dep = deployments[self.deployment_index]
        if not isinstance(dep, dict):
            return value
        base_domain = get_domain_setting(dep, DomainSetting.BASE_DOMAIN)
        custom_domain = dep.get("base-domain:custom")
        subdomain = get_domain_setting(dep, DomainSetting.SUBDOMAIN)

        cluster = opi_config.settings.CLUSTER_MANAGER
        supported = get_supported_base_domains(cluster)

        # The domain this deployment actually asks for. "__custom__" means the wizard's
        # custom-domain input holds it; an empty base-domain means the cluster-default
        # URL, which is the platform default and not a user-requested domain, so there
        # is nothing to validate or approve -- None makes every domain check below skip.
        # (Previously the empty case picked an arbitrary next(iter(supported)) domain and
        # ran ITS subdomain restrictions against the deployment, wrongly rejecting
        # cluster-default PRs with e.g. "subdomein 'pr797' voor 'rijksapp.dev' is op
        # aanvraag".)
        actual_domain = custom_domain if base_domain == "__custom__" else base_domain or None

        # Bare domain, BEFORE the domain-format early return: the rule does not depend on
        # the format, and since the web address moved under the service the field is
        # writable through PUT .../services/publish-on-web/deployments/{d}/config, which
        # can set it without ever setting a domain-format. Behind the early return the
        # rule was reachable from the wizard only.
        #
        # ``validate_bare_domain_allowed`` carries the whole rule: not a platform domain,
        # and approved for THIS project. The one exemption is the same as for the
        # subdomain checks below -- an approver revoking a domain a deployment already
        # exposes on the apex must be able to save that verdict. Only an explicit
        # ``denied`` status qualifies, and only in the save gate; a domain with no entry
        # or a self-created ``requested`` entry is refused on every path. Publication
        # refuses the revoked case outright, so no apex is claimed on it.
        bare_domain_component = get_domain_setting(dep, DomainSetting.BARE_DOMAIN_COMPONENT)
        if bare_domain_component and actual_domain:
            bare_config = get_project_allowed_domain_config(value, actual_domain)
            bare_status = bare_config.get("status") if isinstance(bare_config, dict) else None
            if self.denied_blocks or bare_status != "denied":
                validate_bare_domain_allowed(actual_domain, supported, value)
            await self._check_bare_domain_availability(actual_domain, context)

        # Whether this cluster can certify the domain at all, computed here for the same
        # reason the bare-domain rule sits here: it does not depend on the domain-format,
        # and the field is writable through PUT .../config without one. Held rather than
        # raised, so a real FieldError further down still wins over a warning.
        certificate_note = custom_domain_certificate_note(cluster, actual_domain)
        certificate_field = (
            f"deployments[{self.deployment_index}]/base-domain:custom"
            if base_domain == "__custom__"
            else domain_setting_path(DomainSetting.BASE_DOMAIN, self.deployment_index)
        )

        domain_format = get_domain_setting(dep, DomainSetting.DOMAIN_FORMAT)
        if not domain_format:
            if certificate_note:
                raise FieldWarning(certificate_field, certificate_note)
            return value

        # When base-domain is "__custom__", user selected custom domain input
        # Validate that they actually filled it in
        if base_domain == "__custom__" and not custom_domain:
            raise ValueError("Een aangepast domein is geselecteerd maar niet ingevuld")

        # An id that is no template at all used to fall through here as ``""``, which made
        # every check below vacuous: 'onzin' passed the entire enforcer untouched while the
        # perfectly valid 'subdomain' was stopped for missing its subdomain. The typo was
        # the one thing that got through, and it reached a project file.
        #
        # The valid set is not restated here. It comes from the field's own values provider
        # -- the same one that fills the form's select and that the API publishes as
        # ``x-choices-source`` -- given this deployment's base-domain, so the message names
        # what this deployment can actually pick rather than all eleven ids. That context is
        # exactly what the generic ``validate_declared_choices`` gate cannot supply, which is
        # why this check lives in the enforcer (see DOMAIN_FORMAT_EDITABLE).
        template = DOMAIN_FORMAT_TEMPLATES.get(domain_format)
        if template is None:
            # Imported here rather than at module scope, for the same reason
            # ``project_validation.validate_declared_choices`` does it: this module is
            # reached from connectors and managers, and pulling the whole visualizer
            # package in at import time drags the form stack along with it. Measured, not
            # assumed -- as a top-level import it turned 33 database-backed tests into
            # errors in the full-suite run while every one of them passed in isolation.
            from opi.forms.visualizers.providers import DomainFormatOptionsProvider

            offered = [
                str(option["value"])
                for option in DomainFormatOptionsProvider(base_domain=base_domain, cluster=cluster).get_options()
            ]
            raise FieldError(
                domain_setting_path(DomainSetting.DOMAIN_FORMAT, self.deployment_index),
                f"'{domain_format}' is geen bestaand URL-formaat. Kies uit: {', '.join(offered)}.",
            )

        if "{subdomain}" in template and not subdomain:
            # Field-level required validation only fires when the field is
            # rendered. If the user changed domain-format to one that needs a
            # subdomain but never filled it (or the field was hidden when
            # processed), surface the error against the subdomain input so it
            # is visible — not against the parent group path.
            raise FieldError(
                domain_setting_path(DomainSetting.SUBDOMAIN, self.deployment_index),
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
        #
        #
        # The certificate note rides along on the same warning. Approval and certificate
        # are two different obstacles on the same value: an approver can grant the first
        # and never the second, and it is precisely AFTER approval that the certificate
        # problem starts and the approval notice disappears. One warning is the only way
        # the user hears both from the field they are filling in.
        if actual_domain and actual_domain.lower() not in supported:
            domain_field = certificate_field
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
                    if self.denied_blocks:
                        msg = error_msg or f"Het domein '{actual_domain}' is afgewezen."
                        raise FieldError(domain_field, msg)
                else:
                    warning = f"Gebruik van het domein '{actual_domain}' is op aanvraag."
                    raise FieldWarning(domain_field, f"{warning} {certificate_note}" if certificate_note else warning)

        # Check subdomain restrictions for restricted domains
        if subdomain and actual_domain and "{subdomain}" in template:
            subdomain_field = domain_setting_path(DomainSetting.SUBDOMAIN, self.deployment_index)
            is_allowed, error_msg = is_subdomain_allowed_for_project(subdomain, actual_domain, value, cluster)
            if not is_allowed:
                status = get_subdomain_status(value, actual_domain, subdomain)
                request_checked = dep.get("_request-subdomain")
                if status is None and request_checked:
                    pass  # User is requesting this subdomain — allow through
                elif status == "requested":
                    pass  # Already requested — allow through
                elif status == "denied":
                    if self.denied_blocks:
                        raise FieldError(
                            subdomain_field, error_msg or "Dit subdomein is niet toegestaan voor dit project"
                        )
                else:
                    raise FieldWarning(
                        subdomain_field,
                        f"Gebruik van een subdomein '{subdomain}' voor het domein '{actual_domain}' is op aanvraag.",
                    )

        # Check subdomain availability for nice-URL formats
        if subdomain and actual_domain and "{subdomain}" in template:
            await self._check_subdomain_availability(
                subdomain,
                actual_domain,
                context,
                field_path=domain_setting_path(DomainSetting.SUBDOMAIN, self.deployment_index),
            )

        # Last, so that a FieldError from the checks above still wins: a warning that
        # pre-empted a real failure would hide it.
        if certificate_note:
            raise FieldWarning(certificate_field, certificate_note)

        return value

    @staticmethod
    async def _check_subdomain_availability(
        subdomain: str,
        base_domain: str,
        context: dict[str, Any],
        field_path: str,
    ) -> None:
        """Check if the subdomain + base_domain pair is available.

        Skips the check when the current project already owns the registration
        (edit mode). Raises a FieldError tied to ``field_path`` (the subdomain
        input) so the message renders at the field instead of on the invisible
        deployment-group path, and names the project that holds it.
        """
        connector = SubdomainConnector()
        registration = await connector.get_by_subdomain(subdomain.lower(), base_domain.lower())

        if registration is None:
            return  # Available

        # On edit: allow if same project owns it
        project_name = context.get("project_name")
        if project_name and registration.get("project_name") == project_name:
            return  # Owned by this project

        owner = registration.get("project_name")
        owner_suffix = f", in gebruik door project '{owner}'" if owner else ""
        raise FieldError(
            field_path,
            f"Het subdomein '{subdomain}' voor domein '{base_domain}' is niet beschikbaar{owner_suffix}",
        )

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


class UniqueInviteKeyEnforcer:
    """Ensures every invite key is unique across ALL projects, not just this one.

    ``_find_project_by_invite_key`` (invite_routes) returns the first project in the whole
    store whose invite matches a key, so a key is a global namespace: two projects sharing a
    key makes the second invite silently unreachable. This section-level async enforcer reads
    the store, skips this project, and raises a ``FieldError`` on the offending key field.

    Only user-provided (non-empty) keys are checked; an empty key is filled with a generated
    128-bit random key at save time, whose collision odds are negligible. The message never
    names the other project -- that would leak the existence and ownership of other teams' keys.
    """

    _MESSAGE = "Deze uitnodigingssleutel is al in gebruik."

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        from opi.forms.editables.service_path import smart_get_value
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.services.project_store import get_project_store

        active = smart_get_value(value, "services/invite/config/active") or []
        keyed = [
            (i, str(entry.get("key"))) for i, entry in enumerate(active) if isinstance(entry, dict) and entry.get("key")
        ]
        if not keyed:
            return value

        # Duplicates within this same form.
        seen: dict[str, int] = {}
        for index, key in keyed:
            if key in seen:
                raise FieldError(f"services/invite/config/active[{index}]/key", self._MESSAGE)
            seen[key] = index

        # Collisions against every OTHER project in the store.
        own_project = context.get("project_name")
        handler = ProjectFileHandler()
        other_projects = [p for p in get_project_store().get_all() if p.name != own_project]
        for index, key in keyed:
            for project in other_projects:
                if project.data and handler.get_invite_by_key(project.data, key):
                    raise FieldError(f"services/invite/config/active[{index}]/key", self._MESSAGE)
        return value


class UniqueSchemaEnforcer:
    """Validates the extra-schema list of the postgresql-database service (RC-17).

    Per the plan's "naming fails loudly" invariant, every way a schema list can be
    unsafe is caught here, at save time, with a ``FieldError`` on the offending postfix:

    * a duplicate postfix (two schemas with the same name);
    * a postfix whose ``DATABASE_SCHEMA_{POSTFIX}`` variable collides with one the
      database service already exposes (e.g. ``db`` -> ``DATABASE_SCHEMA_DB``);
    * a full name ``{project}_{deployment}_{postfix}`` over PostgreSQL's 63-char limit
      for any current deployment (two long postfixes must not truncate to the same
      name, so the whole name is checked, not the postfix alone).

    Schemas marked for deletion are skipped: they are on their way out, so they must not
    block a save. The section is only reached when the service carries a schema list, so
    an empty list returns immediately.
    """

    _PATH = "services/postgresql-database/config/schemas"

    async def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        from opi.forms.editables.service_path import smart_get_value
        from opi.services.catalog.postgresql_database.variables import reserved_database_variable_names
        from opi.utils.naming import generate_extra_database_schema, generate_schema_variable_name

        schemas = smart_get_value(value, self._PATH) or []
        active = [
            (i, str(entry.get("postfix")))
            for i, entry in enumerate(schemas)
            if isinstance(entry, dict) and entry.get("postfix") and not entry.get("marked-for-deletion")
        ]
        if not active:
            return value

        project_name = context.get("project_name") or value.get("name") or ""
        deployment_names = [
            name for d in (value.get("deployments") or []) if isinstance(d, dict) and (name := d.get("name"))
        ]
        reserved = reserved_database_variable_names()

        seen: dict[str, int] = {}
        for index, postfix in active:
            path = f"{self._PATH}[{index}]/postfix"
            if postfix in seen:
                raise FieldError(path, f"Schema-postfix '{postfix}' is al in gebruik.")
            seen[postfix] = index

            if generate_schema_variable_name(postfix) in reserved:
                raise FieldError(
                    path,
                    f"De postfix '{postfix}' levert variabele {generate_schema_variable_name(postfix)} op, "
                    "die botst met een bestaande databasevariabele. Kies een andere postfix.",
                )

            for deployment_name in deployment_names:
                try:
                    generate_extra_database_schema(project_name, deployment_name, postfix)
                except ValueError:
                    raise FieldError(
                        path,
                        f"De volledige schemanaam voor deployment '{deployment_name}' wordt langer dan 63 tekens. "
                        "Kies een kortere postfix.",
                    ) from None
        return value


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
