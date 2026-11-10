"""Handler for executing Keycloak configuration from YAML files.

This handler processes declarative YAML configurations and executes them
via the Keycloak connector. Supports variable substitution and forEach loops.
"""

import logging
import re
from pathlib import Path
from typing import Any

from opi.connectors.keycloak import (
    AUTO_LINK_FIRST_BROKER_LOGIN_FLOW,
    KeycloakConnector,
    RealmType,
    role_gate_flow_alias,
)
from opi.core.cluster_config import get_mail_from_address
from opi.core.config import settings
from opi.utils.yaml_util import load_yaml_safe

logger = logging.getLogger(__name__)

#: The realm fields a blueprint decides, applied on create AND on reconcile.
#:
#: A key that is absent from a blueprint is not touched, so a realm keeps whatever it has
#: and a blueprint that says nothing about a field claims nothing about it.
#:
#: The last three are the LANGUAGE of what a realm shows and sends, the login screen
#: included. Keycloak's texts stay English until a realm turns internationalisation on and
#: picks a default. ``supportedLocales`` keeps ``en`` alongside ``nl`` so a user can switch,
#: and Keycloak's own Dutch translation is incomplete, so a sentence falls back to English.
_BLUEPRINT_REALM_FIELDS = (
    "registrationAllowed",
    "loginWithEmailAllowed",
    "resetPasswordAllowed",
    "verifyEmail",
    "internationalizationEnabled",
    "supportedLocales",
    "defaultLocale",
)

#: The fields a blueprint sets on an EXISTING realm as protection: lockout, password policy
#: and event retention.
#:
#: Apart from ``_BLUEPRINT_REALM_FIELDS``, which runs over every project realm, because
#: ``configure_sso_redirect_flow`` turns ``bruteForceProtected`` OFF on a realm with SSO
#: redirect: the two would fight over that field on every reconcile. Only
#: ``master-realm.yaml`` names these fields.
_HARDENING_REALM_FIELDS = (
    "bruteForceProtected",
    "failureFactor",
    "waitIncrementSeconds",
    "maxFailureWaitSeconds",
    "maxDeltaTimeSeconds",
    "passwordPolicy",
    "eventsEnabled",
    "eventsExpiration",
    "adminEventsEnabled",
    "adminEventsDetailsEnabled",
)

#: The ``smtpServer`` keys that describe a CONNECTION, and that OPI therefore removes.
#:
#: Not a defence: Keycloak's sender is replaced and ignores this whole map
#: (``images/keycloak-zad/extensions/relay-email-sender/``), and a realm administrator keeps
#: ``manage-realm`` and can write these back a second after the reconcile. It is drift
#: repair of the fail-closed property, which holds only while no realm names a destination:
#: should the SPI ever silently fall back, a realm with a ``host`` would deliver there.
#:
#: Everything not in this list survives, ``replyTo`` above all: the relay does not touch
#: ``Reply-To:``, so that is the one field a realm can really have of its own.
_SMTP_CONNECTION_KEYS = ("host", "port", "auth", "user", "password", "ssl", "starttls")


def _field_matches(current: Any, wanted: Any) -> bool:
    """Does the realm already carry this field, so nothing has to be written?

    ``==`` for almost every field, but not for a LIST: Keycloak keeps ``supportedLocales`` as
    a SET and returns it in an order of its own, so a bare ``!=`` would rewrite the field on
    every single reconcile. The order is therefore not a claim and is not compared as one.
    """
    if isinstance(current, list) and isinstance(wanted, list):
        return sorted(current) == sorted(wanted)
    return current == wanted


