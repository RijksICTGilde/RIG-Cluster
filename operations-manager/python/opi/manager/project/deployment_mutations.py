"""Creating and updating a deployment in the project file."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from opi.core.config import settings
from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldWarning
from opi.manager.clone_validation import derive_deployment_from_source, resolve_clone_source_deployment
from opi.manager.project.notices import approval_notices, certificate_warnings
from opi.manager.project_validation import validate_component_references
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, set_domain_setting
from opi.services.catalog.publish_on_web.subdomain import ensure_domain_requests
from opi.services.project import Project
from opi.services.project_environment import Omgeving, environment_markings, log_production_downgrades
from opi.services.project_store import ConcurrencyError, ConflictError
from opi.services.project_utils import normalize_container_image
from opi.services.redeploy import run_redeploy_hooks
from opi.services.services import deployment_index

if TYPE_CHECKING:
    from opi.manager.project.access import ProjectFileAccess

logger = logging.getLogger(__name__)

# Matches the store's MAX_MUTATION_ATTEMPTS.
_UPSERT_CONFLICT_RETRIES = 5


class ComponentReference(Protocol):
    """What an upsert needs of the router's ComponentReference: which component, which image."""

    reference: str
    image: str


@dataclass(frozen=True)
class DeploymentUpsertRequest:
    """What one upsert call asks for."""

    deployment_name: str
    components: list[ComponentReference]
    clone_from: str | None = None
    force_clone: bool = False
    domain_format: str | None = None
    subdomain: str | None = None
    base_domain: str | None = None
    environment: str | None = None
    actor: str | None = None

    @property
    def changes_domain_settings(self) -> bool:
        """Whether this call sets any domain field, and so has to pass the domain enforcer."""
        return self.domain_format is not None or self.base_domain is not None or self.subdomain is not None


def _normalize_component_image(component: ComponentReference) -> tuple[str, str | None]:
    """The image as the OCI spec requires it (lowercase), plus a warning when that changed it."""
    normalized, was_normalized = normalize_container_image(component.image)
    if not was_normalized:
        return normalized, None
    return normalized, (
        f"Image for component '{component.reference}' was normalized to lowercase: "
        f"'{component.image}' -> '{normalized}'"
    )


def _apply_component_images(
    request: DeploymentUpsertRequest, deployment: dict[str, Any], change_parts: list[str]
) -> list[str]:
    """Point the deployment's components at the requested images; returns the warnings.

    ``change_parts`` is appended to while mutating, not compared afterwards: by then the
    old image is gone from the deployment and every comparison says "no change".
    """
    existing_components = {c["reference"]: c for c in deployment.get("components", [])}
    warnings: list[str] = []

    for component in request.components:
        normalized_image, warning = _normalize_component_image(component)
        if warning:
            warnings.append(warning)

        if component.reference in existing_components:
            old_image = existing_components[component.reference].get("image", "")
            existing_components[component.reference]["image"] = normalized_image
            if old_image != normalized_image:
                change_parts.append(f"{component.reference} image to {normalized_image}")
            logger.info(f"Updated image for component '{component.reference}' to '{normalized_image}'")
        else:
            deployment["components"].append({"reference": component.reference, "image": normalized_image})
            change_parts.append(f"add component {component.reference}")
            logger.info(f"Added new component '{component.reference}' with image '{normalized_image}'")

    return warnings


def _update_commit_message(request: DeploymentUpsertRequest, project_name: str, change_parts: list[str]) -> str:
    """Name in the commit what this update changed, or "configuration" when nothing shows."""
    if request.domain_format is not None:
        change_parts.append(f"domain-format to {request.domain_format}")
    if request.subdomain is not None:
        change_parts.append(f"subdomain to {request.subdomain}")
    if request.base_domain is not None:
        change_parts.append(f"base-domain to {request.base_domain}")
    if request.environment is not None:
        change_parts.append(f"environment to {request.environment}")
    if request.clone_from and request.force_clone:
        change_parts.append(f"clone-from {request.clone_from}")

    details = ", ".join(change_parts) if change_parts else "configuration"
    return f"Update deployment '{request.deployment_name}' in project '{project_name}': {details}"


@dataclass
class _DeploymentDraft:
    """A deployment under construction, before it joins the project file.

    The clone path writes ``requested_domain_settings`` a second time: the copy from the
    source lands on the same service block they go into.
    """

    deployment: dict[str, Any]
    requested_domain_settings: dict[DomainSetting, str]
    warnings: list[str]


