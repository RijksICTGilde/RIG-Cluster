"""platform service (hidden, always-on platform variables)."""

from __future__ import annotations

from opi.services.catalog.base import ProjectManifestContext, ProjectManifestSpec, Service
from opi.services.catalog.platform.variables import PlatformVariables
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ServiceBinding, ServiceKind, ServiceType


class PlatformService(Service):
    service_type = ServiceType.PLATFORM
    definition = ServiceDefinition(
        name="Platform",
        description="Automatisch beschikbare platform variabelen",
        help_template="platform/help.md",
        icon="info",
        color="grijs-600",
        binding=ServiceBinding.COMPONENT,
        secret_class="PlatformSecret",
        variables=[var.value for var in PlatformVariables],
        # Always on, never chosen by a project -> a system service. kind=SYSTEM
        # also keeps it out of the picker, so an explicit hidden is not needed.
        kind=ServiceKind.SYSTEM,
    )
    # May enrol itself (RC-84): a system service is not a user choice, so there is no
    # project-level decision to make first.
    allows_implicit_project_selection = True

    def contribute_project_manifests(self, ctx: ProjectManifestContext) -> list[ProjectManifestSpec]:
        """De eigen serviceaccount van het project, zonder pull-secrets.

        Bij het platform en niet bij image-registries, want elk project heeft hem nodig --
        ook een project dat nooit een eigen registry opgeeft. Een dienst die alleen
        bijdraagt als er registries in staan zou hem juist daar laten ontbreken.

        Waarom hij er is: de ``default`` serviceaccount draagt elk pull-secret dat het
        platform in de namespace repliceert, dus ook dat van de proxy-organisatie van een
        ander project. Draaien op ``default`` maakt een private registry alleen op papier
        prive, en laat kubelet uit negen secrets voor dezelfde host de juiste vissen.
        Elke gegenereerde podspec draagt in plaats daarvan de secrets die hij zelf nodig
        heeft (resolve_image); meestal is dat er precies een.
        """
        from opi.utils.naming import generate_project_service_account_name

        name = generate_project_service_account_name(ctx.project_name)
        return [
            ProjectManifestSpec(
                filename=f"{self.service_type.value}-{name}",
                template_path="project-serviceaccount.yaml.jinja",
                values={"name": name, "namespace": ctx.namespace},
            )
        ]
