"""platform service (hidden, always-on platform variables)."""

from __future__ import annotations

from opi.services.catalog.base import ProjectManifestContext, ProjectManifestSpec, Service
from opi.services.catalog.platform.network_policies import namespace_network_policy_specs
from opi.services.catalog.platform.variables import PlatformVariables
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ServiceKind, ServiceType
from opi.utils.naming import generate_project_service_account_name


class PlatformService(Service):
    service_type = ServiceType.PLATFORM
    definition = ServiceDefinition(
        name="Platform",
        description="Automatisch beschikbare platform variabelen",
        help_template="platform/help.md",
        icon="info",
        color="lintblauw",
        secret_class="PlatformSecret",  # noqa: S106  # the name of the key or the secret class, not a secret
        variables=[var.value for var in PlatformVariables],
        # Always on, never chosen by a project -> a system service. kind=SYSTEM
        # also keeps it out of the picker, so an explicit hidden is not needed.
        kind=ServiceKind.SYSTEM,
    )
    # May enrol itself: a system service is not a user choice, so there is no
    # project-level decision to make first.
    allows_implicit_project_selection = True

    def contribute_project_manifests(self, ctx: ProjectManifestContext) -> list[ProjectManifestSpec]:
        """The project's own service account and the namespace-wide NetworkPolicies.

        With the platform and not with image-registries or a database service, because
        every project needs them, including a project without a private registry or a
        dedicated database. See ``generate_project_service_account_name`` and
        ``namespace_network_policy_specs``.
        """
        name = generate_project_service_account_name(ctx.project_name)
        return [
            ProjectManifestSpec(
                filename=f"{self.service_type.value}-{name}",
                template_path="project-serviceaccount.yaml.jinja",
                values={"name": name, "namespace": ctx.namespace},
            ),
            *namespace_network_policy_specs(
                cluster=ctx.cluster, project_name=ctx.project_name, namespace=ctx.namespace
            ),
        ]