def _build_new_deployment(request: DeploymentUpsertRequest) -> _DeploymentDraft:
    """The deployment a create starts from: name, components, environment, domain settings."""
    new_deployment: dict[str, Any] = {"name": request.deployment_name, "components": []}
    if request.environment is not None:
        new_deployment["environment"] = Omgeving(request.environment).value

    requested_domain_settings: dict[DomainSetting, str] = {}
    if request.domain_format:
        requested_domain_settings[DomainSetting.DOMAIN_FORMAT] = request.domain_format
    if request.subdomain:
        requested_domain_settings[DomainSetting.SUBDOMAIN] = request.subdomain
    if request.base_domain:
        requested_domain_settings[DomainSetting.BASE_DOMAIN] = request.base_domain
    for setting, setting_value in requested_domain_settings.items():
        set_domain_setting(new_deployment, setting, setting_value)

    warnings: list[str] = []
    for component in request.components:
        normalized_image, warning = _normalize_component_image(component)
        if warning:
            warnings.append(warning)
        new_deployment["components"].append({"reference": component.reference, "image": normalized_image})

    return _DeploymentDraft(new_deployment, requested_domain_settings, warnings)


def _clone_onto_new_deployment(
    request: DeploymentUpsertRequest, project_data: dict[str, Any], project_name: str, draft: _DeploymentDraft
) -> None:
    """Copy the source deployment's config onto the new one, and record the clone."""
    # Exact name match, not a jsonpath filter built from the reference: such a filter also
    # matches crafted values.
    source_deployment = Project(project_data).find("deployments", name=request.clone_from)
    if not source_deployment:
        raise ValueError(f"Source deployment '{request.clone_from}' not found in project '{project_name}'")

    logger.info(f"Cloning deployment configuration from '{request.clone_from}'")

    # Images come from the request, so components stay out of the derivation.
    draft.deployment.update(
        derive_deployment_from_source(source_deployment, request.deployment_name, draft.requested_domain_settings)
    )
    draft.deployment["clone-from"] = {"type": "deployment", "reference": request.clone_from, "mode": "once"}


def _infer_missing_deployment_fields(
    new_deployment: dict[str, Any], project_data: dict[str, Any], project_name: str
) -> dict[str, Any] | None:
    """Fill cluster, namespace and repository from the project. Returns an error, or None.

    A project with more than one cluster or repository cannot be guessed for: picking the
    first would roll out somewhere the caller did not ask for.
    """
    if not new_deployment.get("cluster"):
        project_clusters = project_data.get("clusters", [])
        if len(project_clusters) == 1:
            new_deployment["cluster"] = project_clusters[0]
        elif len(project_clusters) > 1:
            logger.error(
                f"Multiple clusters defined in project '{project_name}': {project_clusters}. Cluster must be specified explicitly for new deployment."
            )
            return {
                "success": False,
                "created": False,
                "error": "Multiple clusters defined, cluster must be specified explicitly",
                "error_type": "ambiguous_cluster",
            }

    if not new_deployment.get("namespace"):
        new_deployment["namespace"] = project_name

    if not new_deployment.get("repository"):
        repositories = project_data.get("repositories", [])
        if len(repositories) == 1:
            new_deployment["repository"] = repositories[0]["name"]
        elif len(repositories) > 1:
            repo_names = [repo["name"] for repo in repositories]
            error_msg = f"Multiple repositories defined in project '{project_name}': {repo_names}. Repository must be specified explicitly for new deployment."
            logger.error(error_msg)
            return {
                "success": False,
                "created": False,
                "error": error_msg,
                "error_type": "ambiguous_repository",
            }
        else:
            error_msg = "No repositories found in project configuration"
            logger.error(error_msg)
            return {"success": False, "created": False, "error": error_msg, "error_type": "no_repositories"}

    return None


