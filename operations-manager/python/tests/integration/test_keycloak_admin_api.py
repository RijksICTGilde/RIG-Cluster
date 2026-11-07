"""OPI's Keycloak admin API against a live Keycloak (RC-413).

Every ``KeycloakConnector`` method called from ``opi/`` runs here at least once. After each
write, the module reads back the stored state and compares it with what OPI sent. Keycloak
silently ignores unknown fields, so "no exception" proves nothing.

The connector runs in production authentication mode: the service account
``opi-admin-service``, created by OPI's own bootstrap. Requires ``KEYCLOAK_IMAGE_TEST_URL``
(set by ``task test-keycloak``) and skips without it. Coverage is guarded by
``tests/test_keycloak_admin_api_coverage.py``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest
from opi.bootstrap.keycloak_setup import OPERATIONS_REALM_NAME, KeycloakSetup
from opi.connectors.keycloak import RealmType
from opi.core.config import settings
from tests.integration.keycloak_image import (
    SERVICE_ACCOUNT_SECRET,
    TIMEOUT,
    USER_PASSWORD,
    AdminApi,
    assert_realm_consistent,
    execution,
    omgeving,
    subflow,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opi.connectors.keycloak import KeycloakConnector

pytestmark = [pytest.mark.requires_infra, pytest.mark.keycloak_live]

#: Prefix of every realm this module creates, so leftovers are recognisable.
REALM_PREFIX = "adminapi-"


# ==================== Fixtures ====================


@pytest.fixture
def realm(api: AdminApi) -> Iterator[str]:
    """A throwaway realm name; the realm itself is removed afterwards either way."""
    name = f"{REALM_PREFIX}{uuid4().hex[:10]}"
    try:
        yield name
    finally:
        api.drop_realm(name)


@pytest.fixture
async def made_realm(connector: KeycloakConnector, realm: str) -> str:
    """A realm created the way OPI creates one."""
    await connector.create_realm(realm_name=realm, display_name="RC-413 measurement")
    return realm


# ==================== Realm ====================


class TestRealm:
    async def test_create_realm_stores_the_closed_defaults(
        self, connector: KeycloakConnector, api: AdminApi, realm: str
    ) -> None:
        result = await connector.create_realm(
            realm_name=realm,
            display_name="RC-413",
            sso_session_idle_timeout=1800,
            sso_session_max_lifespan=36000,
            events_enabled=True,
            events_expiration=7200,
            admin_events_enabled=True,
            admin_events_details_enabled=True,
        )
        assert result["created"] is True
        assert result["discovery_url"].endswith(f"/realms/{realm}/.well-known/openid-configuration")

        stored = api.realm(realm)
        assert stored["displayName"] == "RC-413"
        assert stored["enabled"] is True
        assert stored["registrationAllowed"] is False
        assert stored["loginWithEmailAllowed"] is False
        assert stored["resetPasswordAllowed"] is False
        assert stored["editUsernameAllowed"] is False
        assert stored["duplicateEmailsAllowed"] is False
        assert stored["bruteForceProtected"] is True
        assert stored["rememberMe"] is False
        assert stored["loginTheme"] == "nl-design-system"
        assert stored["ssoSessionIdleTimeout"] == 1800
        assert stored["ssoSessionMaxLifespan"] == 36000
        assert stored["eventsEnabled"] is True
        assert stored["eventsExpiration"] == 7200
        assert stored["adminEventsEnabled"] is True
        assert stored["adminEventsDetailsEnabled"] is True
        assert stored["browserFlow"] == "browser"

    async def test_create_realm_makes_identity_fields_admin_only(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        """The impersonation hardening: a user cannot self-edit the fields authorization keys on."""
        profile = api.user_profile(made_realm)
        edit_permissions = {
            attribute["name"]: attribute.get("permissions", {}).get("edit", []) for attribute in profile["attributes"]
        }
        for field in ("email", "firstName", "lastName"):
            assert edit_permissions[field] == ["admin"], f"{field} is still self-editable"

    async def test_create_realm_twice_keeps_the_session_settings(
        self, connector: KeycloakConnector, api: AdminApi, realm: str
    ) -> None:
        await connector.create_realm(realm_name=realm, display_name="First", sso_session_idle_timeout=1200)
        second = await connector.create_realm(realm_name=realm, display_name="Second", sso_session_idle_timeout=2400)

        assert second["created"] is True
        stored = api.realm(realm)
        assert stored["ssoSessionIdleTimeout"] == 2400
        # The display name is NOT replayed onto an existing realm, only the session and
        # event settings are; the blueprint owns the rest.
        assert stored["displayName"] == "First"

    async def test_update_realm_settings_writes_only_the_given_fields(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        before = api.realm(made_realm)
        await connector.update_realm_settings(made_realm, {"verifyEmail": True, "rememberMe": True})

        stored = api.realm(made_realm)
        assert stored["verifyEmail"] is True
        assert stored["rememberMe"] is True
        assert stored["loginTheme"] == before["loginTheme"]

        await connector.update_realm_settings(made_realm, {})
        assert api.realm(made_realm)["verifyEmail"] is True

    async def test_set_required_action_enabled_and_default(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.set_required_action_enabled(made_realm, "UPDATE_PASSWORD", enabled=False)
        assert api.required_action(made_realm, "UPDATE_PASSWORD")["enabled"] is False

        await connector.set_required_action_default(made_realm, "CONFIGURE_TOTP", default=True)
        assert api.required_action(made_realm, "CONFIGURE_TOTP")["defaultAction"] is True

        # Idempotent: the second call sees no difference and writes nothing.
        await connector.set_required_action_enabled(made_realm, "UPDATE_PASSWORD", enabled=False)
        assert api.required_action(made_realm, "UPDATE_PASSWORD")["enabled"] is False

    async def test_remove_default_role_drops_manage_account(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        before = {role["name"] for role in api.default_role_composites(made_realm)}
        assert "manage-account" in before

        await connector.remove_default_role(made_realm, "account", "manage-account")
        assert "manage-account" not in {role["name"] for role in api.default_role_composites(made_realm)}

        # Idempotent: gone stays gone, and nothing else is removed with it.
        await connector.remove_default_role(made_realm, "account", "manage-account")
        after = {role["name"] for role in api.default_role_composites(made_realm)}
        assert "view-profile" in after


# ==================== Reading and deleting ====================


class TestReadAndDelete:
    async def test_realm_exists_and_get_realm(self, connector: KeycloakConnector, made_realm: str) -> None:
        assert await connector.realm_exists(made_realm) is True
        assert await connector.realm_exists(f"{REALM_PREFIX}absent") is False

        realm_data = await connector.get_realm(made_realm)
        assert realm_data is not None
        assert realm_data["realm"] == made_realm
        assert await connector.get_realm(f"{REALM_PREFIX}absent") is None

    async def test_list_realms_contains_the_new_realm(self, connector: KeycloakConnector, made_realm: str) -> None:
        realms = await connector.list_realms()
        assert made_realm in realms
        assert "master" in realms

    async def test_realm_discovery_available(self, connector: KeycloakConnector, made_realm: str) -> None:
        assert await connector.realm_discovery_available(made_realm) is True
        assert await connector.realm_discovery_available(f"{REALM_PREFIX}absent") is False

    async def test_connection_works(self, connector: KeycloakConnector) -> None:
        assert await connector.connection_works() is True

    async def test_delete_realm_removes_it(self, connector: KeycloakConnector, api: AdminApi, made_realm: str) -> None:
        assert await connector.delete_realm(made_realm) is True
        assert api.status(f"/admin/realms/{made_realm}") == 404
        assert await connector.realm_exists(made_realm) is False


# ==================== Clients ====================


class TestClients:
    async def test_create_oidc_client_stores_uris_and_derived_origins(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        result = await connector.create_oidc_client(
            realm_name=made_realm,
            client_id="measured-oidc",
            client_name="Measured",
            redirect_uris=["https://app.example.invalid/callback"],
            direct_access_grants=True,
        )
        assert result["created"] is True

        stored = api.client(made_realm, "measured-oidc")
        assert stored["name"] == "Measured"
        assert stored["publicClient"] is False
        assert stored["standardFlowEnabled"] is True
        assert stored["implicitFlowEnabled"] is False
        assert stored["directAccessGrantsEnabled"] is True
        assert stored["serviceAccountsEnabled"] is False
        assert stored["redirectUris"] == ["https://app.example.invalid/callback"]
        assert stored["webOrigins"] == ["https://app.example.invalid"]

    async def test_create_oidc_client_converges_an_existing_one(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.create_oidc_client(
            realm_name=made_realm,
            client_id="measured-oidc",
            redirect_uris=["https://old.example.invalid/callback"],
        )
        second = await connector.create_oidc_client(
            realm_name=made_realm,
            client_id="measured-oidc",
            redirect_uris=["https://new.example.invalid/callback"],
        )
        assert second["created"] is False

        stored = api.client(made_realm, "measured-oidc")
        assert stored["redirectUris"] == ["https://new.example.invalid/callback"]
        assert stored["webOrigins"] == ["https://new.example.invalid"]

    async def test_create_oidc_client_without_redirect_uris_is_refused(
        self, connector: KeycloakConnector, made_realm: str
    ) -> None:
        with pytest.raises(ValueError, match="at least one redirect URI"):
            await connector.create_oidc_client(realm_name=made_realm, client_id="no-uris")

    async def test_find_client_get_and_regenerate_secret(self, connector: KeycloakConnector, made_realm: str) -> None:
        await connector.create_oidc_client(
            realm_name=made_realm,
            client_id="measured-oidc",
            redirect_uris=["https://app.example.invalid/callback"],
        )
        found = await connector.find_client_by_client_id("measured-oidc", made_realm)
        assert found is not None
        assert await connector.find_client_by_client_id("absent-client", made_realm) is None

        first = await connector.get_client_secret(found["id"], made_realm)
        assert first
        regenerated = await connector.regenerate_client_secret(found["id"], made_realm)
        assert regenerated != first
        assert await connector.get_client_secret(found["id"], made_realm) == regenerated

    async def test_create_deployment_client_stores_uris_attributes_and_public_twin(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        # The scope is created by the realm blueprint before any deployment client, and
        # create_deployment_client only assigns an existing one.
        await connector.create_custom_client_scope(made_realm, realm_type=RealmType.PROJECT)

        result = await connector.create_deployment_client(
            deployment_name="depl",
            project_name="proj",
            ingress_hosts=["depl.example.invalid", "alias.example.invalid"],
            realm_name=made_realm,
            support_http=True,
            additional_redirect_uris=["http://localhost:8080/*"],
            logout_path="/logout",
            require_pkce=True,
        )
        assert result["created"] is True
        assert result["client_id"] == "proj-depl"
        assert result["public_client_id"] == "proj-depl-public"

        stored = api.client(made_realm, "proj-depl")
        assert "https://depl.example.invalid/*" in stored["redirectUris"]
        assert "http://depl.example.invalid/*" in stored["redirectUris"]
        assert "http://localhost:8080/*" in stored["redirectUris"]
        assert "https://alias.example.invalid" in stored["webOrigins"]
        assert stored["frontchannelLogout"] is True
        assert stored["attributes"]["post.logout.redirect.uris"] == "+"
        assert stored["attributes"]["frontchannel.logout.url"] == "https://depl.example.invalid/logout"
        assert stored["attributes"]["pkce.code.challenge.method"] == "S256"
        assert stored["publicClient"] is False

        public = api.client(made_realm, "proj-depl-public")
        assert public["publicClient"] is True

        # The custom scope is assigned to both clients, so tokens carry the passthrough.
        assert "custom_attributes_passthrough" in api.default_client_scopes(made_realm, stored["id"])
        assert "custom_attributes_passthrough" in api.default_client_scopes(made_realm, public["id"])

    async def test_create_deployment_client_updates_hosts_and_clears_the_logout_url(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.create_deployment_client(
            deployment_name="depl",
            project_name="proj",
            ingress_hosts=["old.example.invalid"],
            realm_name=made_realm,
            logout_path="/logout",
        )
        second = await connector.create_deployment_client(
            deployment_name="depl",
            project_name="proj",
            ingress_hosts=["new.example.invalid"],
            realm_name=made_realm,
        )
        assert second["created"] is False
        assert second["updated"] is True

        stored = api.client(made_realm, "proj-depl")
        assert stored["redirectUris"] == ["https://new.example.invalid/*"]
        # Keycloak drops an attribute whose value is empty instead of storing "", so the
        # stale URL is gone rather than blanked.
        assert not stored["attributes"].get("frontchannel.logout.url")

    async def test_create_deployment_client_twice_changes_nothing_the_second_time(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        arguments = {
            "deployment_name": "depl",
            "project_name": "proj",
            "ingress_hosts": ["depl.example.invalid"],
            "realm_name": made_realm,
            "logout_path": "/logout",
        }
        await connector.create_deployment_client(**arguments)
        first = api.client(made_realm, "proj-depl")

        second_result = await connector.create_deployment_client(**arguments)
        assert second_result["updated"] is False
        second = api.client(made_realm, "proj-depl")
        assert second["redirectUris"] == first["redirectUris"]
        assert second["attributes"] == first["attributes"]

    async def test_list_client_ids_by_prefix(self, connector: KeycloakConnector, made_realm: str) -> None:
        for name in ("dbconsole-a", "dbconsole-b", "other"):
            await connector.create_oidc_client(
                realm_name=made_realm,
                client_id=name,
                redirect_uris=["https://app.example.invalid/callback"],
            )
        found = await connector.list_client_ids_by_prefix(made_realm, "dbconsole-")
        assert sorted(found) == ["dbconsole-a", "dbconsole-b"]

    async def test_delete_deployment_client_removes_both_clients(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.create_deployment_client(
            deployment_name="depl",
            project_name="proj",
            ingress_hosts=["depl.example.invalid"],
            realm_name=made_realm,
        )
        assert await connector.delete_deployment_client("depl", "proj", made_realm) is True

        assert api.client_or_none(made_realm, "proj-depl") is None
        assert api.client_or_none(made_realm, "proj-depl-public") is None
        assert await connector.delete_deployment_client("depl", "proj", made_realm) is False

    async def test_delete_client_by_client_id_and_delete_oidc_client(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        for name in ("by-client-id", "by-oidc"):
            await connector.create_oidc_client(
                realm_name=made_realm,
                client_id=name,
                redirect_uris=["https://app.example.invalid/callback"],
            )

        assert await connector.delete_client_by_client_id(made_realm, "by-client-id") is True
        assert api.client_or_none(made_realm, "by-client-id") is None
        assert await connector.delete_client_by_client_id(made_realm, "by-client-id") is False

        assert await connector.delete_oidc_client(made_realm, "by-oidc") is True
        assert api.client_or_none(made_realm, "by-oidc") is None
        assert await connector.delete_oidc_client(made_realm, "by-oidc") is False

    async def test_list_clients_contains_the_builtin_and_the_new_client(
        self, connector: KeycloakConnector, made_realm: str
    ) -> None:
        await connector.create_oidc_client(
            realm_name=made_realm,
            client_id="measured-oidc",
            redirect_uris=["https://app.example.invalid/callback"],
        )
        listed = await connector.list_clients(made_realm)
        by_client_id = {client["client_id"]: client for client in listed}
        assert "account" in by_client_id
        assert by_client_id["measured-oidc"]["public"] is False
        assert by_client_id["measured-oidc"]["internal_id"]

    async def test_create_custom_client_scope_adds_mappers_and_becomes_a_realm_default(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        scope = await connector.create_custom_client_scope(made_realm, realm_type=RealmType.PROJECT)
        assert scope is not None

        stored = next(s for s in api.client_scopes(made_realm) if s["name"] == "custom_attributes_passthrough")
        assert stored["protocol"] == "openid-connect"
        assert stored["attributes"]["include.in.token.scope"] == "true"
        assert "custom_attributes_passthrough" in api.default_realm_scopes(made_realm)

        mappers = api.scope_mappers(made_realm, stored["id"])
        assert mappers, "The scope carries no protocol mappers"

        # Idempotent: a second call neither duplicates the scope nor its mappers.
        await connector.create_custom_client_scope(made_realm, realm_type=RealmType.PROJECT)
        again = [s for s in api.client_scopes(made_realm) if s["name"] == "custom_attributes_passthrough"]
        assert len(again) == 1
        assert len(api.scope_mappers(made_realm, stored["id"])) == len(mappers)


# ==================== Identity providers ====================


class TestIdentityProviders:
    @staticmethod
    def _discovery_url(keycloak_url: str) -> str:
        """A discovery URL that really resolves, so the derived endpoints are checkable."""
        return f"{keycloak_url}/realms/master/.well-known/openid-configuration"

    async def test_add_identity_provider_stores_config_and_derived_endpoints(
        self, connector: KeycloakConnector, api: AdminApi, keycloak_url: str, made_realm: str
    ) -> None:
        discovery = self._discovery_url(keycloak_url)
        await connector.add_identity_provider(
            realm_name=made_realm,
            provider_alias="sso-rijk",
            display_name="SSO Rijk",
            client_id="broker-client",
            client_secret="broker-secret",
            discovery_url=discovery,
        )

        stored = api.idp(made_realm, "sso-rijk")
        assert stored["providerId"] == "oidc"
        assert stored["displayName"] == "SSO Rijk"
        assert stored["enabled"] is True
        assert stored["trustEmail"] is True
        assert stored["storeToken"] is True
        assert stored["linkOnly"] is False
        assert stored["firstBrokerLoginFlowAlias"] == "first broker login"
        # 26.8.1 does not store updateProfileFirstLoginMode at all: the profile-review step
        # is now decided by the first-broker-login flow. OPI still sends the field, and
        # Keycloak ignores it silently.
        assert "updateProfileFirstLoginMode" not in stored

        config = stored["config"]
        assert config["clientId"] == "broker-client"
        assert config["syncMode"] == "IMPORT"
        assert config["validateSignature"] == "true"
        assert config["useJwksUrl"] == "true"
        assert config["authorizationUrl"] == f"{keycloak_url}/realms/master/protocol/openid-connect/auth"
        assert config["tokenUrl"] == f"{keycloak_url}/realms/master/protocol/openid-connect/token"
        assert config["jwksUrl"] == f"{keycloak_url}/realms/master/protocol/openid-connect/certs"

    async def test_add_identity_provider_lets_overrides_win_and_converges_on_replay(
        self, connector: KeycloakConnector, api: AdminApi, keycloak_url: str, made_realm: str
    ) -> None:
        discovery = self._discovery_url(keycloak_url)
        arguments: dict[str, Any] = {
            "realm_name": made_realm,
            "provider_alias": "sso-rijk",
            "display_name": "SSO Rijk",
            "client_id": "broker-client",
            "client_secret": "broker-secret",
            "discovery_url": discovery,
        }
        await connector.add_identity_provider(**arguments, config_overrides={"syncMode": "FORCE"})
        assert api.idp(made_realm, "sso-rijk")["config"]["syncMode"] == "FORCE"

        # Replay with a different display name and override: the existing provider is
        # updated rather than duplicated or left behind.
        await connector.add_identity_provider(
            **{**arguments, "display_name": "SSO Rijk (new)"},
            config_overrides={"syncMode": "LEGACY"},
        )
        stored = api.idp(made_realm, "sso-rijk")
        assert stored["displayName"] == "SSO Rijk (new)"
        assert stored["config"]["syncMode"] == "LEGACY"
        assert len(api.get(f"/admin/realms/{made_realm}/identity-provider/instances")) == 1

    async def test_add_saml_identity_provider_stores_the_saml_config(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.add_saml_identity_provider(
            realm_name=made_realm,
            provider_alias="sso-rijk-saml",
            display_name="SSO Rijk SAML",
            idp_entity_id="https://idp.example.invalid/entity",
            single_sign_on_service_url="https://idp.example.invalid/sso",
            single_logout_service_url="https://idp.example.invalid/slo",
            sp_entity_id="https://sp.example.invalid/entity",
            validate_signature=False,
            want_assertions_signed=False,
            sync_mode="FORCE",
        )

        stored = api.idp(made_realm, "sso-rijk-saml")
        assert stored["providerId"] == "saml"
        assert stored["trustEmail"] is True
        assert stored["storeToken"] is False

        config = stored["config"]
        assert config["idpEntityId"] == "https://idp.example.invalid/entity"
        assert config["singleSignOnServiceUrl"] == "https://idp.example.invalid/sso"
        assert config["singleLogoutServiceUrl"] == "https://idp.example.invalid/slo"
        assert config["entityId"] == "https://sp.example.invalid/entity"
        assert config["syncMode"] == "FORCE"
        assert config["validateSignature"] == "false"
        assert config["wantAssertionsSigned"] == "false"
        assert config["principalType"] == "SUBJECT"

    async def test_add_saml_identity_provider_refuses_validation_without_a_certificate(
        self, connector: KeycloakConnector, made_realm: str
    ) -> None:
        with pytest.raises(ValueError, match="cannot verify assertion signatures"):
            await connector.add_saml_identity_provider(
                realm_name=made_realm,
                provider_alias="unsigned",
                display_name="Unsigned",
                idp_entity_id="https://idp.example.invalid/entity",
                single_sign_on_service_url="https://idp.example.invalid/sso",
                validate_signature=True,
            )

    async def test_update_identity_provider_merges_into_the_stored_config(
        self, connector: KeycloakConnector, api: AdminApi, keycloak_url: str, made_realm: str
    ) -> None:
        await connector.add_identity_provider(
            realm_name=made_realm,
            provider_alias="sso-rijk",
            display_name="SSO Rijk",
            client_id="broker-client",
            client_secret="broker-secret",
            discovery_url=self._discovery_url(keycloak_url),
        )
        await connector.update_identity_provider(
            made_realm, "sso-rijk", config={"syncMode": "FORCE", "uiLocales": "true"}
        )

        config = api.idp(made_realm, "sso-rijk")["config"]
        assert config["syncMode"] == "FORCE"
        assert config["uiLocales"] == "true"
        assert config["clientId"] == "broker-client", "The merge dropped a field it did not own"

    async def test_get_identity_providers_lists_what_was_added(
        self, connector: KeycloakConnector, keycloak_url: str, made_realm: str
    ) -> None:
        assert await connector.get_identity_providers(made_realm) == []
        await connector.add_identity_provider(
            realm_name=made_realm,
            provider_alias="sso-rijk",
            display_name="SSO Rijk",
            client_id="broker-client",
            client_secret="broker-secret",
            discovery_url=self._discovery_url(keycloak_url),
        )
        aliases = [provider["alias"] for provider in await connector.get_identity_providers(made_realm)]
        assert aliases == ["sso-rijk"]

    async def test_identity_provider_mappers_use_our_own_xpath_mapper(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        """The mapper type from our own image, so the image is measured and not just Keycloak."""
        await connector.add_saml_identity_provider(
            realm_name=made_realm,
            provider_alias="sso-rijk-saml",
            display_name="SSO Rijk SAML",
            idp_entity_id="https://idp.example.invalid/entity",
            single_sign_on_service_url="https://idp.example.invalid/sso",
            validate_signature=False,
        )

        created = await connector.create_identity_provider_mapper(
            realm_name=made_realm,
            provider_alias="sso-rijk-saml",
            mapper_config={
                "name": "organisatie",
                "identityProviderAlias": "sso-rijk-saml",
                "identityProviderMapper": "saml-unrestricted-xpath-idp-mapper",
                "config": {
                    "syncMode": "FORCE",
                    "xpath": "//*[local-name()='Organisatie']",
                    "user.attribute": "organisatie",
                },
            },
        )
        assert created is not None

        mappers = api.idp_mappers(made_realm, "sso-rijk-saml")
        assert len(mappers) == 1
        assert mappers[0]["identityProviderMapper"] == "saml-unrestricted-xpath-idp-mapper"
        assert mappers[0]["config"]["syncMode"] == "FORCE"
        assert mappers[0]["config"]["user.attribute"] == "organisatie"

        read = await connector.get_identity_provider_mappers(made_realm, "sso-rijk-saml")
        assert [mapper["id"] for mapper in read] == [mappers[0]["id"]]

        updated = await connector.update_identity_provider_mapper(
            realm_name=made_realm,
            provider_alias="sso-rijk-saml",
            mapper_id=mappers[0]["id"],
            mapper_config={**mappers[0], "config": {**mappers[0]["config"], "user.attribute": "afdeling"}},
        )
        assert updated is True
        assert api.idp_mappers(made_realm, "sso-rijk-saml")[0]["config"]["user.attribute"] == "afdeling"


# ==================== Authentication flows ====================


class TestFlows:
    async def test_configure_sso_redirect_flow_builds_the_redirector_and_binds_it(
        self, connector: KeycloakConnector, api: AdminApi, keycloak_url: str, made_realm: str
    ) -> None:
        await connector.add_identity_provider(
            realm_name=made_realm,
            provider_alias="sso-rijk",
            display_name="SSO Rijk",
            client_id="broker-client",
            client_secret="broker-secret",
            discovery_url=f"{keycloak_url}/realms/master/.well-known/openid-configuration",
        )
        await connector.configure_sso_redirect_flow(made_realm, "sso-rijk")

        realm_data = api.realm(made_realm)
        assert realm_data["browserFlow"] == "External IDP Redirector"
        assert realm_data["registrationAllowed"] is False
        assert realm_data["resetPasswordAllowed"] is False
        assert realm_data["loginWithEmailAllowed"] is False

        executions = api.executions(made_realm, "External IDP Redirector")
        assert execution(executions, "auth-cookie")["requirement"] == "ALTERNATIVE"
        redirector = execution(executions, "identity-provider-redirector")
        assert redirector["requirement"] == "ALTERNATIVE"
        config = api.authenticator_config(made_realm, redirector["authenticationConfig"])
        assert config["config"]["defaultProvider"] == "sso-rijk"

        # Idempotent: a replay neither duplicates executions nor loses the binding.
        await connector.configure_sso_redirect_flow(made_realm, "sso-rijk")
        again = api.executions(made_realm, "External IDP Redirector")
        assert len(again) == len(executions)
        assert api.realm(made_realm)["browserFlow"] == "External IDP Redirector"

    async def test_ensure_browser_flow_binds_an_existing_flow_once(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.ensure_required_otp_browser_flow(made_realm, "required-otp")

        assert await connector.ensure_browser_flow(made_realm, "required-otp") is True
        assert api.realm(made_realm)["browserFlow"] == "required-otp"
        assert await connector.ensure_browser_flow(made_realm, "required-otp") is False

    async def test_ensure_required_otp_browser_flow_always_demands_an_otp(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.ensure_required_otp_browser_flow(made_realm, "required-otp")

        executions = api.executions(made_realm, "required-otp")
        assert execution(executions, "auth-cookie")["requirement"] == "ALTERNATIVE"
        assert execution(executions, "identity-provider-redirector")["requirement"] == "ALTERNATIVE"
        assert subflow(executions, "required-otp forms")["requirement"] == "ALTERNATIVE"
        assert execution(executions, "auth-username-password-form")["requirement"] == "REQUIRED"
        # The OTP subflow is REQUIRED and the "has the user configured one" condition is off,
        # which together is what makes an OTP unavoidable.
        assert subflow(executions, "required-otp otp")["requirement"] == "REQUIRED"
        assert execution(executions, "conditional-user-configured")["requirement"] == "DISABLED"
        assert execution(executions, "auth-otp-form")["requirement"] == "REQUIRED"

        # Idempotent: no second copy of any step.
        await connector.ensure_required_otp_browser_flow(made_realm, "required-otp")
        again = api.executions(made_realm, "required-otp")
        assert [(e.get("displayName"), e.get("requirement")) for e in again] == [
            (e.get("displayName"), e.get("requirement")) for e in executions
        ]

    async def test_create_restricted_browser_flow_gates_on_a_client_role(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.create_oidc_client(
            realm_name=made_realm,
            client_id="gated-app",
            redirect_uris=["https://app.example.invalid/callback"],
        )
        await connector.create_client_role(made_realm, "gated-app", "user")
        await connector.create_restricted_browser_flow(
            realm_name=made_realm,
            flow_alias="gate-client",
            client_id="gated-app",
            role_name="user",
            redirect_to_idp=None,
        )

        executions = api.executions(made_realm, "gate-client")
        # The authenticators sit in a REQUIRED subflow of their own; the role check sits
        # beside it, not between the ALTERNATIVE login steps.
        assert subflow(executions, "gate-client-authenticate")["requirement"] == "REQUIRED"
        assert subflow(executions, "gate-client-deny-no-role")["requirement"] == "CONDITIONAL"
        assert execution(executions, "auth-cookie")["requirement"] == "ALTERNATIVE"
        assert execution(executions, "auth-username-password-form")["requirement"] == "REQUIRED"
        # Without require_otp the OTP step only applies to users who configured one.
        assert subflow(executions, "gate-client-otp")["requirement"] == "CONDITIONAL"
        assert execution(executions, "conditional-user-configured")["requirement"] == "REQUIRED"

        condition = execution(executions, "conditional-user-role")
        assert condition["requirement"] == "REQUIRED"
        condition_config = api.authenticator_config(made_realm, condition["authenticationConfig"])["config"]
        assert condition_config["condUserRole"] == "gated-app.user"
        assert condition_config["negate"] == "true"
        assert execution(executions, "deny-access-authenticator")["requirement"] == "REQUIRED"

        # The deny subflow must come AFTER authentication, or everyone is refused.
        aliases = [e.get("displayName") for e in executions]
        assert aliases.index("gate-client-authenticate") < aliases.index("gate-client-deny-no-role")

        # Idempotent.
        await connector.create_restricted_browser_flow(
            realm_name=made_realm,
            flow_alias="gate-client",
            client_id="gated-app",
            role_name="user",
            redirect_to_idp=None,
        )
        assert [(e.get("displayName"), e.get("requirement")) for e in api.executions(made_realm, "gate-client")] == [
            (e.get("displayName"), e.get("requirement")) for e in executions
        ]

    async def test_create_restricted_browser_flow_realm_role_can_redirect_and_require_otp(
        self, connector: KeycloakConnector, api: AdminApi, keycloak_url: str, made_realm: str
    ) -> None:
        await connector.add_identity_provider(
            realm_name=made_realm,
            provider_alias="sso-rijk",
            display_name="SSO Rijk",
            client_id="broker-client",
            client_secret="broker-secret",
            discovery_url=f"{keycloak_url}/realms/master/.well-known/openid-configuration",
        )
        await connector.create_realm_role(made_realm, "project-user")
        await connector.create_restricted_browser_flow_realm_role(
            realm_name=made_realm,
            flow_alias="gate-realm",
            role_name="project-user",
            redirect_to_idp="sso-rijk",
            require_otp=True,
        )

        executions = api.executions(made_realm, "gate-realm")
        condition = execution(executions, "conditional-user-role")
        condition_config = api.authenticator_config(made_realm, condition["authenticationConfig"])["config"]
        assert condition_config["condUserRole"] == "project-user"
        assert condition_config["negate"] == "true"

        # redirect_to_idp has to reach the redirector, or the gate shows a password form
        # where the realm promises an SSO redirect.
        redirector = execution(executions, "identity-provider-redirector")
        redirector_config = api.authenticator_config(made_realm, redirector["authenticationConfig"])["config"]
        assert redirector_config["defaultProvider"] == "sso-rijk"

        assert subflow(executions, "gate-realm-otp")["requirement"] == "REQUIRED"
        assert execution(executions, "conditional-user-configured")["requirement"] == "DISABLED"

    async def test_each_deployment_gate_in_a_realm_gets_its_own_config(
        self, connector: KeycloakConnector, api: AdminApi, keycloak_url: str, made_realm: str
    ) -> None:
        """Keycloak 26 refuses a config alias that already exists in the realm.

        Deployments of a project share a realm and usually a role, so every step here runs
        twice, and again to prove a reprocess leaves both gates as they are.
        """
        await connector.add_identity_provider(
            realm_name=made_realm,
            provider_alias="sso-rijk",
            display_name="SSO Rijk",
            client_id="broker-client",
            client_secret="broker-secret",
            discovery_url=f"{keycloak_url}/realms/master/.well-known/openid-configuration",
        )
        await connector.configure_sso_redirect_flow(made_realm, "sso-rijk")
        await connector.create_realm_role(made_realm, "allowed-user")

        for _ in range(2):
            for deployment in ("one", "two"):
                await connector.create_restricted_browser_flow_realm_role(
                    realm_name=made_realm,
                    flow_alias=f"gate-{deployment}",
                    role_name="allowed-user",
                    redirect_to_idp="sso-rijk",
                )
                await connector.create_post_broker_login_flow_realm_role(
                    realm_name=made_realm,
                    flow_alias=f"post-broker-{deployment}",
                    role_name="allowed-user",
                )

        for deployment in ("one", "two"):
            gate = api.executions(made_realm, f"gate-{deployment}")
            condition = execution(gate, "conditional-user-role")
            assert (
                api.authenticator_config(made_realm, condition["authenticationConfig"])["config"]["condUserRole"]
                == "allowed-user"
            )
            redirector = execution(gate, "identity-provider-redirector")
            assert (
                api.authenticator_config(made_realm, redirector["authenticationConfig"])["config"]["defaultProvider"]
                == "sso-rijk"
            )
            # Without its config this step fails every SSO login in the realm.
            authenticator = execution(
                api.executions(made_realm, f"post-broker-{deployment}"), "require-client-role-authenticator"
            )
            assert (
                api.authenticator_config(made_realm, authenticator["authenticationConfig"])["config"]["roleName"]
                == "allowed-user"
            )

    async def test_set_client_authentication_flow_override_stores_the_flow_id(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.create_oidc_client(
            realm_name=made_realm,
            client_id="gated-app",
            redirect_uris=["https://app.example.invalid/callback"],
        )
        await connector.create_realm_role(made_realm, "project-user")
        await connector.create_restricted_browser_flow_realm_role(
            realm_name=made_realm, flow_alias="gate-realm", role_name="project-user"
        )
        await connector.set_client_authentication_flow_override(made_realm, "gated-app", "gate-realm")

        flow_id = next(flow["id"] for flow in api.flows(made_realm) if flow["alias"] == "gate-realm")
        stored = api.client(made_realm, "gated-app")
        assert stored["authenticationFlowBindingOverrides"]["browser"] == flow_id

    async def test_ensure_auto_link_first_broker_login_flow_builds_the_auto_link_branch(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        alias = "auto-link"
        await connector.ensure_auto_link_first_broker_login_flow(made_realm, flow_alias=alias)

        executions = api.executions(made_realm, alias)
        assert execution(executions, "idp-review-profile")["requirement"] == "DISABLED"
        assert subflow(executions, f"{alias} user creation or linking")["requirement"] == "REQUIRED"
        assert execution(executions, "idp-create-user-if-unique")["requirement"] == "ALTERNATIVE"
        assert subflow(executions, f"{alias} handle existing account")["requirement"] == "ALTERNATIVE"
        assert execution(executions, "idp-auto-link")["requirement"] == "REQUIRED"
        # Without confirmation there is no idp-confirm-link step at all.
        assert not [e for e in executions if e.get("providerId") == "idp-confirm-link"]
        # idp-create-user-if-unique must run before the handle-existing subflow.
        order = [e.get("providerId") or e.get("displayName") for e in executions]
        assert order.index("idp-create-user-if-unique") < order.index(f"{alias} handle existing account")

        # Idempotent, and switching require_confirmation on adds exactly the one step.
        await connector.ensure_auto_link_first_broker_login_flow(made_realm, flow_alias=alias)
        assert len(api.executions(made_realm, alias)) == len(executions)

        await connector.ensure_auto_link_first_broker_login_flow(
            made_realm, require_confirmation=True, flow_alias=alias
        )
        with_confirmation = api.executions(made_realm, alias)
        assert execution(with_confirmation, "idp-confirm-link")["requirement"] == "REQUIRED"

    async def test_create_post_broker_login_flow_uses_our_own_role_authenticator(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        """The authenticator from our own image, configured for a client role."""
        await connector.create_oidc_client(
            realm_name=made_realm,
            client_id="gated-app",
            redirect_uris=["https://app.example.invalid/callback"],
        )
        await connector.create_client_role(made_realm, "gated-app", "user")
        await connector.create_post_broker_login_flow(
            realm_name=made_realm,
            flow_alias="post-broker-client",
            client_id="gated-app",
            role_name="user",
            skip_clients=["invite-client"],
        )

        executions = api.executions(made_realm, "post-broker-client")
        authenticator = execution(executions, "require-client-role-authenticator")
        assert authenticator["requirement"] == "REQUIRED"
        config = api.authenticator_config(made_realm, authenticator["authenticationConfig"])["config"]
        assert config["clientId"] == "gated-app"
        assert config["roleName"] == "user"
        assert config["errorMessage"] == "${accessDeniedNoPermission}"
        assert config["skipClients"] == "invite-client"

        # Idempotent: the same execution and config, not a second one.
        await connector.create_post_broker_login_flow(
            realm_name=made_realm,
            flow_alias="post-broker-client",
            client_id="gated-app",
            role_name="user",
            skip_clients=["invite-client"],
        )
        again = api.executions(made_realm, "post-broker-client")
        assert len(again) == len(executions)
        assert again[0]["authenticationConfig"] == authenticator["authenticationConfig"]

    async def test_create_post_broker_login_flow_realm_role_omits_the_client_id(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.create_realm_role(made_realm, "project-user")
        await connector.create_post_broker_login_flow_realm_role(
            realm_name=made_realm, flow_alias="post-broker-realm", role_name="project-user"
        )

        authenticator = execution(api.executions(made_realm, "post-broker-realm"), "require-client-role-authenticator")
        config = api.authenticator_config(made_realm, authenticator["authenticationConfig"])["config"]
        assert config["roleName"] == "project-user"
        assert "clientId" not in config or not config["clientId"]

    async def test_set_identity_provider_post_broker_login_flow(
        self, connector: KeycloakConnector, api: AdminApi, keycloak_url: str, made_realm: str
    ) -> None:
        await connector.add_identity_provider(
            realm_name=made_realm,
            provider_alias="sso-rijk",
            display_name="SSO Rijk",
            client_id="broker-client",
            client_secret="broker-secret",
            discovery_url=f"{keycloak_url}/realms/master/.well-known/openid-configuration",
        )
        await connector.create_realm_role(made_realm, "project-user")
        await connector.create_post_broker_login_flow_realm_role(
            realm_name=made_realm, flow_alias="post-broker-realm", role_name="project-user"
        )
        await connector.set_identity_provider_post_broker_login_flow(made_realm, "sso-rijk", "post-broker-realm")

        stored = api.idp(made_realm, "sso-rijk")
        assert stored["postBrokerLoginFlowAlias"] == "post-broker-realm"
        assert stored["config"]["clientId"] == "broker-client", "Setting the flow dropped the config"

    async def test_delete_authentication_flow_by_alias(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.ensure_required_otp_browser_flow(made_realm, "required-otp")
        assert any(flow["alias"] == "required-otp" for flow in api.flows(made_realm))

        assert await connector.delete_authentication_flow_by_alias(made_realm, "required-otp") is True
        assert not any(flow["alias"] == "required-otp" for flow in api.flows(made_realm))
        assert await connector.delete_authentication_flow_by_alias(made_realm, "required-otp") is False


# ==================== Roles, groups and users ====================


class TestRolesGroupsUsers:
    async def test_create_realm_role(self, connector: KeycloakConnector, api: AdminApi, made_realm: str) -> None:
        assert await connector.create_realm_role(made_realm, "project-user", "Access to the project") is True

        stored = api.get(f"/admin/realms/{made_realm}/roles/project-user")
        assert stored["description"] == "Access to the project"
        assert stored["composite"] is False
        assert stored["clientRole"] is False

        # Idempotent: an existing role is not an error and is not duplicated.
        assert await connector.create_realm_role(made_realm, "project-user", "Other text") is True
        assert api.get(f"/admin/realms/{made_realm}/roles/project-user")["description"] == "Access to the project"

    async def test_create_client_role(self, connector: KeycloakConnector, api: AdminApi, made_realm: str) -> None:
        await connector.create_oidc_client(
            realm_name=made_realm,
            client_id="gated-app",
            redirect_uris=["https://app.example.invalid/callback"],
        )
        assert await connector.create_client_role(made_realm, "gated-app", "user", "App user") is True

        client_uuid = api.client(made_realm, "gated-app")["id"]
        roles = api.get(f"/admin/realms/{made_realm}/clients/{client_uuid}/roles")
        stored = next(role for role in roles if role["name"] == "user")
        assert stored["description"] == "App user"
        assert stored["clientRole"] is True

        assert await connector.create_client_role(made_realm, "gated-app", "user") is True
        assert len(api.get(f"/admin/realms/{made_realm}/clients/{client_uuid}/roles")) == 1

    async def test_assign_client_role_to_user(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.create_oidc_client(
            realm_name=made_realm,
            client_id="gated-app",
            redirect_uris=["https://app.example.invalid/callback"],
        )
        await connector.create_client_role(made_realm, "gated-app", "user")
        user = await connector.create_user(
            realm_name=made_realm, username="member", password=USER_PASSWORD, skip_email_verification=True
        )

        await connector.assign_client_role_to_user(made_realm, "gated-app", user["id"], "user")

        client_uuid = api.client(made_realm, "gated-app")["id"]
        assert api.client_roles_of_user(made_realm, user["id"], client_uuid) == {"user"}

    async def test_assign_realm_roles_to_user_reports_the_missing_ones(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        await connector.create_realm_role(made_realm, "project-user")
        user = await connector.create_user(
            realm_name=made_realm, username="member", password=USER_PASSWORD, skip_email_verification=True
        )

        result = await connector.assign_realm_roles_to_user(made_realm, user["id"], ["project-user", "does-not-exist"])
        assert result["assigned"] == ["project-user"]
        assert result["not_found"] == ["does-not-exist"]
        assert "project-user" in api.realm_roles_of_user(made_realm, user["id"])

    async def test_remove_all_realm_roles_strips_the_default_role(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        user = await connector.create_user(
            realm_name=made_realm, username="member", password=USER_PASSWORD, skip_email_verification=True
        )
        assert api.realm_roles_of_user(made_realm, user["id"]) == {f"default-roles-{made_realm}"}

        assert await connector.remove_all_realm_roles(made_realm, user["id"]) is True
        assert api.realm_roles_of_user(made_realm, user["id"]) == set()

    async def test_assign_realm_admin_from_master_grants_the_realm_client_roles(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        """A delegated admin in master who may manage exactly one realm."""
        username = f"delegate-{uuid4().hex[:8]}"
        user = await connector.create_user(
            realm_name="master", username=username, password=USER_PASSWORD, skip_email_verification=True
        )
        try:
            assert (
                await connector.assign_realm_admin_from_master(
                    made_realm, user["id"], role_names=["view-realm", "manage-users"]
                )
                is True
            )

            realm_client_uuid = api.client("master", f"{made_realm}-realm")["id"]
            granted = api.client_roles_of_user("master", user["id"], realm_client_uuid)
            assert granted == {"view-realm", "manage-users"}
        finally:
            api.delete(f"/admin/realms/master/users/{user['id']}")

    async def test_create_group_and_join_user(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        group = await connector.create_group(made_realm, "beheerders")
        assert group["name"] == "beheerders"
        assert group["path"] == "/beheerders"

        user = await connector.create_user(
            realm_name=made_realm, username="member", password=USER_PASSWORD, skip_email_verification=True
        )
        assert await connector.join_user_to_group(made_realm, user["id"], "beheerders") is True

        members = api.get(f"/admin/realms/{made_realm}/groups/{group['id']}/members")
        assert [member["username"] for member in members] == ["member"]
        assert await connector.join_user_to_group(made_realm, user["id"], "absent-group") is False

        # Idempotent: a second create neither fails nor duplicates the group.
        await connector.create_group(made_realm, "beheerders")
        assert len(api.get(f"/admin/realms/{made_realm}/groups")) == 1

    async def test_create_user_with_an_email_requires_verification(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        user = await connector.create_user(
            realm_name=made_realm,
            username="member",
            password=USER_PASSWORD,
            email="member@rijksoverheid.nl",
            first_name="Mede",
            last_name="Werker",
        )

        stored = api.user(made_realm, "member")
        assert stored["id"] == user["id"]
        assert stored["email"] == "member@rijksoverheid.nl"
        assert stored["firstName"] == "Mede"
        assert stored["lastName"] == "Werker"
        assert stored["enabled"] is True
        assert stored["emailVerified"] is False
        assert stored["requiredActions"] == ["VERIFY_EMAIL"]
        # The required action is useless while the provider is off, so OPI enables it.
        assert api.required_action(made_realm, "VERIFY_EMAIL")["enabled"] is True
        assert api.credential_types(made_realm, user["id"]) == {"password"}

    async def test_create_user_with_a_totp_secret_imports_an_otp_credential(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        user = await connector.create_user(
            realm_name=made_realm,
            username="otp-member",
            password=USER_PASSWORD,
            email="otp@rijksoverheid.nl",
            totp_secret="JBSWY3DPEHPK3PXP",
            skip_email_verification=True,
        )

        stored = api.user(made_realm, "otp-member")
        assert stored["emailVerified"] is True
        assert "requiredActions" not in stored or stored["requiredActions"] == []
        assert api.credential_types(made_realm, user["id"]) == {"password", "otp"}

    async def test_get_user_by_username_and_email(self, connector: KeycloakConnector, made_realm: str) -> None:
        created = await connector.create_user(
            realm_name=made_realm,
            username="member",
            password=USER_PASSWORD,
            email="member@rijksoverheid.nl",
            skip_email_verification=True,
        )

        by_username = await connector.get_user_by_username(made_realm, "member")
        assert by_username is not None
        assert by_username["id"] == created["id"]
        assert await connector.get_user_by_username(made_realm, "absent") is None

        by_email = await connector.get_user_by_email(made_realm, "member@rijksoverheid.nl")
        assert by_email is not None
        assert by_email["id"] == created["id"]
        assert await connector.get_user_by_email(made_realm, "absent@rijksoverheid.nl") is None

    async def test_delete_user_by_username(self, connector: KeycloakConnector, api: AdminApi, made_realm: str) -> None:
        await connector.create_user(
            realm_name=made_realm, username="member", password=USER_PASSWORD, skip_email_verification=True
        )
        assert await connector.delete_user_by_username(made_realm, "member") is True
        assert api.get(f"/admin/realms/{made_realm}/users", username="member", exact="true") == []
        assert await connector.delete_user_by_username(made_realm, "member") is False

    async def test_ensure_configure_totp_required_only_touches_users_without_otp(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        without_otp = await connector.create_user(
            realm_name=made_realm, username="no-otp", password=USER_PASSWORD, skip_email_verification=True
        )
        with_otp = await connector.create_user(
            realm_name=made_realm,
            username="has-otp",
            password=USER_PASSWORD,
            totp_secret="JBSWY3DPEHPK3PXP",
            skip_email_verification=True,
        )
        spared = await connector.create_user(
            realm_name=made_realm, username="spared", password=USER_PASSWORD, skip_email_verification=True
        )

        changed = await connector.ensure_configure_totp_required(made_realm, skip_usernames=frozenset({"spared"}))
        assert sorted(changed) == ["no-otp"]

        assert api.get(f"/admin/realms/{made_realm}/users/{without_otp['id']}")["requiredActions"] == ["CONFIGURE_TOTP"]
        assert not api.get(f"/admin/realms/{made_realm}/users/{with_otp['id']}").get("requiredActions")
        assert not api.get(f"/admin/realms/{made_realm}/users/{spared['id']}").get("requiredActions")
        assert api.required_action(made_realm, "CONFIGURE_TOTP")["enabled"] is True

        # Idempotent: a second run finds nothing left to do.
        assert await connector.ensure_configure_totp_required(made_realm, skip_usernames=frozenset({"spared"})) == []

    async def test_send_verify_email_arrives_at_the_relay_sink(
        self, connector: KeycloakConnector, api: AdminApi, made_realm: str
    ) -> None:
        """The mail goes out through our own emailSender provider, to the relay sink.

        Skips without a sink in the environment, like the other mail measurement.
        """
        sink = omgeving("KEYCLOAK_IMAGE_TEST_RELAY_SINK_URL").rstrip("/")
        lokaas = omgeving("KEYCLOAK_IMAGE_TEST_LOKAAS_SINK_SMTP")
        host, _, port = lokaas.rpartition(":")
        # Keycloak refuses to send from a realm without an smtpServer; our provider does
        # not read it, which is what test_keycloak_image_mail.py measures.
        await connector.update_realm_settings(
            made_realm,
            {
                "smtpServer": {
                    "host": host,
                    "port": port,
                    "from": "omgeleid@toets.invalid",
                    "auth": "false",
                    "ssl": "false",
                    "starttls": "false",
                }
            },
        )
        httpx.delete(f"{sink}/api/v1/messages", timeout=TIMEOUT)

        user = await connector.create_user(
            realm_name=made_realm,
            username="member",
            password=USER_PASSWORD,
            email="member@rijksoverheid.nl",
        )
        await connector.send_verify_email(made_realm, user["id"])

        deadline = time.monotonic() + 60.0
        while True:
            messages = httpx.get(f"{sink}/api/v1/messages", timeout=TIMEOUT).json()["messages"]
            if messages:
                break
            assert time.monotonic() < deadline, "No verification mail at the relay sink within 60 seconds"
            time.sleep(0.5)
        assert any("member@rijksoverheid.nl" in str(message) for message in messages)


# ==================== Two real flows ====================


class TestBootstrapFlow:
    async def test_ensure_master_service_account_client_repairs_the_client(
        self, connector: KeycloakConnector, api: AdminApi
    ) -> None:
        """The client the fixture already created, written again with the same arguments."""
        await connector.ensure_master_service_account_client(
            client_id="opi-admin-service", client_secret=SERVICE_ACCOUNT_SECRET
        )

        stored = api.client("master", "opi-admin-service")
        assert stored["serviceAccountsEnabled"] is True
        assert stored["publicClient"] is False
        assert stored["standardFlowEnabled"] is False
        assert stored["directAccessGrantsEnabled"] is False

        service_account = api.get(f"/admin/realms/master/clients/{stored['id']}/service-account-user")
        assert "admin" in api.realm_roles_of_user("master", service_account["id"])

        # The connector still works on the rewritten client, so the secret survived.
        assert await connector.connection_works() is True

    async def test_setup_all_builds_the_platform_realms_and_is_idempotent(
        self, connector: KeycloakConnector, api: AdminApi, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OPI's whole Keycloak bootstrap, twice, with only kubectl faked.

        Everything Keycloak is real; the one non-Keycloak step is the write of the
        operations-manager-keycloak secret, which needs a cluster.
        """
        applied: list[str] = []

        class FakeKubectl:
            async def apply_manifest(self, path: str) -> None:
                applied.append(path)

        monkeypatch.setattr("opi.bootstrap.keycloak_setup.KubectlConnector", FakeKubectl)
        monkeypatch.setattr(settings, "KEYCLOAK_BOOTSTRAP_CONFIG", "local")
        monkeypatch.setattr(settings, "KEYCLOAK_DEFAULT_REALM", "rig-platform")
        monkeypatch.setattr(settings, "KEYCLOAK_ENFORCE_MASTER_OTP", False)
        monkeypatch.setattr(settings, "OWN_DOMAIN", "zad.example.invalid")
        monkeypatch.setattr(settings, "ADDITIONAL_DOMAINS", "")

        try:
            assert await KeycloakSetup().setup_all() is True
            assert applied, "The operations secret was never written"

            platform = api.realm("rig-platform")
            assert platform["loginTheme"] == "nl-design-system"
            operations = api.realm(OPERATIONS_REALM_NAME)
            assert operations["realm"] == OPERATIONS_REALM_NAME

            opi_client = api.client(OPERATIONS_REALM_NAME, "rig-platform-operations-manager")
            assert "https://zad.example.invalid/*" in opi_client["redirectUris"]
            assert opi_client["attributes"]["pkce.code.challenge.method"] == "S256"

            for realm_name in ("rig-platform", OPERATIONS_REALM_NAME):
                assert_realm_consistent(api, realm_name, step="first bootstrap")

            first = {
                "platform_flows": sorted(flow["alias"] for flow in api.flows("rig-platform")),
                "operations_flows": sorted(flow["alias"] for flow in api.flows(OPERATIONS_REALM_NAME)),
                "operations_clients": sorted(c["clientId"] for c in api.clients(OPERATIONS_REALM_NAME)),
                "operations_idps": sorted(
                    idp["alias"]
                    for idp in api.get(f"/admin/realms/{OPERATIONS_REALM_NAME}/identity-provider/instances")
                ),
            }

            assert await KeycloakSetup().setup_all() is True
            second = {
                "platform_flows": sorted(flow["alias"] for flow in api.flows("rig-platform")),
                "operations_flows": sorted(flow["alias"] for flow in api.flows(OPERATIONS_REALM_NAME)),
                "operations_clients": sorted(c["clientId"] for c in api.clients(OPERATIONS_REALM_NAME)),
                "operations_idps": sorted(
                    idp["alias"]
                    for idp in api.get(f"/admin/realms/{OPERATIONS_REALM_NAME}/identity-provider/instances")
                ),
            }
            assert second == first
            for realm_name in ("rig-platform", OPERATIONS_REALM_NAME):
                assert_realm_consistent(api, realm_name, step="second bootstrap")
        finally:
            api.drop_realm(OPERATIONS_REALM_NAME)
            api.drop_realm("rig-platform")
