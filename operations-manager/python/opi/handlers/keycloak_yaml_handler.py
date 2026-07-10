"""Handler for executing Keycloak configuration from YAML files.

This handler processes declarative YAML configurations and executes them
via the Keycloak connector. Supports variable substitution and forEach loops.
"""

import logging
import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from opi.connectors.keycloak import AUTO_LINK_FIRST_BROKER_LOGIN_FLOW, KeycloakConnector, RealmType

logger = logging.getLogger(__name__)


class KeycloakYamlHandler:
    """Handler for processing Keycloak YAML configurations."""

    def __init__(self, keycloak_connector: KeycloakConnector) -> None:
        """Initialize handler with Keycloak connector.

        Args:
            keycloak_connector: Configured Keycloak connector instance
        """
        self.keycloak = keycloak_connector
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.outputs: dict[str, Any] = {}  # Store outputs from operations with 'as' key

    async def execute_config(self, yaml_path: str | Path, context: dict[str, Any]) -> None:
        """Execute YAML configuration with provided context.

        Args:
            yaml_path: Path to YAML configuration file
            context: Dictionary of variables for substitution
        """
        logger.info(f"Executing Keycloak configuration from {yaml_path}")

        # Load YAML
        config = self._load_yaml(yaml_path)

        # Merge variables: YAML variables + context (context overrides)
        variables = {**config.get("variables", {}), **context}

        logger.debug(f"Context variables: {list(variables.keys())}")

        # Process sections in dependency order
        # Platform clients must be processed before identity providers since
        # identity providers may reference platform_client outputs (client_id, client_secret)
        await self._process_realms(config.get("realms"), variables)
        await self._process_platform_clients(config.get("platformClients"), variables)
        await self._process_identity_providers(config.get("identityProviders"), variables)
        await self._process_authentication_flows(config.get("authenticationFlows"), variables)
        await self._process_browser_flow(config.get("browserFlow"), variables)
        await self._process_client_scopes(config.get("clientScopes"), variables)
        await self._process_clients(config.get("clients"), variables)
        await self._process_realm_roles(config.get("realmRoles"), variables)
        await self._process_groups(config.get("groups"), variables)
        await self._process_users(config.get("users"), variables)

        logger.info("Keycloak configuration execution completed")

    async def ensure_authentication_flows(self, yaml_path: str | Path, context: dict[str, Any]) -> None:
        """Ensure authentication flows are correctly configured (idempotent).

        This method only processes the authenticationFlows section of the YAML config.
        It's used to update existing realms where the full execute_config is not needed.

        Args:
            yaml_path: Path to YAML configuration file
            context: Dictionary of variables for substitution (must include realm_name)
        """
        logger.info(f"Ensuring authentication flows from {yaml_path}")

        # Load YAML
        config = self._load_yaml(yaml_path)

        # Merge variables: YAML variables + context (context overrides)
        variables = {**config.get("variables", {}), **context}

        # Process authentication flows and browser flow setting
        flows_section = config.get("authenticationFlows")
        if flows_section:
            await self._process_authentication_flows(flows_section, variables)
            logger.info("Authentication flows configuration completed")
        else:
            logger.debug("No authenticationFlows section in configuration")

        await self._process_browser_flow(config.get("browserFlow"), variables)

    async def ensure_identity_providers(self, yaml_path: str | Path, context: dict[str, Any]) -> None:
        """Ensure identity providers are correctly configured (idempotent).

        Processes platformClients first so the IDP config block can reference
        captured client_id/client_secret outputs, then processes identityProviders.

        Args:
            yaml_path: Path to YAML configuration file
            context: Dictionary of variables for substitution (must include realm_name)
        """
        logger.info(f"Ensuring identity providers from {yaml_path}")

        config = self._load_yaml(yaml_path)
        variables = {**config.get("variables", {}), **context}

        platform_clients_section = config.get("platformClients")
        if platform_clients_section:
            await self._process_platform_clients(platform_clients_section, variables)

        idp_section = config.get("identityProviders")
        if idp_section:
            await self._process_identity_providers(idp_section, variables)
            logger.info("Identity providers configuration completed")
        else:
            logger.debug("No identityProviders section in configuration")

    async def ensure_clients(self, yaml_path: str | Path, context: dict[str, Any]) -> None:
        """Ensure clients are correctly configured (idempotent).

        This method only processes the clients section of the YAML config.
        It's used to update existing realms where clients may have been added
        to the template after initial realm creation.

        Args:
            yaml_path: Path to YAML configuration file
            context: Dictionary of variables for substitution (must include realm_name)
        """
        logger.info(f"Ensuring clients from {yaml_path}")

        # Load YAML
        config = self._load_yaml(yaml_path)

        # Merge variables: YAML variables + context (context overrides)
        variables = {**config.get("variables", {}), **context}

        # Process only clients
        clients_section = config.get("clients")
        if clients_section:
            await self._process_clients(clients_section, variables)
            logger.info("Clients configuration completed")
        else:
            logger.debug("No clients section in configuration")

    def _load_yaml(self, yaml_path: str | Path) -> dict[str, Any]:
        """Load YAML file.

        Args:
            yaml_path: Path to YAML file

        Returns:
            Parsed YAML as dictionary
        """
        path = Path(yaml_path)
        if not path.exists():
            msg = f"YAML configuration file not found: {yaml_path}"
            raise FileNotFoundError(msg)

        with path.open("r") as f:
            config = self.yaml.load(f)

        if not isinstance(config, dict):
            msg = f"Invalid YAML configuration: expected dict, got {type(config)}"
            raise TypeError(msg)

        return config

    def _substitute_variables(self, value: Any, variables: dict[str, Any]) -> Any:
        """Recursively substitute {{ variable }} placeholders.

        Args:
            value: Value to process (can be str, dict, list, or other)
            variables: Dictionary of variable values

        Returns:
            Value with substitutions applied
        """
        if isinstance(value, str):
            # Check if entire string is a variable reference
            match = re.fullmatch(r"\{\{\s*([^}]+)\s*\}\}", value)
            if match:
                var_path = match.group(1).strip()
                return self._get_nested_value(variables, var_path)

            # Replace inline variables
            def replace_var(match_obj: re.Match) -> str:
                var_path = match_obj.group(1).strip()
                result = self._get_nested_value(variables, var_path)
                return str(result) if result is not None else ""

            return re.sub(r"\{\{\s*([^}]+)\s*\}\}", replace_var, value)

        if isinstance(value, dict):
            return {k: self._substitute_variables(v, variables) for k, v in value.items()}

        if isinstance(value, list):
            return [self._substitute_variables(item, variables) for item in value]

        return value

    def _get_nested_value(self, data: dict[str, Any], path: str) -> Any:
        """Get value from nested dict using dot notation.

        Checks outputs first (for captured operation results), then context data.

        Args:
            data: Dictionary to search (context)
            path: Dot-separated path (e.g., "user.name" or "platform_client.client_secret")

        Returns:
            Value at path

        Raises:
            KeyError: If variable path is not found in context or outputs
        """
        keys = path.split(".")

        # Check if first key is in outputs (captured operation results)
        if keys[0] in self.outputs:
            value = self.outputs
            for key in keys:
                if isinstance(value, dict) and key in value:
                    value = value[key]
                else:
                    available_keys = list(value.keys()) if isinstance(value, dict) else []
                    raise KeyError(
                        f"Output path not found: '{path}'. Available keys in outputs: {available_keys}. "
                        f"Check that the operation producing '{keys[0]}' has completed and uses 'as: {keys[0]}' in the configuration."
                    )
            return value

        # Check context data
        value = data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                available_keys = list(data.keys()) if isinstance(data, dict) else []
                raise KeyError(
                    f"Variable path not found: '{path}'. Available variables: {available_keys}. "
                    f"Ensure the variable is defined in the 'variables' section of the Keycloak configuration."
                )
        return value

    def _expand_list(self, section: Any, variables: dict[str, Any]) -> list[dict[str, Any]]:
        """Expand list section, handling forEach loops.

        Args:
            section: YAML section (can be list or dict with forEach)
            variables: Context variables

        Returns:
            List of expanded items with variables substituted
        """
        if section is None:
            return []

        # Case 1: Section is entire forEach loop (dict with forEach key)
        if isinstance(section, dict) and "forEach" in section:
            return self._expand_foreach(section, variables)

        # Case 2: Section is a list
        if isinstance(section, list):
            expanded = []
            for item in section:
                # Check if this item is a forEach loop
                if isinstance(item, dict) and "forEach" in item:
                    expanded.extend(self._expand_foreach(item, variables))
                else:
                    # Regular item - substitute variables
                    expanded.append(self._substitute_variables(item, variables))
            return expanded

        # Case 3: Single dict item (not forEach)
        if isinstance(section, dict):
            return [self._substitute_variables(section, variables)]

        return []

    def _expand_foreach(self, loop_def: dict[str, Any], variables: dict[str, Any]) -> list[dict[str, Any]]:
        """Expand a forEach loop definition.

        Args:
            loop_def: Loop definition with forEach, as, and singular key
            variables: Context variables

        Returns:
            List of expanded items
        """
        # Get loop variable name
        loop_var = loop_def.get("as", "item")

        # Get collection to iterate over
        for_each_expr = loop_def.get("forEach", "")
        collection = self._substitute_variables(for_each_expr, variables)

        if not isinstance(collection, list):
            logger.warning(f"forEach expression did not resolve to a list: {for_each_expr}")
            return []

        # Find the singular key (e.g., "user" in users section)
        template_key = None
        for key in loop_def:
            if key not in ("forEach", "as"):
                template_key = key
                break

        if template_key is None:
            logger.warning("No template found in forEach definition")
            return []

        template = loop_def[template_key]

        # Expand loop
        expanded = []
        for item in collection:
            # Create loop context
            loop_context = {**variables, loop_var: item}
            # Substitute variables in template
            expanded_item = self._substitute_variables(template, loop_context)
            expanded.append(expanded_item)

        return expanded

    async def _process_realms(self, realms_section: Any, variables: dict[str, Any]) -> None:
        """Process realms section.

        Args:
            realms_section: Realms YAML section
            variables: Context variables
        """
        if not realms_section:
            return

        items = self._expand_list(realms_section, variables)
        for item in items:
            realm_name = item.get("name")
            if not realm_name:
                logger.warning("Realm missing 'name' field, skipping")
                continue

            logger.info(f"Creating realm: {realm_name}")
            await self.keycloak.create_realm(
                realm_name=realm_name,
                display_name=item.get("displayName", realm_name),
                add_master_idp=False,
                sso_session_idle_timeout=item.get("ssoSessionIdleTimeout"),
                sso_session_max_lifespan=item.get("ssoSessionMaxLifespan"),
            )

    async def _process_identity_providers(self, idp_section: Any, variables: dict[str, Any]) -> None:
        """Process identityProviders section.

        Supports both OIDC and SAML identity providers based on the providerId field.

        Args:
            idp_section: Identity providers YAML section
            variables: Context variables
        """
        if not idp_section:
            return

        # Get realm name from context
        realm_name = variables.get("realm_name") or variables.get("project_realm_name")
        if not realm_name:
            logger.warning("No realm_name in context for identity providers, skipping")
            return

        # Per-realm account-link mode (project realms only; the platform realm never sets it):
        #   automatic -> silently link a brokered SSO identity to a pre-existing account
        #   confirm   -> same, after one confirmation screen
        #   verify (default / unset) -> Keycloak's stock flow: the user proves ownership of the
        #                               existing account by email link or re-authentication
        # For automatic/confirm, ensure the custom first-broker-login flow exists (the IdPs below
        # reference it by alias) and point the IdPs at it; otherwise keep the stock flow. Switching
        # modes on an existing realm is picked up on reconcile: the flow is reconciled in place and
        # the IdP's firstBrokerLoginFlowAlias is re-pointed via the connector's 409-diff-update.
        account_link = variables.get("account_link")
        first_broker_login_flow_alias = "first broker login"
        if account_link in ("automatic", "confirm"):
            await self.keycloak.ensure_auto_link_first_broker_login_flow(
                realm_name, require_confirmation=(account_link == "confirm")
            )
            first_broker_login_flow_alias = AUTO_LINK_FIRST_BROKER_LOGIN_FLOW

        items = self._expand_list(idp_section, variables)
        for item in items:
            alias = item.get("alias")
            if not alias:
                logger.warning("Identity provider missing 'alias' field, skipping")
                continue

            config = item.get("config", {})
            provider_id = item.get("providerId", "oidc")
            logger.info(f"Adding {provider_id} identity provider: {alias} to realm {realm_name}")

            # Keys consumed by named parameters - remaining config entries flow through as overrides
            saml_named_keys = {
                "idpEntityId",
                "singleSignOnServiceUrl",
                "singleLogoutServiceUrl",
                "nameIDPolicyFormat",
                "principalType",
                "signingCertificate",
                "entityId",
                "validateSignature",
                "wantAssertionsSigned",
                "wantAssertionsEncrypted",
                "syncMode",
            }
            oidc_named_keys = {"clientId", "clientSecret", "discoveryUrl"}

            if provider_id == "saml":
                saml_overrides = {k: v for k, v in config.items() if k not in saml_named_keys}
                # SAML identity provider
                await self.keycloak.add_saml_identity_provider(
                    realm_name=realm_name,
                    provider_alias=alias,
                    display_name=item.get("displayName", alias),
                    idp_entity_id=config.get("idpEntityId"),
                    single_sign_on_service_url=config.get("singleSignOnServiceUrl"),
                    single_logout_service_url=config.get("singleLogoutServiceUrl"),
                    name_id_policy_format=config.get(
                        "nameIDPolicyFormat", "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent"
                    ),
                    principal_type=config.get("principalType", "SUBJECT"),
                    signing_certificate=config.get("signingCertificate"),
                    sp_entity_id=config.get("entityId"),
                    validate_signature=config.get("validateSignature", False),
                    want_assertions_signed=config.get("wantAssertionsSigned", False),
                    want_assertions_encrypted=config.get("wantAssertionsEncrypted", False),
                    authenticate_by_default=item.get("authenticateByDefault", False),
                    sync_mode=config.get("syncMode", "FORCE"),
                    enabled=item.get("enabled", True),
                    update_profile_first_login=item.get("updateProfileFirstLogin", "off"),
                    config_overrides=saml_overrides,
                    first_broker_login_flow_alias=first_broker_login_flow_alias,
                )
            else:
                oidc_overrides = {k: v for k, v in config.items() if k not in oidc_named_keys}
                # OIDC identity provider (default)
                await self.keycloak.add_identity_provider(
                    realm_name=realm_name,
                    provider_alias=alias,
                    display_name=item.get("displayName", alias),
                    client_id=config.get("clientId"),
                    client_secret=config.get("clientSecret"),
                    discovery_url=config.get("discoveryUrl"),
                    authenticate_by_default=item.get("authenticateByDefault", True),
                    update_profile_first_login=item.get("updateProfileFirstLogin", "off"),
                    config_overrides=oidc_overrides,
                    first_broker_login_flow_alias=first_broker_login_flow_alias,
                )

            # Process mappers if present
            if "mappers" in item:
                await self._process_idp_mappers(realm_name, alias, item["mappers"], variables)

    async def _process_idp_mappers(
        self, realm_name: str, provider_alias: str, mappers: list[dict[str, Any]], variables: dict[str, Any]
    ) -> None:
        """Process identity provider mappers from YAML configuration.

        Args:
            realm_name: Realm name
            provider_alias: Identity provider alias
            mappers: List of mapper definitions from YAML
            variables: Context variables
        """
        logger.info(f"Processing {len(mappers)} mappers for {provider_alias}")

        # Get existing mappers for create-or-update logic
        existing_mappers = await self.keycloak.get_identity_provider_mappers(realm_name, provider_alias)
        existing_mapper_by_name = {m.get("name"): m for m in existing_mappers}

        for mapper_def in mappers:
            mapper_name = mapper_def.get("name")
            if not mapper_name:
                logger.warning("Mapper missing 'name' field, skipping")
                continue

            # Build mapper config for Keycloak API
            mapper_config = {
                "name": mapper_name,
                "identityProviderAlias": provider_alias,
                "identityProviderMapper": mapper_def.get("identityProviderMapper"),
                "config": mapper_def.get("config", {}),
            }

            existing = existing_mapper_by_name.get(mapper_name)
            if existing:
                existing["identityProviderMapper"] = mapper_config["identityProviderMapper"]
                existing["config"].update(mapper_config["config"])
                mapper_id = existing.get("id")
                logger.info(f"Updating existing mapper: {mapper_name}")
                await self.keycloak.update_identity_provider_mapper(realm_name, provider_alias, mapper_id, existing)
            else:
                logger.info(f"Creating mapper: {mapper_name}")
                await self.keycloak.create_identity_provider_mapper(realm_name, provider_alias, mapper_config)

    async def _process_authentication_flows(self, flows_section: Any, variables: dict[str, Any]) -> None:
        """Process authenticationFlows section.

        Args:
            flows_section: Authentication flows YAML section
            variables: Context variables
        """
        if not flows_section:
            return

        # Get realm name from context
        realm_name = variables.get("realm_name") or variables.get("project_realm_name")
        if not realm_name:
            logger.warning("No realm_name in context for authentication flows, skipping")
            return

        items = self._expand_list(flows_section, variables)
        logger.debug(f"Processing {len(items)} authentication flow items")

        for item in items:
            logger.debug(
                f"Processing flow item: alias={item.get('alias')}, setAsBrowserFlow={item.get('setAsBrowserFlow')}"
            )

            if item.get("setAsBrowserFlow"):
                # Extract provider alias from the identity-provider-redirector execution config
                provider_alias = None
                executions = item.get("executions", [])
                logger.debug(f"Flow has {len(executions)} executions")

                for execution in executions:
                    authenticator = execution.get("authenticator")
                    logger.debug(f"Checking execution: authenticator={authenticator}")

                    if authenticator == "identity-provider-redirector":
                        auth_config = execution.get("authenticatorConfig", {})
                        config = auth_config.get("config", {})
                        provider_alias = config.get("defaultProvider")
                        logger.debug(
                            f"Found identity-provider-redirector: "
                            f"authenticatorConfig={auth_config}, config={config}, defaultProvider={provider_alias}"
                        )
                        break

                if not provider_alias:
                    # Fallback to context variable or default
                    provider_alias = variables.get("provider_alias", "sso-rijk")
                    logger.warning(f"Could not find defaultProvider in flow config, using fallback: {provider_alias}")

                logger.info(f"Configuring SSO redirect flow for realm {realm_name} with provider {provider_alias}")
                await self.keycloak.configure_sso_redirect_flow(realm_name, provider_alias)

    async def _process_browser_flow(self, flow_alias: str | None, variables: dict[str, Any]) -> None:
        """Set the realm's browser flow to the specified flow alias.

        Args:
            flow_alias: The flow alias to set as browser flow (e.g., "browser")
            variables: Context variables (must include realm_name or project_realm_name)
        """
        if not flow_alias:
            return

        realm_name = variables.get("realm_name") or variables.get("project_realm_name")
        if not realm_name:
            logger.warning("No realm_name in context for browser flow, skipping")
            return

        resolved_alias: str = self._substitute_variables(flow_alias, variables)
        changed = await self.keycloak.ensure_browser_flow(realm_name, resolved_alias)
        if changed:
            logger.info(f"Set browser flow to '{resolved_alias}' for realm {realm_name}")
        else:
            logger.debug(f"Browser flow already '{resolved_alias}' for realm {realm_name}")

    async def _process_client_scopes(self, scopes_section: Any, variables: dict[str, Any]) -> None:
        """Process clientScopes section.

        Args:
            scopes_section: Client scopes YAML section
            variables: Context variables
        """
        if not scopes_section:
            return

        # Get realm name from context
        realm_name = variables.get("realm_name") or variables.get("project_realm_name")
        if not realm_name:
            logger.warning("No realm_name in context for client scopes, skipping")
            return

        items = self._expand_list(scopes_section, variables)
        for item in items:
            scope_name = item.get("name")
            if not scope_name or scope_name != "custom_attributes_passthrough":
                # Only handle our specific scope for now
                continue

            # Determine realm type
            realm_type_str = item.get("realmType", "PROJECT")
            realm_type = RealmType.PLATFORM if realm_type_str == "PLATFORM" else RealmType.PROJECT

            logger.info(f"Creating client scope: {scope_name} in realm {realm_name}")
            await self.keycloak.create_custom_client_scope(
                realm_name=realm_name, scope_name=scope_name, realm_type=realm_type
            )

    async def _process_platform_clients(self, clients_section: Any, variables: dict[str, Any]) -> None:
        """Process platformClients section (federation clients in the platform realm).

        Mirrors _process_clients but creates clients in the YAML-supplied platform
        realm rather than the context's project realm, and captures the created
        client_id + client_secret as an output under `as:` so downstream sections
        (notably the project realm's OIDC IdP) can reference them.
        """
        if not clients_section:
            return

        items = self._expand_list(clients_section, variables)
        for item in items:
            realm_name = item.get("realm")
            client_id = item.get("clientId")
            as_name = item.get("as")

            if not realm_name or not client_id:
                logger.warning("Platform client missing 'realm' or 'clientId', skipping")
                continue

            logger.info(f"Creating platform client: {client_id} in realm {realm_name}")

            client_data = self._build_client_data_from_yaml(item)

            try:
                self.keycloak.admin.change_current_realm(realm_name)

                try:
                    self.keycloak.admin.create_client(payload=client_data)
                    logger.info(f"Created platform client '{client_id}' in realm '{realm_name}'")
                except Exception as e:
                    if "409" in str(e) or "Conflict" in str(e):
                        logger.info(
                            f"Platform client '{client_id}' already exists in realm '{realm_name}', reconciling"
                        )
                        await self._update_existing_client(realm_name, client_id, client_data)
                        # Existing client may have a different secret; fetch the current one
                        # so downstream references resolve to a working credential.
                        existing = await self.keycloak.find_client_by_client_id(client_id, realm_name)
                        if existing:
                            self.keycloak.admin.change_current_realm(realm_name)
                            client_data["secret"] = await self.keycloak.get_client_secret(existing["id"], realm_name)
                    else:
                        raise

                self.keycloak.admin.change_current_realm("master")

            except Exception as e:
                logger.error(f"Failed to create/update platform client '{client_id}': {e}")
                self.keycloak.admin.change_current_realm("master")
                raise

            if as_name:
                output = {
                    "client_id": client_id,
                    "client_secret": client_data.get("secret"),
                    "realm": realm_name,
                }
                self.outputs[as_name] = output
                logger.debug(f"Captured output as '{as_name}': {list(output.keys())}")

    @staticmethod
    def _build_client_data_from_yaml(item: dict[str, Any]) -> dict[str, Any]:
        """Build a Keycloak client representation from a YAML client definition.

        Honors all common client fields declared in YAML and generates a secret
        for confidential clients. Used by both platform and regular client flows
        to keep YAML as the single source of truth.
        """
        import secrets
        import string

        client_id = item["clientId"]
        client_data: dict[str, Any] = {
            "clientId": client_id,
            "name": item.get("name", client_id),
            "protocol": item.get("protocol", "openid-connect"),
            "enabled": item.get("enabled", True),
            "publicClient": item.get("publicClient", False),
            "standardFlowEnabled": item.get("standardFlowEnabled", True),
            "implicitFlowEnabled": item.get("implicitFlowEnabled", False),
            "directAccessGrantsEnabled": item.get("directAccessGrantsEnabled", False),
            "serviceAccountsEnabled": item.get("serviceAccountsEnabled", False),
        }

        if "redirectUris" in item:
            redirect_uris = [uri for uri in item["redirectUris"] if uri is not None]
            if redirect_uris:
                client_data["redirectUris"] = redirect_uris

        if "webOrigins" in item:
            web_origins = [origin for origin in item["webOrigins"] if origin is not None]
            if web_origins:
                client_data["webOrigins"] = web_origins

        if "attributes" in item and isinstance(item["attributes"], dict):
            client_data["attributes"] = dict(item["attributes"])

        if not client_data["publicClient"]:
            alphabet = string.ascii_letters + string.digits
            client_data["secret"] = "".join(secrets.choice(alphabet) for _ in range(32))

        return client_data

    async def _process_clients(self, clients_section: Any, variables: dict[str, Any]) -> None:
        """Process clients section.

        Args:
            clients_section: Clients YAML section
            variables: Context variables
        """
        if not clients_section:
            return

        # Get realm name from context
        realm_name = variables.get("realm_name") or variables.get("project_realm_name")
        if not realm_name:
            logger.warning("No realm_name in context for clients, skipping")
            return

        items = self._expand_list(clients_section, variables)
        for item in items:
            client_id = item.get("clientId")
            if not client_id:
                logger.warning("Client missing 'clientId', skipping")
                continue

            logger.info(f"Creating client: {client_id} in realm {realm_name}")

            # Build client data from YAML configuration
            client_data = {
                "clientId": client_id,
                "name": item.get("name", client_id),
                "protocol": item.get("protocol", "openid-connect"),
                "enabled": item.get("enabled", True),
                "publicClient": item.get("publicClient", False),
                "standardFlowEnabled": item.get("standardFlowEnabled", True),
                "implicitFlowEnabled": item.get("implicitFlowEnabled", False),
                "directAccessGrantsEnabled": item.get("directAccessGrantsEnabled", False),
                "serviceAccountsEnabled": item.get("serviceAccountsEnabled", False),
                "authorizationServicesEnabled": item.get("authorizationServicesEnabled", False),
            }

            # Add redirect URIs
            if "redirectUris" in item:
                # Filter out None values
                redirect_uris = [uri for uri in item["redirectUris"] if uri is not None]
                if redirect_uris:
                    client_data["redirectUris"] = redirect_uris

            # Add post-logout redirect URIs
            if "postLogoutRedirectUris" in item:
                client_data["attributes"] = client_data.get("attributes", {})
                # Filter out None values before joining
                post_logout_uris = [uri for uri in item["postLogoutRedirectUris"] if uri is not None]
                if post_logout_uris:
                    client_data["attributes"]["post.logout.redirect.uris"] = "+".join(post_logout_uris)

            # Add web origins
            if "webOrigins" in item:
                # Filter out None values
                web_origins = [origin for origin in item["webOrigins"] if origin is not None]
                if web_origins:
                    client_data["webOrigins"] = web_origins

            # Add PKCE code challenge method for public clients
            if "pkceCodeChallengeMethod" in item:
                client_data["attributes"] = client_data.get("attributes", {})
                client_data["attributes"]["pkce.code.challenge.method"] = item["pkceCodeChallengeMethod"]

            # Merge any additional attributes from YAML (e.g., login_theme)
            if "attributes" in item and isinstance(item["attributes"], dict):
                client_data["attributes"] = client_data.get("attributes", {})
                client_data["attributes"].update(item["attributes"])

            # Add protocol mappers if specified
            if "protocolMappers" in item:
                client_data["protocolMappers"] = item["protocolMappers"]

            # Generate secret for confidential clients
            if not client_data["publicClient"]:
                import secrets
                import string

                alphabet = string.ascii_letters + string.digits
                client_data["secret"] = "".join(secrets.choice(alphabet) for _ in range(32))

            # Create or update the client
            try:
                self.keycloak.admin.change_current_realm(realm_name)

                try:
                    self.keycloak.admin.create_client(payload=client_data)
                    logger.info(f"Created client '{client_id}' in realm '{realm_name}'")
                except Exception as e:
                    if "409" in str(e) or "Conflict" in str(e):
                        logger.info(
                            f"Client '{client_id}' already exists in realm '{realm_name}', updating redirect URIs"
                        )
                        # Find existing client and update redirect URIs and web origins
                        await self._update_existing_client(realm_name, client_id, client_data)
                    else:
                        raise

                # Handle service account client roles
                if "serviceAccountClientRoles" in item:
                    await self._assign_service_account_roles(realm_name, client_id, item["serviceAccountClientRoles"])

                self.keycloak.admin.change_current_realm("master")

                # Handle client roles
                if "clientRoles" in item:
                    await self._process_client_roles(realm_name, client_id, item["clientRoles"])

                # Handle access restriction
                if "restrictAccess" in item:
                    await self._process_restrict_access(realm_name, client_id, item["restrictAccess"])

                # Handle browser flow override (e.g., to bypass access restrictions for invite client)
                if "browserFlowOverride" in item:
                    flow_alias = item["browserFlowOverride"]
                    logger.info(f"Setting browser flow override '{flow_alias}' for client '{client_id}'")
                    await self.keycloak.set_client_authentication_flow_override(
                        realm_name=realm_name,
                        client_id=client_id,
                        browser_flow_alias=flow_alias,
                    )

            except Exception as e:
                logger.error(f"Failed to create client '{client_id}': {e}")
                self.keycloak.admin.change_current_realm("master")
                raise

    async def _update_existing_client(self, realm_name: str, client_id: str, client_data: dict[str, Any]) -> None:
        """Reconcile an existing client against YAML-driven client_data.

        Compares the supplied YAML-derived client_data against the stored client and
        issues a partial update for fields that drifted. Scalar fields (name, enabled),
        list fields (redirectUris, webOrigins), and the attributes map are all reconciled.

        Args:
            realm_name: Realm name (for logging only; caller is expected to have switched).
            client_id: Client ID (the clientId field, not internal ID)
            client_data: YAML-derived client data
        """
        try:
            # Find existing client by clientId
            all_clients = self.keycloak.admin.get_clients()
            existing_client = None
            for client in all_clients:
                if client.get("clientId") == client_id:
                    existing_client = client
                    break

            if not existing_client:
                logger.warning(f"Could not find existing client '{client_id}' to update")
                return

            update_data: dict[str, Any] = {}
            changed_keys: list[str] = []

            # Scalar fields: only update if drift
            for field in ("name", "enabled"):
                if field in client_data and client_data[field] != existing_client.get(field):
                    update_data[field] = client_data[field]
                    changed_keys.append(field)

            # List fields: compare as sets (order-insensitive)
            for field in ("redirectUris", "webOrigins"):
                if field in client_data:
                    expected = set(client_data[field])
                    existing = set(existing_client.get(field) or [])
                    if expected != existing:
                        update_data[field] = client_data[field]
                        changed_keys.append(field)

            # Attributes: merge YAML on top of existing; record which keys actually drifted
            if "attributes" in client_data:
                existing_attrs = existing_client.get("attributes", {}) or {}
                drifted_attrs = {k: v for k, v in client_data["attributes"].items() if existing_attrs.get(k) != v}
                if drifted_attrs:
                    update_data["attributes"] = {**existing_attrs, **client_data["attributes"]}
                    changed_keys.extend(f"attributes.{k}" for k in drifted_attrs)

            if not update_data:
                logger.debug(f"Client '{client_id}' in realm '{realm_name}' is already in sync")
                return

            self.keycloak.admin.update_client(client_id=existing_client["id"], payload=update_data)
            logger.info(f"Updated client '{client_id}' in realm '{realm_name}': changed_keys={sorted(changed_keys)}")

        except Exception as e:
            logger.error(f"Failed to update existing client '{client_id}': {e}")
            # Don't raise - this is best-effort update during refresh

    async def _assign_service_account_roles(
        self, realm_name: str, client_id: str, role_mappings: dict[str, list[str]]
    ) -> None:
        """Assign roles to a service account.

        Args:
            realm_name: Realm name
            client_id: Client ID
            role_mappings: Dict mapping client IDs to lists of role names
        """
        # Find the service account client
        all_clients = self.keycloak.admin.get_clients()
        service_client = None
        for client in all_clients:
            if client.get("clientId") == client_id:
                service_client = client
                break

        if not service_client:
            logger.warning(f"Could not find client '{client_id}' for service account role assignment")
            return

        # Get service account user
        service_account_user = self.keycloak.admin.get_client_service_account_user(client_id=service_client["id"])

        if not service_account_user:
            logger.warning(f"Could not find service account user for client '{client_id}'")
            return

        user_id = service_account_user["id"]

        # Assign roles for each target client
        for target_client_id, role_names in role_mappings.items():
            # Find target client
            target_client = None
            for client in all_clients:
                if client.get("clientId") == target_client_id:
                    target_client = client
                    break

            if not target_client:
                logger.warning(f"Could not find target client '{target_client_id}' for role assignment")
                continue

            # Get available roles for this client
            available_roles = self.keycloak.admin.get_client_roles(client_id=target_client["id"])

            # Filter to requested roles
            roles_to_assign = [role for role in available_roles if role["name"] in role_names]

            if roles_to_assign:
                self.keycloak.admin.assign_client_role(
                    user_id=user_id, client_id=target_client["id"], roles=roles_to_assign
                )
                logger.info(f"Assigned {len(roles_to_assign)} roles from '{target_client_id}' to service account")
            else:
                logger.warning(f"No matching roles found in '{target_client_id}' for: {role_names}")

    async def _process_realm_roles(self, roles_section: Any, variables: dict[str, Any]) -> None:
        """Process realmRoles section.

        Args:
            roles_section: Realm roles YAML section
            variables: Context variables
        """
        if not roles_section:
            return

        # Get realm name from context
        realm_name = variables.get("realm_name") or variables.get("project_realm_name")
        if not realm_name:
            logger.warning("No realm_name in context for realm roles, skipping")
            return

        items = self._expand_list(roles_section, variables)
        for item in items:
            role_name = item.get("name")
            if not role_name:
                logger.warning("Realm role missing 'name', skipping")
                continue

            logger.info(f"Creating realm role: {role_name} in realm {realm_name}")
            await self.keycloak.create_realm_role(
                realm_name=realm_name, role_name=role_name, description=item.get("description", "")
            )

    async def _process_groups(self, groups_section: Any, variables: dict[str, Any]) -> None:
        """Process groups section.

        Args:
            groups_section: Groups YAML section
            variables: Context variables
        """
        if not groups_section:
            return

        # Get realm name from context
        realm_name = variables.get("realm_name") or variables.get("project_realm_name")
        if not realm_name:
            logger.warning("No realm_name in context for groups, skipping")
            return

        items = self._expand_list(groups_section, variables)
        for item in items:
            group_name = item.get("name")
            if not group_name:
                logger.warning("Group missing 'name', skipping")
                continue

            logger.info(f"Creating group: {group_name} in realm {realm_name}")
            await self.keycloak.create_group(realm_name=realm_name, group_name=group_name, path=item.get("path"))

    async def _process_users(self, users_section: Any, variables: dict[str, Any]) -> None:
        """Process users section.

        Args:
            users_section: Users YAML section
            variables: Context variables
        """
        if not users_section:
            return

        # Get realm name from context
        realm_name = variables.get("realm_name") or variables.get("project_realm_name")
        if not realm_name:
            logger.warning("No realm_name in context for users, skipping")
            return

        items = self._expand_list(users_section, variables)
        for item in items:
            username = item.get("username")
            if not username:
                logger.warning("User missing 'username', skipping")
                continue

            # Extract password from credentials
            password = None
            credentials = item.get("credentials", [])
            if credentials and isinstance(credentials, list):
                for cred in credentials:
                    if cred.get("type") == "password":
                        password = cred.get("value")
                        break

            logger.info(f"Creating user: {username} in realm {realm_name}")
            user_info = await self.keycloak.create_user(
                realm_name=realm_name,
                username=username,
                password=password,
                email=item.get("email"),
                first_name=item.get("firstName"),
                last_name=item.get("lastName"),
                enabled=item.get("enabled", True),
            )

            user_id = user_info["id"]

            # Remove all default realm roles if explicitly requested
            if item.get("removeDefaultRoles", False):
                await self.keycloak.remove_all_realm_roles(realm_name, user_id)

            # Assign realm roles if specified
            if "realmRoles" in item:
                role_names = item["realmRoles"]
                if isinstance(role_names, list):
                    await self.keycloak.assign_realm_roles_to_user(realm_name, user_id, role_names)
                else:
                    logger.warning(f"realmRoles for user {username} is not a list, skipping")

            # Add to groups if specified
            if "groups" in item:
                group_names = item["groups"]
                if isinstance(group_names, list):
                    for group_name in group_names:
                        await self.keycloak.join_user_to_group(realm_name, user_id, group_name)
                else:
                    logger.warning(f"groups for user {username} is not a list, skipping")

    async def _process_client_roles(self, realm_name: str, client_id: str, client_roles: list[dict[str, Any]]) -> None:
        """Process client roles for a client.

        Args:
            realm_name: Realm name
            client_id: Client ID
            client_roles: List of client role definitions
        """
        for role_def in client_roles:
            role_name = role_def.get("name")
            if not role_name:
                logger.warning(f"Client role missing 'name' for client '{client_id}', skipping")
                continue

            description = role_def.get("description", "")
            logger.info(f"Creating client role '{role_name}' for client '{client_id}'")
            await self.keycloak.create_client_role(
                realm_name=realm_name,
                client_id=client_id,
                role_name=role_name,
                description=description,
            )

    async def _process_restrict_access(self, realm_name: str, client_id: str, restrict_access: dict[str, Any]) -> None:
        """Process access restriction configuration for a client.

        This creates a restricted browser flow and sets it as the client's
        authentication flow override.

        Args:
            realm_name: Realm name
            client_id: Client ID
            restrict_access: Access restriction configuration
        """
        if not restrict_access.get("enabled", False):
            logger.debug(f"Access restriction not enabled for client '{client_id}'")
            return

        role_name = restrict_access.get("role")
        if not role_name:
            logger.warning(f"Access restriction for client '{client_id}' missing 'role', skipping")
            return

        error_message = restrict_access.get("errorMessage", "${accessDeniedNoPermission}")

        # Generate a flow alias based on the client ID
        flow_alias = f"browser-restricted-{client_id}"

        logger.info(f"Setting up access restriction for client '{client_id}' with role '{role_name}'")

        # Step 1: Create the restricted browser flow
        await self.keycloak.create_restricted_browser_flow(
            realm_name=realm_name,
            flow_alias=flow_alias,
            client_id=client_id,
            role_name=role_name,
            error_message=error_message,
        )

        # Step 2: Set the flow override on the client
        await self.keycloak.set_client_authentication_flow_override(
            realm_name=realm_name,
            client_id=client_id,
            browser_flow_alias=flow_alias,
        )

        logger.info(f"Access restriction configured for client '{client_id}'")