class KeycloakYamlHandler:
    def __init__(self, keycloak_connector: KeycloakConnector) -> None:
        self.keycloak = keycloak_connector
        #: Outputs of operations that carried an ``as`` key.
        self.outputs: dict[str, Any] = {}

    async def execute_config(self, yaml_path: str | Path, context: dict[str, Any]) -> None:
        """Run a YAML configuration, substituting the variables in ``context``."""
        logger.info(f"Executing Keycloak configuration from {yaml_path}")

        config = self._load_yaml(yaml_path)
        # Context overrides the YAML's own variables.
        variables = {**config.get("variables", {}), **context}

        logger.debug(f"Context variables: {list(variables.keys())}")

        # Platform clients go before identity providers: an identity provider may reference
        # a platform_client output (client_id, client_secret).
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
        """Only the authenticationFlows section, for an existing realm (idempotent).

        Args:
            context: Must include realm_name.
        """
        logger.info(f"Ensuring authentication flows from {yaml_path}")

        config = self._load_yaml(yaml_path)
        # Context overrides the YAML's own variables.
        variables = {**config.get("variables", {}), **context}

        flows_section = config.get("authenticationFlows")
        if flows_section:
            await self._process_authentication_flows(flows_section, variables)
            logger.info("Authentication flows configuration completed")
        else:
            logger.debug("No authenticationFlows section in configuration")

        await self._process_browser_flow(config.get("browserFlow"), variables)

    async def ensure_identity_providers(self, yaml_path: str | Path, context: dict[str, Any]) -> None:
        """Only the identityProviders section (idempotent).

        platformClients goes first, so the IDP config block can reference the captured
        client_id/client_secret outputs.

        Args:
            context: Must include realm_name.
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
        """Only the clients section, for a realm that already exists (idempotent).

        Args:
            context: Must include realm_name.
        """
        logger.info(f"Ensuring clients from {yaml_path}")

        config = self._load_yaml(yaml_path)
        # Context overrides the YAML's own variables.
        variables = {**config.get("variables", {}), **context}

        clients_section = config.get("clients")
        if clients_section:
            await self._process_clients(clients_section, variables)
            logger.info("Clients configuration completed")
        else:
            logger.debug("No clients section in configuration")

    def _load_yaml(self, yaml_path: str | Path) -> dict[str, Any]:
        return self._load_yaml_resolving_extends(yaml_path, ())

    def _load_yaml_resolving_extends(self, yaml_path: str | Path, seen: tuple[str, ...]) -> dict[str, Any]:
        """Load a config, resolving an ``extends:`` chain onto it.

        A blueprint that only differs from a shared one in a few places declares
        ``extends: <name>`` and carries just those places. Without it the only way to add
        one client to one realm is to copy the whole template, and the copy then drifts.
        """
        path = Path(yaml_path)
        if not path.exists():
            msg = f"YAML configuration file not found: {yaml_path}"
            raise FileNotFoundError(msg)

        config = load_yaml_safe(path.read_text(encoding="utf-8"))

        if not isinstance(config, dict):
            msg = f"Invalid YAML configuration: expected dict, got {type(config)}"
            raise TypeError(msg)

        base_name = config.pop("extends", None)
        if base_name is None:
            return config

        if base_name in seen:
            msg = f"Circular 'extends' in Keycloak blueprints: {' -> '.join([*seen, base_name])}"
            raise ValueError(msg)

        base_path = path.parent / f"{base_name}.yaml"
        base = self._load_yaml_resolving_extends(base_path, (*seen, base_name))
        return self._merge_config(base, config)

    #: Keys that identify an entry in a config list, so a child can override one entry
    #: instead of appending a second one with the same name.
    _IDENTITY_KEYS = ("clientId", "realm", "alias", "username", "name")

    def _merge_config(self, base: Any, override: Any) -> Any:
        """Merge a child blueprint over its base.

        Dicts recurse. Lists merge on the entry's identity: same identity overrides, new
        identity appends. Anything else is replaced.
        """
        if isinstance(base, dict) and isinstance(override, dict):
            merged = dict(base)
            for key, value in override.items():
                merged[key] = self._merge_config(base[key], value) if key in base else value
            return merged
        if isinstance(base, list) and isinstance(override, list):
            return self._merge_list(base, override)
        return override

    def _merge_list(self, base: list[Any], override: list[Any]) -> list[Any]:
        merged = list(base)
        for item in override:
            index = self._find_by_identity(merged, item)
            if index is None:
                merged.append(item)
            else:
                merged[index] = self._merge_config(merged[index], item)
        return merged

    def _find_by_identity(self, entries: list[Any], item: Any) -> int | None:
        if not isinstance(item, dict):
            return None
        for key in self._IDENTITY_KEYS:
            if key not in item:
                continue
            for index, existing in enumerate(entries):
                if isinstance(existing, dict) and existing.get(key) == item[key]:
                    return index
            return None
        return None

    def _substitute_variables(self, value: Any, variables: dict[str, Any]) -> Any:
        """Recursively substitute {{ variable }} placeholders in a str, dict or list."""
        if isinstance(value, str):
            match = re.fullmatch(r"\{\{\s*([^}]+)\s*\}\}", value)
            if match:
                var_path = match.group(1).strip()
                return self._get_nested_value(variables, var_path)

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
        """The value at a dot-separated ``path``, looked up in the outputs before the context.

        Raises:
            KeyError: the path is in neither.
        """
        keys = path.split(".")

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
        """Expand a section into a list of items, resolving forEach loops and variables."""
        if section is None:
            return []

        if isinstance(section, dict) and "forEach" in section:
            return self._expand_foreach(section, variables)

        if isinstance(section, list):
            expanded = []
            for item in section:
                if isinstance(item, dict) and "forEach" in item:
                    expanded.extend(self._expand_foreach(item, variables))
                else:
                    expanded.append(self._substitute_variables(item, variables))
            return expanded

        if isinstance(section, dict):
            return [self._substitute_variables(section, variables)]

        return []

    def _expand_foreach(self, loop_def: dict[str, Any], variables: dict[str, Any]) -> list[dict[str, Any]]:
        """Expand a forEach loop: the collection, the ``as`` name, and the singular key."""
        loop_var = loop_def.get("as", "item")

        for_each_expr = loop_def.get("forEach", "")
        collection = self._substitute_variables(for_each_expr, variables)

        if not isinstance(collection, list):
            logger.warning(f"forEach expression did not resolve to a list: {for_each_expr}")
            return []

        # The singular key, e.g. "user" in a users section.
        template_key = None
        for key in loop_def:
            if key not in ("forEach", "as"):
                template_key = key
                break

        if template_key is None:
            logger.warning("No template found in forEach definition")
            return []

        template = loop_def[template_key]

        expanded = []
        for item in collection:
            loop_context = {**variables, loop_var: item}
            expanded_item = self._substitute_variables(template, loop_context)
            expanded.append(expanded_item)

        return expanded

    async def _process_realms(self, realms_section: Any, variables: dict[str, Any]) -> None:
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
                events_enabled=item.get("eventsEnabled"),
                events_expiration=item.get("eventsExpiration"),
                admin_events_enabled=item.get("adminEventsEnabled"),
                admin_events_details_enabled=item.get("adminEventsDetailsEnabled"),
            )
            await self._apply_realm_self_service(realm_name, item)

    async def _apply_realm_self_service(self, realm_name: str, item: dict[str, Any]) -> None:
        """Apply everything a realms item decides about an EXISTING realm.

        All three are idempotent, so this runs on the create path and on reconcile alike:
        ``create_realm()`` is skipped once a realm exists, so anything applied only there
        never reaches a realm that was made before the blueprint said it.

        1. the minimal ``smtpServer``, which no tenant chooses;
        2. the realm fields in ``_BLUEPRINT_REALM_FIELDS``;
        3. the identity self-service restrictions this method started out as.

        One ``get_realm`` for both of the first two, because both compare against the same
        realm and the ``smtpServer`` write does not move the fields the other one reads.
        """
        realm = await self.keycloak.get_realm(realm_name)
        if realm is None:
            logger.warning(f"Realm {realm_name} not found, skipping realm fields and smtpServer")
        else:
            await self._apply_smtp_server(realm_name, realm)
            await self._apply_realm_fields(realm_name, item, realm)

        for alias in item.get("disabledRequiredActions") or []:
            await self.keycloak.set_required_action_enabled(realm_name, alias, enabled=False)

        # A MAP and not a list, because BOTH states have to be describable. A blueprint that
        # can only say "on" leaves its predecessor's state standing, so a realm moving back
        # from sso-support-otp to sso-support would keep asking every new local account for
        # an OTP.
        for alias, default in (item.get("requiredActionDefaults") or {}).items():
            await self.keycloak.set_required_action_default(realm_name, alias, default=bool(default))

        for entry in item.get("removeFromDefaultRoles") or []:
            client_id, _, role_name = str(entry).partition(":")
            if not role_name:
                logger.warning(f"removeFromDefaultRoles entry '{entry}' is not 'client:role', skipping")
                continue
            await self.keycloak.remove_default_role(realm_name, client_id, role_name)

    @staticmethod
    def _platform_can_send_mail() -> bool:
        """Whether the PLATFORM has a mail relay, which is what decides ``verifyEmail``.

        The measure is the relay and not the realm's ``smtpServer``: every realm carries a
        minimal ``smtpServer`` (see ``_apply_smtp_server``) whether the post works or not,
        and the destination lives in the Keycloak POD's environment. A realm that verifies
        while it cannot send locks out every user who changes their address.
        """
        return bool(settings.MAIL_RELAY_API_URL)

    async def _apply_realm_fields(self, realm_name: str, item: dict[str, Any], realm: dict[str, Any]) -> None:
        """Bring the blueprint-decided realm fields in line with the blueprint.

        Only the fields the blueprint NAMES, and only when they actually differ. Both halves
        matter: an unnamed field is not the blueprint's business, and a write that changes
        nothing still shows up in the admin event log of every realm on every reconcile.

        ``verifyEmail`` is the one exception to "the blueprint decides": turning it ON is
        held back while the platform has no relay, because that realm would lock out every
        user who changes their address. Turning it OFF is never held back: that direction
        only ever un-blocks people.

        A blueprint is not wrong for asking; the CLUSTER is not ready. So this warns and
        carries on, and the next reconcile after the relay is configured completes it.

        Comparing is done through ``_field_matches``, because a LIST does not come back the
        way it went in; see there.
        """
        wanted = {field: item[field] for field in _BLUEPRINT_REALM_FIELDS if field in item}
        if wanted.get("verifyEmail") and not self._platform_can_send_mail():
            logger.warning(
                f"Geen mailrelay op dit cluster, dus verifyEmail blijft uit op realm {realm_name}: een realm "
                "die verifieert en niet kan mailen sluit wie zijn adres wijzigt buiten. Stel MAIL_RELAY_API_URL in; "
                "de volgende verwerking zet het dan alsnog aan."
            )
            del wanted["verifyEmail"]
        if not wanted:
            return

        await self._write_drift(realm_name, realm, wanted)

    async def _write_drift(self, realm_name: str, realm: dict[str, Any], wanted: dict[str, Any]) -> None:
        """Write only the fields that stand differently on the realm than the blueprint asks.

        A write that changes nothing still lands in the admin event log, on every reconcile
        of every project. Comparing goes through ``_field_matches``, because a LIST does not
        come back the way it went in.
        """
        drift = {field: value for field, value in wanted.items() if not _field_matches(realm.get(field), value)}
        await self.keycloak.update_realm_settings(realm_name, drift)

    async def _apply_smtp_server(self, realm_name: str, realm: dict[str, Any]) -> None:
        """Give the realm the MINIMAL ``smtpServer``: one key, ``from``, and no destination.

        Keycloak's own ``EmailSenderProvider`` is replaced on this platform
        (``images/keycloak-zad/extensions/relay-email-sender/``): the relay, the credentials and the
        sender come out of the POD's environment and the realm's ``smtpServer`` is ignored
        entirely. A realm that names a destination is a realm whose administrator can move
        that destination to a listener of their own and read the platform's shared relay
        password out of Keycloak's AUTH exchange (RC-158), so none is written.

        One key is still needed: ``IdpEmailVerificationAuthenticator`` decides before the SPI
        is in the picture, checks ``realm.getSmtpConfig().isEmpty()`` and skips itself, which
        is the "Verify existing account by Email" step of the first-broker-login flow. The
        content does not matter, so it is the one key that names no destination.

        ``from`` is descriptive, not steering: the relay rewrites the sender itself, and the
        address comes from ``get_mail_from_address``, the same value ``MailManager`` hands to
        the relay, so the two cannot drift.

        Nothing happens on a cluster with no relay: writing this key would take the
        ``isEmpty()`` fallback away from that one authenticator, turning a working
        "authenticate to link your account" screen into a failed send.

        Everything OPI does not write survives, ``replyTo`` above all: the relay does not
        touch ``Reply-To:``, so that is the one field a realm can really have of its own.
        """
        if not self._platform_can_send_mail():
            logger.debug(f"Geen mailrelay op dit cluster, realm {realm_name} krijgt geen smtpServer")
            return

        wanted = self._smtp_server_settings()
        current = dict(realm.get("smtpServer") or {})
        merged = {
            **{key: value for key, value in current.items() if key not in _SMTP_CONNECTION_KEYS},
            **wanted,
        }
        if merged == current:
            return

        await self.keycloak.update_realm_settings(realm_name, {"smtpServer": merged})

    @staticmethod
    def _smtp_server_settings() -> dict[str, str]:
        """The whole of what OPI writes into a realm's ``smtpServer``: one key.

        There is deliberately no ``host``, no ``user`` and no ``password``. Without a
        ``host`` a silent fallback to Keycloak's own sender cannot deliver anywhere either:
        it fails with "Please provide a valid address" and a ``SEND_VERIFY_EMAIL_ERROR``.
        That fail-closed property holds only as long as this map names no destination. What
        a tenant writes there anyway is swept off again; see ``_SMTP_CONNECTION_KEYS``.
        """
        return {"from": get_mail_from_address(settings.CLUSTER_MANAGER)}

    async def ensure_realm_self_service(self, yaml_path: str | Path, context: dict[str, Any]) -> None:
        """Reconcile every realm in this blueprint against what the blueprint says (idempotent).

        Reconcile counterpart of the create path: ``create_realm()`` only runs when a realm is
        actually created, so without this an existing realm would never pick these up. That is
        why the realm fields and the ``smtpServer`` go through the same method.
        """
        config = self._load_yaml(yaml_path)
        variables = {**config.get("variables", {}), **context}

        for item in self._expand_list(config.get("realms"), variables):
            realm_name = item.get("name")
            if not realm_name:
                logger.warning("Realm missing 'name' field, skipping self-service restrictions")
                continue
            await self._apply_realm_self_service(realm_name, item)

    async def ensure_realm_hardening(self, yaml_path: str | Path, context: dict[str, Any]) -> None:
        """Set the fields from ``_HARDENING_REALM_FIELDS`` on every realm in this blueprint.

        Smaller than ``ensure_realm_self_service``: a realm passing here already exists and
        gets nothing else, no ``smtpServer`` and no required actions. The master realm is the
        only one that uses this route.
        """
        config = self._load_yaml(yaml_path)
        variables = {**config.get("variables", {}), **context}

        for item in self._expand_list(config.get("realms"), variables):
            realm_name = item.get("name")
            if not realm_name:
                logger.warning("Realm missing 'name' field, skipping hardening fields")
                continue

            realm = await self.keycloak.get_realm(realm_name)
            if realm is None:
                logger.warning(f"Realm {realm_name} not found, skipping hardening fields")
                continue

            wanted = {field: item[field] for field in _HARDENING_REALM_FIELDS if field in item}
            await self._write_drift(realm_name, realm, wanted)

    async def _process_identity_providers(self, idp_section: Any, variables: dict[str, Any]) -> None:
        """The identityProviders section; ``providerId`` picks between OIDC and SAML."""
        if not idp_section:
            return

        realm_name = variables.get("realm_name") or variables.get("project_realm_name")
        if not realm_name:
            logger.warning("No realm_name in context for identity providers, skipping")
            return

        # Per-realm account-link mode (project realms only; the platform realm never sets it):
        #   automatic -> silently link a brokered SSO identity to a pre-existing account
        #   confirm   -> same, after one confirmation screen
        #   unset (the default) -> Keycloak's stock flow: the user proves ownership of the
        #                          existing account by email link or re-authentication
        # automatic and confirm need the custom first-broker-login flow to exist, because the
        # IdPs below reference it by alias.
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

            # Keys consumed by named parameters; the remaining config entries are overrides.
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
                    validate_signature=config.get("validateSignature", True),
                    want_assertions_signed=config.get("wantAssertionsSigned", True),
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

            if "mappers" in item:
                await self._process_idp_mappers(realm_name, alias, item["mappers"], variables)

    async def _process_idp_mappers(
        self, realm_name: str, provider_alias: str, mappers: list[dict[str, Any]], variables: dict[str, Any]
    ) -> None:
        logger.info(f"Processing {len(mappers)} mappers for {provider_alias}")

        existing_mappers = await self.keycloak.get_identity_provider_mappers(realm_name, provider_alias)
        existing_mapper_by_name = {m.get("name"): m for m in existing_mappers}

        for mapper_def in mappers:
            mapper_name = mapper_def.get("name")
            if not mapper_name:
                logger.warning("Mapper missing 'name' field, skipping")
                continue

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
                if not mapper_id:
                    msg = f"Existing mapper '{mapper_name}' on '{provider_alias}' has no id to update"
                    raise ValueError(msg)
                logger.info(f"Updating existing mapper: {mapper_name}")
                await self.keycloak.update_identity_provider_mapper(realm_name, provider_alias, mapper_id, existing)
            else:
                logger.info(f"Creating mapper: {mapper_name}")
                await self.keycloak.create_identity_provider_mapper(realm_name, provider_alias, mapper_config)

    async def _process_authentication_flows(self, flows_section: Any, variables: dict[str, Any]) -> None:
        if not flows_section:
            return

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

            # This reader cannot create nested subflows, and the required OTP step sits in
            # one. The blueprint names the alias, the connector builds the structure, and the
            # ``browserFlow`` key then binds it to the realm.
            if item.get("requireOtp"):
                await self.keycloak.ensure_required_otp_browser_flow(realm_name, item["alias"])
                continue

            if item.get("setAsBrowserFlow"):
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
                    provider_alias = variables.get("provider_alias", "sso-rijk")
                    logger.warning(f"Could not find defaultProvider in flow config, using fallback: {provider_alias}")

                logger.info(f"Configuring SSO redirect flow for realm {realm_name} with provider {provider_alias}")
                await self.keycloak.configure_sso_redirect_flow(realm_name, provider_alias)

    async def _process_browser_flow(self, flow_alias: str | None, variables: dict[str, Any]) -> None:
        """Args:
        variables: Must include realm_name or project_realm_name.
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
        if not scopes_section:
            return

        realm_name = variables.get("realm_name") or variables.get("project_realm_name")
        if not realm_name:
            logger.warning("No realm_name in context for client scopes, skipping")
            return

        items = self._expand_list(scopes_section, variables)
        for item in items:
            scope_name = item.get("name")
            if not scope_name or scope_name != "custom_attributes_passthrough":
                continue

            realm_type_str = item.get("realmType", "PROJECT")
            realm_type = RealmType.PLATFORM if realm_type_str == "PLATFORM" else RealmType.PROJECT

            logger.info(f"Creating client scope: {scope_name} in realm {realm_name}")
            await self.keycloak.create_custom_client_scope(
                realm_name=realm_name, scope_name=scope_name, realm_type=realm_type
            )

    async def _process_platform_clients(self, clients_section: Any, variables: dict[str, Any]) -> None:
        """The platformClients section: federation clients in the platform realm.

        Like _process_clients, but in the YAML-supplied platform realm rather than the
        context's project realm, and the created client_id and client_secret are captured as
        an output under ``as:`` so a later section can reference them.
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
                        # An existing client may have a different secret; fetch the current
                        # one so later references resolve to a working credential.
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
        """A Keycloak client representation from a YAML client definition.

        Generates a secret for a confidential client. Used by both the platform and the
        regular client route, so the YAML stays the single source of truth.
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
        if not clients_section:
            return

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

            if "redirectUris" in item:
                redirect_uris = [uri for uri in item["redirectUris"] if uri is not None]
                if redirect_uris:
                    client_data["redirectUris"] = redirect_uris

            if "postLogoutRedirectUris" in item:
                client_data["attributes"] = client_data.get("attributes", {})
                post_logout_uris = [uri for uri in item["postLogoutRedirectUris"] if uri is not None]
                if post_logout_uris:
                    client_data["attributes"]["post.logout.redirect.uris"] = "+".join(post_logout_uris)

            if "webOrigins" in item:
                web_origins = [origin for origin in item["webOrigins"] if origin is not None]
                if web_origins:
                    client_data["webOrigins"] = web_origins

            if "pkceCodeChallengeMethod" in item:
                client_data["attributes"] = client_data.get("attributes", {})
                client_data["attributes"]["pkce.code.challenge.method"] = item["pkceCodeChallengeMethod"]

            if "attributes" in item and isinstance(item["attributes"], dict):
                client_data["attributes"] = client_data.get("attributes", {})
                client_data["attributes"].update(item["attributes"])

            if "protocolMappers" in item:
                client_data["protocolMappers"] = item["protocolMappers"]

            if not client_data["publicClient"]:
                import secrets
                import string

                alphabet = string.ascii_letters + string.digits
                client_data["secret"] = "".join(secrets.choice(alphabet) for _ in range(32))

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
                        await self._update_existing_client(realm_name, client_id, client_data)
                    else:
                        raise

                if "serviceAccountClientRoles" in item:
                    await self._assign_service_account_roles(realm_name, client_id, item["serviceAccountClientRoles"])

                self.keycloak.admin.change_current_realm("master")

                if "clientRoles" in item:
                    await self._process_client_roles(realm_name, client_id, item["clientRoles"])

                if "restrictAccess" in item:
                    await self._process_restrict_access(realm_name, client_id, item["restrictAccess"])

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
        """Partially update the stored client with the fields that drifted from the YAML.

        Scalar fields (name, enabled), list fields (redirectUris, webOrigins) and the
        attributes map are reconciled.

        Args:
            realm_name: For logging only; the caller is expected to have switched realm.
            client_id: The clientId field, not the internal id.
        """
        try:
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

            for field in ("name", "enabled"):
                if field in client_data and client_data[field] != existing_client.get(field):
                    update_data[field] = client_data[field]
                    changed_keys.append(field)

            # Compared as sets: the order is not a claim.
            for field in ("redirectUris", "webOrigins"):
                if field in client_data:
                    expected = set(client_data[field])
                    existing = set(existing_client.get(field) or [])
                    if expected != existing:
                        update_data[field] = client_data[field]
                        changed_keys.append(field)

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
            # Best effort: a failed update during a refresh does not stop the refresh.

    async def _assign_service_account_roles(
        self, realm_name: str, client_id: str, role_mappings: dict[str, list[str]]
    ) -> None:
        """Args:
        role_mappings: Per client id the role names to assign.
        """
        all_clients = self.keycloak.admin.get_clients()
        service_client = None
        for client in all_clients:
            if client.get("clientId") == client_id:
                service_client = client
                break

        if not service_client:
            logger.warning(f"Could not find client '{client_id}' for service account role assignment")
            return

        service_account_user = self.keycloak.admin.get_client_service_account_user(client_id=service_client["id"])

        if not service_account_user:
            logger.warning(f"Could not find service account user for client '{client_id}'")
            return

        user_id = service_account_user["id"]

        for target_client_id, role_names in role_mappings.items():
            target_client = None
            for client in all_clients:
                if client.get("clientId") == target_client_id:
                    target_client = client
                    break

            if not target_client:
                logger.warning(f"Could not find target client '{target_client_id}' for role assignment")
                continue

            available_roles = self.keycloak.admin.get_client_roles(client_id=target_client["id"])

            roles_to_assign = [role for role in available_roles if role["name"] in role_names]

            if roles_to_assign:
                self.keycloak.admin.assign_client_role(
                    user_id=user_id, client_id=target_client["id"], roles=roles_to_assign
                )
                logger.info(f"Assigned {len(roles_to_assign)} roles from '{target_client_id}' to service account")
            else:
                logger.warning(f"No matching roles found in '{target_client_id}' for: {role_names}")

    async def _process_realm_roles(self, roles_section: Any, variables: dict[str, Any]) -> None:
        if not roles_section:
            return

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
        if not groups_section:
            return

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
        if not users_section:
            return

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

            password = None
            credentials = item.get("credentials", [])
            if credentials and isinstance(credentials, list):
                for cred in credentials:
                    if cred.get("type") == "password":
                        password = cred.get("value")
                        break

            if not password:
                msg = f"User '{username}' in realm '{realm_name}' has no password credential"
                raise ValueError(msg)

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

            if item.get("removeDefaultRoles", False):
                await self.keycloak.remove_all_realm_roles(realm_name, user_id)

            if "realmRoles" in item:
                role_names = item["realmRoles"]
                if isinstance(role_names, list):
                    await self.keycloak.assign_realm_roles_to_user(realm_name, user_id, role_names)
                else:
                    logger.warning(f"realmRoles for user {username} is not a list, skipping")

            if "groups" in item:
                group_names = item["groups"]
                if isinstance(group_names, list):
                    for group_name in group_names:
                        await self.keycloak.join_user_to_group(realm_name, user_id, group_name)
                else:
                    logger.warning(f"groups for user {username} is not a list, skipping")

    async def _process_client_roles(self, realm_name: str, client_id: str, client_roles: list[dict[str, Any]]) -> None:
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
        """Create a restricted browser flow and set it as the client's flow override."""
        if not restrict_access.get("enabled", False):
            logger.debug(f"Access restriction not enabled for client '{client_id}'")
            return

        role_name = restrict_access.get("role")
        if not role_name:
            logger.warning(f"Access restriction for client '{client_id}' missing 'role', skipping")
            return

        error_message = restrict_access.get("errorMessage", "${accessDeniedNoPermission}")

        flow_alias = role_gate_flow_alias(client_id)

        logger.info(f"Setting up access restriction for client '{client_id}' with role '{role_name}'")

        await self.keycloak.create_restricted_browser_flow(
            realm_name=realm_name,
            flow_alias=flow_alias,
            client_id=client_id,
            role_name=role_name,
            error_message=error_message,
        )

        await self.keycloak.set_client_authentication_flow_override(
            realm_name=realm_name,
            client_id=client_id,
            browser_flow_alias=flow_alias,
        )

        logger.info(f"Access restriction configured for client '{client_id}'")