class DeploymentMutations:
    """The deployment writers of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: ProjectFileAccess) -> None:
        self._host = host

    async def upsert_deployment(
        self,
        deployment_name: str,
        components: list,  # ComponentReference objects from router
        clone_from: str | None = None,
        force_clone: bool = False,
        domain_format: str | None = None,
        subdomain: str | None = None,
        base_domain: str | None = None,
        environment: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a deployment, retrying a concurrent-edit conflict.

        Every attempt re-reads the project through ``get_contents()`` and re-applies the
        change on top. Only a concurrent-edit conflict is retried; every other outcome
        returns to the caller.
        """
        request = DeploymentUpsertRequest(
            deployment_name=deployment_name,
            components=components,
            clone_from=clone_from,
            force_clone=force_clone,
            domain_format=domain_format,
            subdomain=subdomain,
            base_domain=base_domain,
            environment=environment,
            actor=actor,
        )
        for attempt in range(_UPSERT_CONFLICT_RETRIES):
            try:
                return await self._upsert_deployment_once(request)
            except (ConflictError, ConcurrencyError) as e:
                if attempt == _UPSERT_CONFLICT_RETRIES - 1:
                    logger.warning(
                        "Upsert of '%s' still conflicts after %d attempts; giving up",
                        deployment_name,
                        _UPSERT_CONFLICT_RETRIES,
                    )
                    return {
                        "success": False,
                        "created": False,
                        "error": f"Error upserting deployment '{deployment_name}': {e}",
                        "error_type": "conflict",
                    }
                logger.info(
                    "Upsert of '%s' hit a concurrent edit (attempt %d/%d); re-reading and retrying",
                    deployment_name,
                    attempt + 1,
                    _UPSERT_CONFLICT_RETRIES,
                )
        # Unreachable: the loop either returns a result or the final-attempt error.
        return {"success": False, "created": False, "error": "upsert retry loop exhausted", "error_type": "conflict"}

    async def _upsert_deployment_once(self, request: DeploymentUpsertRequest) -> dict[str, Any]:
        """One attempt at creating or updating a deployment; ``upsert_deployment`` retries.

        An absent deployment is created, an existing one has its component images updated.
        ``clone_from`` is honoured on create, and on update only with ``force_clone``:
        cloning an existing deployment's config over itself is otherwise a silent reset of
        everything the owner changed since.
        """
        try:
            project_data = await self._host.get_contents()
            project_name = await self._host.get_name()

            # V2 validates synchronously (400 to caller), but project changes between accept
            # and run, so we validate here too.
            validation_result = self._validate_component_references(project_data, request.components, "deployment")
            if not validation_result["success"]:
                return {
                    "success": False,
                    "created": False,
                    "error": validation_result["error"],
                    "error_type": "invalid_component_references",
                }

            # The markings from BEFORE this change, so a downgrade away from production can
            # be reported once the new state is there.
            environment_before = environment_markings(project_data)

            if await self._deployment_exists(request.deployment_name):
                return await self._update_deployment(request, project_data, project_name, environment_before)
            return await self._create_deployment(request, project_data, project_name)

        except ConflictError, ConcurrencyError:
            # Retried by upsert_deployment on a fresh read. Must propagate, not become a
            # generic error dict, or the retry never sees it.
            raise
        except Exception as e:
            error_msg = f"Error upserting deployment '{request.deployment_name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "created": False, "error": error_msg, "error_type": "internal_error"}

    async def _deployment_exists(self, deployment_name: str) -> bool:
        """Whether the project already has a deployment of this name, on any cluster."""
        deployments = await self._host.get_deployments(cluster_filter=False)
        return any(deployment.get("name") == deployment_name for deployment in deployments)

    def _apply_deployment_settings(
        self, request: DeploymentUpsertRequest, deployment: dict[str, Any], project_data: dict[str, Any]
    ) -> None:
        """Write the requested environment, domain and clone settings onto the deployment."""
        if request.environment is not None:
            # .value: the v1 route passes an enum member, and the YAML dumper knows plain
            # strings only.
            deployment["environment"] = Omgeving(request.environment).value

        if request.domain_format is not None:
            set_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT, request.domain_format)
        if request.subdomain is not None:
            set_domain_setting(deployment, DomainSetting.SUBDOMAIN, request.subdomain)
        if request.base_domain is not None:
            set_domain_setting(deployment, DomainSetting.BASE_DOMAIN, request.base_domain)

        if request.clone_from and request.force_clone:
            resolve_clone_source_deployment(project_data, request.clone_from, request.deployment_name)
            deployment["clone-from"] = {
                "type": "deployment",
                "reference": request.clone_from,
                "mode": "once",
            }
            logger.info(f"Setting clone-from to deployment '{request.clone_from}' (force_clone=true)")

    async def _domain_validation(
        self, request: DeploymentUpsertRequest, project_data: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """The error result when the requested domain config is invalid, plus the warnings.

        Gated on the request actually setting a domain field, so an image-only update never
        fails on a deployment whose domain config predates this check.
        """
        if not request.changes_domain_settings:
            return None, []

        domain_error = await self._enforce_domain_config(project_data, request.deployment_name)
        if domain_error:
            return {
                "success": False,
                "created": False,
                "error": domain_error,
                "error_type": "domain_validation",
            }, []
        return None, list(certificate_warnings(project_data, request.deployment_name))

    async def _update_deployment(
        self,
        request: DeploymentUpsertRequest,
        project_data: dict[str, Any],
        project_name: str,
        environment_before: dict[str, Omgeving],
    ) -> dict[str, Any]:
        """Apply the request to the deployment that is already there, and commit."""
        logger.info(f"Updating existing deployment '{request.deployment_name}' in project '{project_name}'")

        change_parts: list[str] = []

        normalized_warnings: list[str] = []
        deployment = Project(project_data).find("deployments", name=request.deployment_name)
        if deployment is not None:
            normalized_warnings = _apply_component_images(request, deployment, change_parts)
            self._apply_deployment_settings(request, deployment, project_data)

        domain_error, certificate_notes = await self._domain_validation(request, project_data)
        if domain_error:
            return domain_error
        normalized_warnings.extend(certificate_notes)

        # The services clear what they recorded about the old content through this hook
        # (RC-37), for the components this call names.
        state_notices: list[str] = []
        if deployment is not None:
            state_notices = await run_redeploy_hooks(
                project_name,
                project_data,
                deployment,
                [component.reference for component in request.components],
            )

        ensure_domain_requests(project_data, settings.CLUSTER_MANAGER)

        commit_message = _update_commit_message(request, project_name, change_parts)

        await self._host.save_and_commit_project(project_data, commit_message)

        # After the save: if it refused the change, nothing was downgraded.
        log_production_downgrades(
            environment_before,
            environment_markings(project_data),
            project_name=project_name,
            actor=request.actor or "de API",
        )

        logger.info(f"Successfully updated deployment '{request.deployment_name}' in project '{project_name}'")
        result: dict[str, Any] = {
            "success": True,
            "created": False,
            "error": None,
            "error_type": None,
            "approvals": approval_notices(project_data, request.deployment_name),
        }
        if normalized_warnings:
            result["warnings"] = normalized_warnings
        if state_notices:
            result["state_cleared"] = state_notices
        return result

    async def _create_deployment(
        self, request: DeploymentUpsertRequest, project_data: dict[str, Any], project_name: str
    ) -> dict[str, Any]:
        """Build the deployment the request asks for, add it to the project, and commit."""
        logger.info(f"Creating new deployment '{request.deployment_name}' in project '{project_name}'")

        draft = _build_new_deployment(request)
        if request.clone_from:
            _clone_onto_new_deployment(request, project_data, project_name, draft)
        new_deployment = draft.deployment
        normalized_warnings_create = draft.warnings

        inference_error = _infer_missing_deployment_fields(new_deployment, project_data, project_name)
        if inference_error:
            return inference_error

        # A project created through POST /api/v2/projects has no `deployments` key, so the
        # first deployment added to it creates the list (RC-66).
        project_data.setdefault("deployments", []).append(new_deployment)

        domain_error, certificate_notes = await self._domain_validation(request, project_data)
        if domain_error:
            return domain_error
        normalized_warnings_create.extend(certificate_notes)

        ensure_domain_requests(project_data, settings.CLUSTER_MANAGER)

        # Run on a create as well: there is no earlier state to clear, but a service also
        # sets here what a rollout implies, such as sleep-mode starting its clock (RC-37).
        state_notices_create = await run_redeploy_hooks(
            project_name,
            project_data,
            new_deployment,
            [component.reference for component in request.components],
        )

        commit_message = f"Add deployment '{request.deployment_name}' to project '{project_name}'"
        if request.clone_from:
            commit_message += f" (cloned from '{request.clone_from}')"

        await self._host.save_and_commit_project(project_data, commit_message)

        logger.info(f"Successfully created deployment '{request.deployment_name}' in project '{project_name}'")
        result_create: dict[str, Any] = {
            "success": True,
            "created": True,
            "error": None,
            "error_type": None,
            "approvals": approval_notices(project_data, request.deployment_name),
        }
        if normalized_warnings_create:
            result_create["warnings"] = normalized_warnings_create
        if state_notices_create:
            result_create["state_cleared"] = state_notices_create
        return result_create

    async def _enforce_domain_config(self, project_data: dict[str, Any], deployment_name: str) -> str | None:
        """Run the shared domain enforcer against an upserted deployment.

        Reuses the wizard's ``DomainConfigEnforcer``, so the API enforces the same
        cross-field domain rules, notably that a dot-separated URL format requires a
        domain that supports dots (a dash-only ODCN domain with a dot format produces an
        unreachable multi-label hostname).

        A ``FieldWarning`` is not a violation: the enforcer raises it for a domain or
        subdomain that is on request, which ``ensure_domain_requests`` then creates. The
        caller reads back where it stands under ``approvals``.

        Returns an error message on violation, or None when the config is valid.
        """
        index = deployment_index(project_data, deployment_name)
        if index is None:
            return None
        try:
            await DomainConfigEnforcer(deployment_index=index).enforce(
                project_data, {"project_name": await self._host.get_name()}
            )
        except FieldWarning as e:
            logger.info("Domain on request for deployment '%s': %s", deployment_name, e)
        except ValueError as e:  # FieldError is a ValueError subclass
            return str(e)
        return None

    def _validate_component_references(
        self, project_data: dict, components: list, context: str = "deployment"
    ) -> dict[str, Any]:
        return validate_component_references(project_data, components, context)
