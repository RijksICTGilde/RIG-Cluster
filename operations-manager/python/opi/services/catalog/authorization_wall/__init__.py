"""authorization-wall service.

Owns its project-level ``banner`` field: the wizard/edit layer sources its config
section from here. Also contributes the oauth2-proxy sidecar + service_port override
to the manifests. Depends on keycloak (see ServiceDefinition.requires).
"""

from __future__ import annotations

import secrets
from typing import Any

from opi.services.catalog.authorization_wall.config_model import AuthorizationWallConfig
from opi.services.catalog.base import (
    ConfigLayer,
    ManifestContext,
    ManifestContribution,
    SecretFileSpec,
    Service,
    config_path,
)
from opi.services.services import ServiceDefinition, service_entry_config, service_entry_name
from opi.services.services_enums import ServiceBinding, ServiceType
from opi.utils.secrets import KeycloakSecret


class AuthorizationWallService(Service):
    service_type = ServiceType.AUTHORIZATION_WALL
    definition = ServiceDefinition(
        name="Authorization Wall",
        description="OAuth2-proxy sidecar die Keycloak OIDC authenticatie afdwingt voor webapplicaties",
        icon="schild-met-vinkje-erop",
        color="groen",
        binding=ServiceBinding.COMPONENT,
        help_template="authorization_wall/help.md",
        variables=[],
        requires=[
            "services/publish-on-web",
            "services/keycloak",
            "services/keycloak/config/restrict-access",
        ],
    )
    config_model = AuthorizationWallConfig
    config_schema_version = "1.0"
    config_section_id = "auth-wall-config"
    modal_flow_id = "modal-edit-auth-wall-config"
    # After the secret services (10-50); an auth wall fronts the pod, so its
    # service_port override applies last (RC-5 Phase 6b).
    manifest_order = 60

    # --- config field ownership -------------------------------------------------
    # auth-wall owns its single project-level field (banner). The wizard/edit layer
    # sources this section from here instead of hand-authoring it in wizard_sections.
    # Forms building blocks are imported lazily so the catalog stays forms-free at
    # import time (avoids the forms -> registry -> catalog cycle).

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility: derived from this service's service_type, not a
        hardcoded service-name string."""
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        # Derived from AuthorizationWallConfig (banner), not re-declared here.
        return self.config_model_field_names() if layer is ConfigLayer.PROJECT else []

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.authorization_wall.editables import AUTH_WALL_BANNER_EDITABLE

        return [AUTH_WALL_BANNER_EDITABLE]

    def config_component_visualizers(self):
        """Een wegwijzer in het componentformulier, geen configuratie.

        Deze dienst is de enige in de catalogus die je op het ENE niveau aanzet en op het
        ANDERE instelt: aanvinken doe je per component (daar komt de oauth2-proxy voor de
        pod), maar de bannertekst is er een voor het hele project. Wie hem aanvinkte kreeg
        daarna niets te zien en had geen enkele aanwijzing waar die instelling dan wel
        staat.

        Het blijft bij een verwijzing. De echte oplossing is dat de projectconfiguratie
        bereikbaar is ongeacht hoe de dienst is aangezet -- vandaag hangt de knop
        'Configureer' aan de dienstenlijst op PROJECTniveau, dus een project dat de auth
        wall alleen op een component heeft staan, kan er helemaal niet bij. Dat is een
        eigen taak; dit is de wegwijzer tot die er is.

        Er hoort GEEN component-laag bij: ``config_editables(COMPONENT)`` blijft leeg en
        ``config_form_section(COMPONENT)`` blijft None, want er valt op dat niveau niets
        te bewaren. Om diezelfde reden haakt dit blok NIET via ``config_component_layout()``
        in: die haak telt mee in ``config_layers()``, en dan zou er een configroute
        ``/services/authorization-wall/config/component/{component}`` in de API bijkomen
        voor iets wat geen configuratie is. De plek in het formulier staat daarom in de
        Services-fieldset van COMPONENTS_SECTION, naast het vinkje waar je de dienst aanzet.
        """
        from opi.services.catalog.authorization_wall.visualizers import AUTH_WALL_COMPONENT_INFO

        return [AUTH_WALL_COMPONENT_INFO]

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return None
        # Built once and cached so consumers that compare section identity (e.g.
        # EDIT_SECTIONS[...] is AUTH_WALL_CONFIG_SECTION) keep seeing one object.
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.authorization_wall.visualizers import AUTH_WALL_BANNER

            cached = FormSection(
                section_id="auth-wall-config",
                title="Authorization wall configuratie",
                icon="sleutel",
                description="Instellingen voor de toegangspagina",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=[AUTH_WALL_BANNER],
                layout=[config_path(ConfigLayer.PROJECT, self.service_type, "config", "banner")],
            )
            self._config_section_cache = cached
        return cached

    def contribute_manifest_context(self, ctx: ManifestContext) -> ManifestContribution:
        # An auth wall sits in front of the pod: it adds the oauth2-proxy sidecar and
        # overrides service_port to 4180 (the proxy port). Needs the deployment's
        # already-provisioned keycloak secret for the proxy's issuer/client config.
        keycloak_secret = ctx.get_secret(ctx.deployment_name, "keycloak", KeycloakSecret)
        if keycloak_secret is None:
            # Component asked for an auth wall but no keycloak service is configured;
            # contribute nothing (matches the old warn-and-skip branch -- the manager
            # still logs the warning).
            return ManifestContribution()

        banner_text = None
        for service_item in ctx.project_data.get("services", []):
            if service_entry_name(service_item) == ServiceType.AUTHORIZATION_WALL.value:
                auth_wall_config = service_entry_config(service_item)
                if isinstance(auth_wall_config, dict):
                    banner_text = auth_wall_config.get("banner")
                break

        cookie_secret_name = f"{ctx.unique_name}-oauth2-cookie"
        return ManifestContribution(
            template_vars={
                "authorization_wall": {
                    "issuer_url": keycloak_secret.discovery_url.replace("/.well-known/openid-configuration", ""),
                    "client_id": keycloak_secret.client_id,
                    "keycloak_secret_name": KeycloakSecret.get_secret_name(ctx.deployment_name),
                    "cookie_secret_name": cookie_secret_name,
                    "banner": banner_text,
                },
                # The ingress goes through the auth proxy on 4180; the app's own primary
                # port stays behind it (extra inbound ports keep their Service entries).
                "service_port": 4180,
            },
            sidecars=["authorization-wall"],
            # The oauth2-proxy needs a random cookie-signing secret; the shared writer
            # SOPS-encrypts it. Its <deployment>-<component>- prefix survives the prune.
            secret_files=[
                SecretFileSpec(
                    secret_name=cookie_secret_name,
                    secret_pairs={"cookie-secret": secrets.token_urlsafe(32)},
                )
            ],
        )
