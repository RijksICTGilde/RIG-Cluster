"""
Keycloak bootstrap setup logic.

This module contains the business logic for setting up Keycloak during application startup.
It orchestrates the proper sequence of operations using YAML configuration.

The setup creates two realms:
1. rig-platform realm (from bootstrap YAML) - the platform realm with SSO configuration
2. operations-manager realm (from project file) - OPI's own realm, like any other project
"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from opi.connectors.keycloak import create_keycloak_connector
from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import get_namespace
from opi.core.config import settings
from opi.generation.manifests import ManifestGenerator
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler
from opi.utils.naming import extract_domain_from_url
from opi.utils.passwords import generate_secure_password

logger = logging.getLogger(__name__)

OPERATIONS_REALM_NAME = "operations-manager"


class KeycloakSetup:
    """Handles the complete Keycloak setup sequence for the operations manager."""

    def __init__(self):
        self.keycloak = None
        self.kubectl = None

    async def setup_all(self) -> bool:
        """
        Run the complete Keycloak setup sequence using YAML configuration.

        This creates:
        1. The rig-platform realm (from bootstrap YAML)
        2. The operations-manager realm (from project file, like any other project)
        3. A deployment client in the operations-manager realm

        Returns:
            True if all setup steps completed successfully
        """
        logger.info("Starting Keycloak setup")

        try:
            # Step 0: Ensure OPI's client-credentials service account exists. On a
            # fresh cluster this uses the admin password once to create it; on
            # later boots OPI runs purely on client-credentials.
            await self.ensure_master_admin_service_account()

            # Initialize connectors (uses client-credentials when configured)
            self.keycloak = await create_keycloak_connector()

            # Step 0b: a human master admin that already has OTP, so a fresh cluster is
            # not left with one shared password-only account.
            await self.ensure_otp_master_admin()
            self.kubectl = KubectlConnector()

            # Build context from settings
            local_admins = []
            if settings.KEYCLOAK_LOCAL_ADMIN_EMAIL:
                local_admins.append(
                    {
                        "username": settings.KEYCLOAK_LOCAL_ADMIN_USERNAME,
                        "email": settings.KEYCLOAK_LOCAL_ADMIN_EMAIL,
                        "password": await self._ensure_local_admin_password(),
                    }
                )

            context = {
                "realm_name": settings.KEYCLOAK_DEFAULT_REALM,
                "realm_display_name": settings.KEYCLOAK_DEFAULT_REALM_DISPLAY_NAME,
                "keycloak_url": settings.KEYCLOAK_URL,
                "sso_client_id": settings.KEYCLOAK_MASTER_OIDC_CLIENT_ID,
                "sso_client_secret": settings.KEYCLOAK_MASTER_OIDC_CLIENT_SECRET,
                "sso_discovery_url": settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL,
                # SAML SP Entity ID for direct SSO-Rijk connection
                "saml_sp_entity_id": f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_DEFAULT_REALM}",
                # Een lijst van nul of een, want de blueprint kent geen condities maar wel
                # forEach: een lege lijst maakt niets aan. Zo is dit standaard uit en zetten
                # alleen clusters die het expliciet configureren een noodaccount neer.
                "local_admins": local_admins,
            }

            # Select bootstrap configuration based on setting
            bootstrap_config = settings.KEYCLOAK_BOOTSTRAP_CONFIG
            if bootstrap_config == "local":
                yaml_filename = "bootstrap-local.yaml"
                logger.info("Using local bootstrap configuration (upstream IDP mode)")
            elif bootstrap_config == "sandbox":
                yaml_filename = "bootstrap-sandbox.yaml"
                logger.info("Using sandbox bootstrap configuration (upstream IDP mode)")
            else:
                yaml_filename = "bootstrap.yaml"
                logger.info("Using default bootstrap configuration (production mode)")

            # Step 1: Execute bootstrap (creates rig-platform realm)
            yaml_path = Path(__file__).parent.parent / "configs" / "keycloak" / yaml_filename
            handler = KeycloakYamlHandler(self.keycloak)
            await handler.execute_config(yaml_path, context)
            logger.info("Bootstrap realm setup completed")

            # Step 2: Create OPI's own realm (like any other project)
            await self.setup_operations_realm()

            # Step 3: Create deployment client in OPI realm and update K8s secret
            success = await self.setup_operations_client(realm_name=OPERATIONS_REALM_NAME)

            if success:
                logger.info("Keycloak setup completed successfully")
            else:
                logger.error("Failed to setup operations client")

            return success

        except Exception as e:
            logger.error(f"Keycloak setup failed with exception: {e}")
            return False

    async def ensure_otp_master_admin(self) -> None:
        """Create a second master admin that carries an OTP credential from the start.

        Keycloak creates the ``KEYCLOAK_ADMIN`` account itself at first boot from the
        environment, so there is no moment where we could give it an OTP credential; and
        Keycloak 25 imports an OTP credential only when a user is created, so retrofitting
        means delete-and-recreate. Doing that to the one account OPI is authenticated as,
        and that is the break-glass, is not worth it.

        Creating a *second* admin does work: at creation the credential is imported
        verbatim. A fresh cluster then has a human admin with a second factor from minute
        one, while the shared ``admin`` stays as break-glass. That is also the better end
        state -- named accounts rather than one shared login, so the audit log says who did
        what.

        Idempotent and opt-in: without a seed in the secret nothing happens, and an
        existing user is left alone (recreating it would rotate the operator's OTP).
        """
        username = settings.KEYCLOAK_OTP_ADMIN_USERNAME
        seed = settings.KEYCLOAK_OTP_ADMIN_TOTP_SECRET
        password = settings.KEYCLOAK_OTP_ADMIN_PASSWORD

        if not (username and seed and password):
            logger.debug("No OTP master admin configured; skipping")
            return

        keycloak = self.keycloak or await create_keycloak_connector()
        if await keycloak.get_user_by_username("master", username):
            logger.debug(f"OTP master admin '{username}' already exists")
            return

        logger.info(f"Creating master admin '{username}' with an OTP credential")
        user = await keycloak.create_user(
            realm_name="master",
            username=username,
            password=password,
            email=f"{username}@localhost",
            first_name="Platform",
            last_name="Administrator",
            enabled=True,
            totp_secret=seed,
        )
        await keycloak.assign_realm_roles_to_user("master", user["id"], ["admin"])
        logger.info(f"Master admin '{username}' created with OTP; seed is in the cluster secret")

    async def ensure_master_admin_service_account(self) -> None:
        """First-boot self-bootstrap of OPI's client-credentials service account.

        - No client secret configured: stay on admin-password auth (legacy).
        - Secret set and client-credentials already work: nothing to do.
        - Otherwise: use the admin password once to create/repair the master
          confidential client so subsequent boots run on client-credentials.
        """
        if not settings.KEYCLOAK_ADMIN_CLIENT_SECRET:
            logger.info("KEYCLOAK_ADMIN_CLIENT_SECRET not set; using admin password authentication")
            return

        client_cred = await create_keycloak_connector(use_client_credentials=True)
        if await client_cred.connection_works():
            logger.info("OPI Keycloak service account already functional; using client-credentials")
            return

        logger.info("Bootstrapping OPI Keycloak service account using admin password")
        admin = await create_keycloak_connector(use_client_credentials=False)
        await admin.ensure_master_service_account_client(
            client_id=settings.KEYCLOAK_ADMIN_CLIENT_ID,
            client_secret=settings.KEYCLOAK_ADMIN_CLIENT_SECRET,
        )

    async def setup_operations_realm(self) -> None:
        """Create OPI's own realm using project file config (like any other project).

        Selects the appropriate project file based on KEYCLOAK_BOOTSTRAP_CONFIG:
        - "local" or "sandbox": uses operations-manager-local.yaml (sso-support + admin user)
        - "default" (production): uses operations-manager.yaml (sso-only)
        """
        # Select project file based on bootstrap config
        if settings.KEYCLOAK_BOOTSTRAP_CONFIG in ("local", "sandbox"):
            project_filename = "operations-manager-local.yaml"
        else:
            project_filename = "operations-manager.yaml"

        project_file = Path(__file__).parent.parent / "configs" / "projects" / project_filename
        logger.info(f"Setting up OPI realm from project file: {project_filename}")

        # Read bundled project file
        yaml_parser = YAML()
        with project_file.open() as f:
            project_data = yaml_parser.load(f)

        # Extract keycloak config from services section
        keycloak_config = self._extract_keycloak_service_config(project_data)
        template_name = keycloak_config["template"]

        # Build context for the template
        operations_manager_domain = extract_domain_from_url(settings.OWN_DOMAIN)

        context = {
            "project_name": "rig-platform",
            "cluster": settings.CLUSTER_MANAGER,
            "keycloak_url": settings.KEYCLOAK_URL,
            "platform_realm_name": settings.KEYCLOAK_DEFAULT_REALM,
            "project_realm_name": OPERATIONS_REALM_NAME,
            "project_display_name": "Operations Manager",
            "platform_client_id": f"operations-manager-{settings.CLUSTER_MANAGER}-platform",
            "realm_name": OPERATIONS_REALM_NAME,
            "realm_display_name": "Operations Manager",
            "operations_manager_domain": operations_manager_domain,
            "invite_client_id": settings.INVITE_CLIENT_ID,
            "cli_client_id": settings.CLI_CLIENT_ID,
            "cli_token_audience": settings.CLI_TOKEN_AUDIENCE,
        }
        context.update(keycloak_config.get("variables", {}))

        # Execute YAML template (creates realm, IDP, client scopes, invite client)
        yaml_path = Path(__file__).parent.parent / "configs" / "keycloak" / f"{template_name}.yaml"
        keycloak = self.keycloak or await create_keycloak_connector()
        handler = KeycloakYamlHandler(keycloak)
        await handler.execute_config(yaml_path, context)
        logger.info(f"Created OPI realm '{OPERATIONS_REALM_NAME}' using template '{template_name}'")

        # Create default users from project file (if defined)
        users = keycloak_config.get("users", [])
        if users:
            variables = {**context, "project_realm_name": OPERATIONS_REALM_NAME}
            await handler._process_users(users, variables)
            logger.info(f"Created {len(users)} default user(s) in realm '{OPERATIONS_REALM_NAME}'")

    def _extract_keycloak_service_config(self, project_data: dict) -> dict:
        """Extract keycloak config from project file services section.

        Simplified version of keycloak_manager._get_keycloak_service_config()
        for the OPI's own project files.

        Args:
            project_data: The project file data

        Returns:
            Dictionary with template, variables, and users config
        """
        from opi.services.services import service_entry_config, service_entry_name
        from opi.services.services_enums import ServiceType

        default: dict[str, Any] = {"template": "sso-support", "variables": {}, "users": []}
        services = project_data.get("services", [])
        for service in services:
            # Format-agnostic: the keycloak entry may be a record ({name, config}), a
            # legacy single-key dict, or a bare string. Matching on ``"keycloak" in dict``
            # only saw the legacy form and silently missed the uniform record.
            if service_entry_name(service) != ServiceType.KEYCLOAK.value:
                continue
            config = service_entry_config(service) or {}
            return {
                "template": config.get("template", "sso-support"),
                "variables": config.get("variables", {}),
                "restrict_access": config.get("restrict_access"),
                "users": config.get("users", []),
            }
        return default

    async def setup_operations_client(self, realm_name: str | None = None) -> bool:
        """
        Setup the operations manager's deployment client.

        Creates the client for the operations manager GUI and updates the
        Kubernetes secret with the credentials.

        Args:
            realm_name: The realm to create the client in. Defaults to KEYCLOAK_DEFAULT_REALM.

        Returns:
            True if operations client setup was successful
        """
        target_realm = realm_name or settings.KEYCLOAK_DEFAULT_REALM
        logger.info(f"Setting up operations manager client in realm '{target_realm}'")

        try:
            deployment_name = "operations-manager"
            project_name = "rig-platform"
            expected_client_id = f"{project_name}-{deployment_name}"
            ingress_hosts = [settings.OWN_DOMAIN]

            # Add additional domains if configured
            if settings.ADDITIONAL_DOMAINS:
                additional = [d.strip() for d in settings.ADDITIONAL_DOMAINS.split(",") if d.strip()]
                ingress_hosts.extend(additional)
                logger.info(f"Added additional domains: {additional}")

            logger.info(f"Creating/updating client '{expected_client_id}' with domains: {ingress_hosts}")

            # Create or get the client
            client_info = await self.keycloak.create_deployment_client(
                deployment_name=deployment_name,
                project_name=project_name,
                ingress_hosts=ingress_hosts,
                realm_name=target_realm,
            )

            logger.info(f"Successfully created/retrieved client: {expected_client_id}")

            # Update the operations-manager-keycloak secret
            success = await self._update_operations_secret(client_info)

            if success:
                logger.info("Operations manager client setup completed successfully")
                return True
            else:
                logger.error("Failed to update operations manager secret")
                return False

        except Exception as e:
            logger.error(f"Failed to setup operations client: {e}")
            return False

    async def _ensure_local_admin_password(self) -> str:
        """Lees het wachtwoord van het lokale noodaccount, of maak het bij de eerste run aan.

        Bewust idempotent: bestaat het Secret al, dan wordt het gelezen en niet geroteerd.
        Roteren bij elke start zou het wachtwoord dat een beheerder heeft weggeschreven stil
        ongeldig maken, en juist dit account bestaat voor het moment dat SSO eruit ligt.

        Het Secret is de enige plek waar dit wachtwoord staat. Het hoort niet in git en niet
        in een ConfigMap: het is het enige pad dat SSO omzeilt.
        """
        namespace = get_namespace(settings.CLUSTER_MANAGER)
        secret_name = settings.KEYCLOAK_LOCAL_ADMIN_SECRET_NAME

        existing = await self.kubectl.get_secret(secret_name, namespace)
        if existing and existing.get("PASSWORD"):
            logger.info(f"Lokaal noodaccount: wachtwoord gelezen uit bestaand secret {secret_name}")
            return existing["PASSWORD"]

        password = generate_secure_password(total_length=32)
        logger.info(f"Lokaal noodaccount: nieuw wachtwoord gegenereerd en opgeslagen in secret {secret_name}")

        with tempfile.TemporaryDirectory(dir=settings.TEMP_DIR) as temp_dir:
            manifest_generator = ManifestGenerator()
            template_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "manifests", "generic-secret.yaml.to-sops.jinja"
            )
            secret_values = {
                "name": secret_name,
                "namespace": namespace,
                "secret_type": "local-admin-credentials",
                "secret_pairs": {
                    "USERNAME": settings.KEYCLOAK_LOCAL_ADMIN_USERNAME,
                    "EMAIL": settings.KEYCLOAK_LOCAL_ADMIN_EMAIL,
                    "PASSWORD": password,
                },
                "secret_annotations": {
                    "operations-manager.rig/managed": "true",
                    "operations-manager.rig/purpose": "Lokaal noodaccount voor als de SSO-koppeling eruit ligt",
                },
            }
            manifest_file_path = manifest_generator.create_manifest_file(
                template_path=template_path,
                values=secret_values,
                output_dir=temp_dir,
                output_filename=f"{secret_name}-secret.yaml",
                use_sops=False,
            )
            await self.kubectl.apply_manifest(manifest_file_path)

        return password

    async def _update_operations_secret(self, client_info: dict[str, Any]) -> bool:
        """Update the operations-manager-keycloak secret with client credentials."""
        try:
            logger.info("Updating operations-manager-keycloak secret with OIDC credentials")

            # Create a temporary directory for the manifest
            from opi.core.config import settings

            with tempfile.TemporaryDirectory(dir=settings.TEMP_DIR) as temp_dir:
                manifest_generator = ManifestGenerator()

                # Get the template path for generic secret
                template_path = os.path.join(
                    os.path.dirname(__file__), "..", "..", "manifests", "generic-secret.yaml.to-sops.jinja"
                )

                # Prepare the values for the secret
                secret_values = {
                    "name": "operations-manager-keycloak",
                    "namespace": get_namespace(settings.CLUSTER_MANAGER),
                    "secret_type": "oidc-credentials",
                    "secret_pairs": {
                        "OIDC_CLIENT_ID": client_info["client_id"],
                        "OIDC_CLIENT_SECRET": client_info["client_secret"],
                        "OIDC_DISCOVERY_URL": client_info["discovery_url"],
                        "OIDC_URL": client_info["base_url"],
                        "OIDC_REALM": client_info["realm"],
                    },
                    "secret_annotations": {
                        "operations-manager.rig/managed": "true",
                        "operations-manager.rig/purpose": "OIDC client credentials for operations-manager authentication",
                    },
                }

                # Create the manifest file (without SOPS encryption since we're applying directly)
                manifest_file_path = manifest_generator.create_manifest_file(
                    template_path=template_path,
                    values=secret_values,
                    output_dir=temp_dir,
                    output_filename="operations-manager-keycloak-secret.yaml",
                    use_sops=False,
                )

                # Apply the secret using kubectl. On failure this raises, which the
                # surrounding try/except logs and turns into a False return.
                await self.kubectl.apply_manifest(manifest_file_path)
                logger.info("Successfully updated operations-manager-keycloak secret")

                # Update the current settings if they're not already set
                if not settings.OIDC_CLIENT_ID:
                    settings.OIDC_CLIENT_ID = client_info["client_id"]
                    logger.info(f"Updated settings.OIDC_CLIENT_ID: {client_info['client_id']}")

                if not settings.OIDC_CLIENT_SECRET:
                    settings.OIDC_CLIENT_SECRET = client_info["client_secret"]
                    logger.info("Updated settings.OIDC_CLIENT_SECRET")

                if not settings.OIDC_DISCOVERY_URL:
                    settings.OIDC_DISCOVERY_URL = client_info["discovery_url"]
                    logger.info(f"Updated settings.OIDC_DISCOVERY_URL: {client_info['discovery_url']}")

                return True

        except Exception as e:
            logger.error(f"Error updating operations secret: {e}")
            return False


def extract_realm_from_discovery_url(discovery_url: str | None) -> str | None:
    """Extract realm name from OIDC discovery URL.

    Args:
        discovery_url: The OIDC discovery URL (e.g., https://keycloak.example.com/realms/my-realm/.well-known/...)

    Returns:
        The realm name, or None if not extractable
    """
    if not discovery_url:
        return None
    match = re.search(r"/realms/([^/]+)/", discovery_url)
    return match.group(1) if match else None


# Convenience function for startup
async def setup_keycloak() -> bool:
    """
    Run the complete Keycloak setup sequence.

    This is the main entry point called from startup.py.

    Returns:
        True if all Keycloak setup completed successfully
    """
    setup = KeycloakSetup()
    return await setup.setup_all()
