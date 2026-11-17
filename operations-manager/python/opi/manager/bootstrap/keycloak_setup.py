"""Setting up Keycloak during application startup, from YAML configuration.

The setup creates two realms: rig-platform (from the bootstrap YAML), the platform realm
with the SSO configuration, and operations-manager (from a project file), OPI's own realm,
like any other project.
"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from keycloak.exceptions import KeycloakError

from opi.connectors.keycloak import REQUIRED_OTP_BROWSER_FLOW, create_keycloak_connector
from opi.connectors.kubectl import KubectlConnectionError, KubectlConnector, KubectlExecutionError
from opi.core.cluster_config import get_namespace
from opi.core.config import settings
from opi.generation.manifests import ManifestGenerator
from opi.manager.bootstrap.keycloak_yaml_handler import KeycloakYamlHandler
from opi.utils.naming import extract_domain_from_url
from opi.utils.yaml_util import load_yaml_safe

logger = logging.getLogger(__name__)

OPERATIONS_REALM_NAME = "operations-manager"

#: Keycloak expresses eventsExpiration in seconds, the setting in days.
SECONDS_PER_DAY = 86400


class KeycloakSetup:
    """Handles the complete Keycloak setup sequence for the operations manager."""

    def __init__(self) -> None:
        self.keycloak = None
        self.kubectl = None

    async def setup_all(self) -> bool:
        """Create the rig-platform realm, OPI's own realm, and a deployment client in it."""
        logger.info("Starting Keycloak setup")

        try:
            # On a fresh cluster this uses the admin password once to create the service
            # account; on later boots OPI runs purely on client-credentials.
            await self.ensure_master_admin_service_account()

            self.keycloak = await create_keycloak_connector()

            # A human master admin that already has OTP, so a fresh cluster is not left
            # with one shared password-only account.
            await self.ensure_otp_master_admin()

            # Keycloak creates the master realm itself, so it never passes create_realm and
            # gets nothing from a blueprint.
            await self.ensure_master_realm_hardening()

            await self.ensure_master_otp()
            self.kubectl = KubectlConnector()

            context = {
                "realm_name": settings.KEYCLOAK_DEFAULT_REALM,
                "realm_display_name": settings.KEYCLOAK_DEFAULT_REALM_DISPLAY_NAME,
                "keycloak_url": settings.KEYCLOAK_URL,
                "sso_client_id": settings.KEYCLOAK_MASTER_OIDC_CLIENT_ID,
                "sso_client_secret": settings.KEYCLOAK_MASTER_OIDC_CLIENT_SECRET,
                "sso_discovery_url": settings.KEYCLOAK_MASTER_OIDC_DISCOVERY_URL,
                # SAML SP Entity ID for direct SSO-Rijk connection
                "saml_sp_entity_id": f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_DEFAULT_REALM}",
                # Where the break-glass login of ZAD comes back to (the zad-beheer client).
                "operations_manager_domain": extract_domain_from_url(settings.OWN_DOMAIN),
            }

            bootstrap_config = settings.KEYCLOAK_BOOTSTRAP_CONFIG
            if bootstrap_config == "local":
                yaml_filename = "bootstrap-local.yaml"
                logger.info("Using local bootstrap configuration (upstream IDP mode)")
            elif bootstrap_config in ("sandbox", "staging"):
                yaml_filename = "bootstrap-sandbox.yaml"
                logger.info(f"Using {bootstrap_config} bootstrap configuration (upstream IDP mode)")
            else:
                yaml_filename = "bootstrap.yaml"
                logger.info("Using default bootstrap configuration (production mode)")

            yaml_path = Path(__file__).parent.parent.parent / "configs" / "keycloak" / yaml_filename
            handler = KeycloakYamlHandler(self.keycloak)
            await handler.execute_config(yaml_path, context)
            logger.info("Bootstrap realm setup completed")

            await self.setup_operations_realm()

            success = await self.setup_operations_client(realm_name=OPERATIONS_REALM_NAME)

            if success:
                logger.info("Keycloak setup completed successfully")
            else:
                logger.error("Failed to setup operations client")

            return success

        except (KeycloakError, OSError, ValueError, KeyError) as e:
            logger.error(f"Keycloak setup failed with exception: {e}")
            return False

    async def ensure_otp_master_admin(self) -> None:
        """Create a second master admin that carries an OTP credential from the start.

        Keycloak 25 imports an OTP credential only when a user is created, and Keycloak
        creates ``KEYCLOAK_ADMIN`` itself at first boot, so that account can never get one
        without delete-and-recreate. This second admin gets its credential at creation.

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
            # @localhost is not a mailbox, so requiring verification would lock the
            # break-glass admin out.
            skip_email_verification=True,
        )
        await keycloak.assign_realm_roles_to_user("master", user["id"], ["admin"])
        logger.info(f"Master admin '{username}' created with OTP; seed is in the cluster secret")

    async def ensure_master_realm_hardening(self) -> None:
        """Set lockout, password policy and events on the master realm.

        Keycloak creates the master realm itself, so it does not get this protection via
        create_realm (features/opi-keycloak-service-account.md). A failure here does not
        stop the bootstrap.
        """
        keycloak = self.keycloak or await create_keycloak_connector()
        yaml_path = Path(__file__).parent.parent.parent / "configs" / "keycloak" / "master-realm.yaml"
        context = {
            "realm_name": "master",
            "events_expiration_seconds": settings.KEYCLOAK_MASTER_EVENTS_EXPIRATION_DAYS * SECONDS_PER_DAY,
        }
        try:
            await KeycloakYamlHandler(keycloak).ensure_realm_hardening(yaml_path, context)
        except KeycloakError as e:
            logger.error(f"Kon de beveiligingsstand van de master-realm niet zetten: {e}")

    async def ensure_master_otp(self) -> None:
        """Require OTP for every password login in the master realm. Fails gracefully,
        controlled by ``KEYCLOAK_ENFORCE_MASTER_OTP``.
        """
        if not settings.KEYCLOAK_ENFORCE_MASTER_OTP:
            # The flag only skips this step; a flow bound on an earlier start stays bound, and
            # unbinding it is a manual action in Keycloak.
            logger.info("KEYCLOAK_ENFORCE_MASTER_OTP is off; skipping required OTP on master realm")
            return

        keycloak = self.keycloak or await create_keycloak_connector()
        try:
            await keycloak.ensure_required_otp_browser_flow("master", REQUIRED_OTP_BROWSER_FLOW)
            await keycloak.ensure_browser_flow("master", REQUIRED_OTP_BROWSER_FLOW)
            # Skip KEYCLOAK_ADMIN_USERNAME: an open required action blocks the password grant that
            # OPI falls back to when KEYCLOAK_ADMIN_CLIENT_SECRET is absent.
            users_to_configure = await keycloak.ensure_configure_totp_required(
                "master", skip_usernames=frozenset({settings.KEYCLOAK_ADMIN_USERNAME})
            )
        except KeycloakError as e:
            logger.error(f"Could not set required OTP on master realm: {e}")
            return

        logger.info(f"Required OTP on master realm set; {len(users_to_configure)} user(s) will configure on next login")

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
        - "default" (production) and "staging": uses operations-manager.yaml (sso-only).
          Staging is a pentest environment, so it gets no local admin with a known password.
        """
        if settings.KEYCLOAK_BOOTSTRAP_CONFIG in ("local", "sandbox"):
            project_filename = "operations-manager-local.yaml"
        else:
            project_filename = "operations-manager.yaml"

        project_file = Path(__file__).parent.parent.parent / "configs" / "projects" / project_filename
        logger.info(f"Setting up OPI realm from project file: {project_filename}")

        project_data = load_yaml_safe(project_file.read_text(encoding="utf-8"))

        keycloak_config = self._extract_keycloak_service_config(project_data)
        template_name = keycloak_config["template"]

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

        yaml_path = Path(__file__).parent.parent.parent / "configs" / "keycloak" / f"{template_name}.yaml"
        keycloak = self.keycloak or await create_keycloak_connector()
        handler = KeycloakYamlHandler(keycloak)
        await handler.execute_config(yaml_path, context)
        logger.info(f"Created OPI realm '{OPERATIONS_REALM_NAME}' using template '{template_name}'")

        for item in keycloak_config.get("users", []):
            username = item["username"]
            password = next(
                (cred.get("value") for cred in item.get("credentials", []) if cred.get("type") == "password"), None
            )
            if not password:
                msg = f"User '{username}' in realm '{OPERATIONS_REALM_NAME}' has no password credential"
                raise ValueError(msg)

            await keycloak.create_user(
                # Literal realm name, which is what the guard requires.
                realm_name="operations-manager",
                username=username,
                password=password,
                email=item.get("email"),
                first_name=item.get("firstName"),
                last_name=item.get("lastName"),
                enabled=item.get("enabled", True),
                # The address is not a mailbox, so verification would hold the user back.
                skip_email_verification=True,
            )
            logger.info(f"Created user '{username}' in realm '{OPERATIONS_REALM_NAME}'")

    def _extract_keycloak_service_config(self, project_data: dict) -> dict:
        """The template, variables and users from the keycloak entry in ``services``."""
        from opi.services.services import service_entry_config, service_entry_name
        from opi.services.services_enums import ServiceType

        default: dict[str, Any] = {"template": "sso-support", "variables": {}, "users": []}
        services = project_data.get("services", [])
        for service in services:
            # The keycloak entry may be a record ({name, config}), a legacy single-key dict,
            # or a bare string, so the name is read rather than matched on a dict key.
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
        """Create the client for the OPI GUI and put its credentials in the Kubernetes secret.

        Args:
            realm_name: Defaults to KEYCLOAK_DEFAULT_REALM.
        """
        target_realm = realm_name or settings.KEYCLOAK_DEFAULT_REALM
        logger.info(f"Setting up operations manager client in realm '{target_realm}'")

        try:
            deployment_name = "operations-manager"
            project_name = "rig-platform"
            expected_client_id = f"{project_name}-{deployment_name}"
            ingress_hosts = [settings.OWN_DOMAIN]

            if settings.ADDITIONAL_DOMAINS:
                additional = [d.strip() for d in settings.ADDITIONAL_DOMAINS.split(",") if d.strip()]
                ingress_hosts.extend(additional)
                logger.info(f"Added additional domains: {additional}")

            logger.info(f"Creating/updating client '{expected_client_id}' with domains: {ingress_hosts}")

            client_info = await self.keycloak.create_deployment_client(
                deployment_name=deployment_name,
                project_name=project_name,
                ingress_hosts=ingress_hosts,
                realm_name=target_realm,
                # The OPI login sends PKCE along (opi/runtime/startup/startup.py), so Keycloak
                # may refuse an authorization code without a challenge.
                require_pkce=True,
            )

            logger.info(f"Successfully created/retrieved client: {expected_client_id}")

            success = await self._update_operations_secret(client_info)

            if success:
                logger.info("Operations manager client setup completed successfully")
                return True
            else:
                logger.error("Failed to update operations manager secret")
                return False

        except (KeycloakError, OSError, ValueError, KeyError) as e:
            logger.error(f"Failed to setup operations client: {e}")
            return False

    async def _update_operations_secret(self, client_info: dict[str, Any]) -> bool:
        """Update the operations-manager-keycloak secret with client credentials."""
        try:
            logger.info("Updating operations-manager-keycloak secret with OIDC credentials")

            from opi.core.config import settings

            with tempfile.TemporaryDirectory(dir=settings.TEMP_DIR) as temp_dir:
                manifest_generator = ManifestGenerator()

                template_path = os.path.join(
                    os.path.dirname(__file__), "..", "..", "..", "manifests", "generic-secret.yaml.to-sops.jinja"
                )

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

                # No SOPS encryption: the manifest is applied straight away.
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

                # Also when already set: on a first start the values come from the ConfigMap, whose
                # discovery URL names another realm. This secret only reaches the env on a restart.
                settings.OIDC_CLIENT_ID = client_info["client_id"]
                settings.OIDC_CLIENT_SECRET = client_info["client_secret"]
                settings.OIDC_DISCOVERY_URL = client_info["discovery_url"]
                logger.info(f"Updated OIDC settings for client '{client_info['client_id']}'")

                return True

        except (
            KeycloakError,
            OSError,
            ValueError,
            KeyError,
            RuntimeError,
            KubectlConnectionError,
            KubectlExecutionError,
        ) as e:
            logger.error(f"Error updating operations secret: {e}")
            return False


def extract_realm_from_discovery_url(discovery_url: str | None) -> str | None:
    """The realm in an OIDC discovery URL (.../realms/<realm>/.well-known/...), or None."""
    if not discovery_url:
        return None
    match = re.search(r"/realms/([^/]+)/", discovery_url)
    return match.group(1) if match else None


async def setup_keycloak() -> bool:
    """The entry point startup.py calls."""
    setup = KeycloakSetup()
    return await setup.setup_all()
