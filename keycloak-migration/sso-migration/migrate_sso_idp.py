#!/usr/bin/env python3
"""
SSO-Rijk IDP Migration Script

Migrates the sso-rijk IDP while preserving user federated identities.

Supports two IDP types:
- SAML: For direct SSO-Rijk connection (production)
- OIDC: For Keycloak-based SSO (like digilab, for testing)

Steps:
1. Add new IDP (SAML or OIDC) with temporary alias
2. Add appropriate mappers (SAML or OIDC)
3. Database swap: delete old IDP, rename new IDP to original alias

Usage:
    # Step 1: Add new IDP
    python migrate_sso_idp.py add-idp --type oidc --config config.json

    # Step 2: Add mappers
    python migrate_sso_idp.py add-mappers --idp-alias sso-rijk-new

    # Step 3: Swap IDPs
    python migrate_sso_idp.py swap --old-alias sso-rijk --new-alias sso-rijk-new

Prerequisites:
    pip install requests psycopg2-binary
"""

import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests

# Suppress SSL warnings for local testing
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# =============================================================================
# MAPPER CONFIGURATIONS
# =============================================================================

# SAML mappers for direct SSO-Rijk connection
SAML_MAPPERS = [
    {
        "name": "sso-rijk-userid",
        "identityProviderMapper": "saml-unrestricted-xpath-idp-mapper",
        "config": {
            "syncMode": "FORCE",
            "xpath.expression": "//*[local-name()='Subject']/*[local-name()='NameID']/text()",
            "user.attribute": "sso-rijk-userid",
        },
    },
    {
        "name": "sso-rijk-userid-lowercase",
        "identityProviderMapper": "saml-unrestricted-xpath-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "xpath.expression": "//*[local-name()='Subject']/*[local-name()='NameID']/text()",
            "user.attribute": "sso-rijk-userid-lowercase",
            "value.transformation": "LOWERCASE",
        },
    },
    {
        "name": "email",
        "identityProviderMapper": "saml-user-attribute-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "attribute.name": "urn:rijksoverheid:federation:emailAddress",
            "attribute.name.format": "ATTRIBUTE_FORMAT_BASIC",
            "user.attribute": "email",
        },
    },
    {
        "name": "firstName",
        "identityProviderMapper": "saml-user-attribute-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "attribute.name": "urn:rijksoverheid:federation:givenName",
            "attribute.name.format": "ATTRIBUTE_FORMAT_BASIC",
            "user.attribute": "firstName",
        },
    },
    {
        "name": "lastName",
        "identityProviderMapper": "saml-user-attribute-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "attribute.name": "urn:rijksoverheid:federation:surName",
            "attribute.name.format": "ATTRIBUTE_FORMAT_BASIC",
            "user.attribute": "lastName",
        },
    },
    {
        "name": "organizationName",
        "identityProviderMapper": "saml-user-attribute-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "attribute.name": "urn:rijksoverheid:federation:organizationDisplayName",
            "attribute.name.format": "ATTRIBUTE_FORMAT_BASIC",
            "user.attribute": "organization.name",
        },
    },
    {
        "name": "organizationNumber",
        "identityProviderMapper": "saml-user-attribute-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "attribute.name": "urn:rijksoverheid:federation:organizationNumber",
            "attribute.name.format": "ATTRIBUTE_FORMAT_BASIC",
            "user.attribute": "organization.number",
        },
    },
]

# OIDC mappers for Keycloak-based SSO (like digilab)
OIDC_MAPPERS = [
    {
        "name": "sso-rijk-userid-mapper",
        "identityProviderMapper": "oidc-user-attribute-idp-mapper",
        "config": {
            "syncMode": "FORCE",
            "claim": "sub",
            "user.attribute": "sso-rijk-userid",
        },
    },
    {
        "name": "sso-rijk-userid-lowercase-mapper",
        "identityProviderMapper": "oidc-user-attribute-idp-mapper",
        "config": {
            "syncMode": "FORCE",
            "claim": "preferred_username",
            "user.attribute": "sso-rijk-userid-lowercase",
        },
    },
    {
        "name": "email-to-username",
        "identityProviderMapper": "oidc-username-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "template": "${CLAIM.email}",
        },
    },
    {
        "name": "email-mapper",
        "identityProviderMapper": "oidc-user-attribute-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "claim": "email",
            "user.attribute": "email",
        },
    },
    {
        "name": "first-name-mapper",
        "identityProviderMapper": "oidc-user-attribute-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "claim": "given_name",
            "user.attribute": "firstName",
        },
    },
    {
        "name": "last-name-mapper",
        "identityProviderMapper": "oidc-user-attribute-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "claim": "family_name",
            "user.attribute": "lastName",
        },
    },
    {
        "name": "organization-number-mapper",
        "identityProviderMapper": "oidc-user-attribute-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "claim": "organization.number",
            "user.attribute": "organization.number",
        },
    },
    {
        "name": "organization-name-mapper",
        "identityProviderMapper": "oidc-user-attribute-idp-mapper",
        "config": {
            "syncMode": "INHERIT",
            "claim": "organization.name",
            "user.attribute": "organization.name",
        },
    },
]


# =============================================================================
# KEYCLOAK CLIENT
# =============================================================================


class KeycloakClient:
    """Client for Keycloak Admin API."""

    def __init__(
        self,
        base_url: str,
        admin_user: str,
        admin_password: str,
        verify_ssl: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.verify_ssl = verify_ssl
        self._token: str | None = None

    def _get_token(self) -> str:
        """Get admin access token."""
        if self._token:
            return self._token

        response = requests.post(
            f"{self.base_url}/realms/master/protocol/openid-connect/token",
            data={
                "username": self.admin_user,
                "password": self.admin_password,
                "grant_type": "password",
                "client_id": "admin-cli",
            },
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        self._token = response.json()["access_token"]
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def get_idp(self, realm: str, alias: str) -> dict | None:
        """Get IDP configuration by alias."""
        response = requests.get(
            f"{self.base_url}/admin/realms/{realm}/identity-provider/instances/{alias}",
            headers=self._headers(),
            verify=self.verify_ssl,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def create_oidc_idp(
        self,
        realm: str,
        alias: str,
        display_name: str,
        discovery_url: str,
        client_id: str,
        client_secret: str,
    ) -> dict:
        """Create an OIDC Identity Provider."""
        idp_config = {
            "alias": alias,
            "displayName": display_name,
            "providerId": "oidc",
            "enabled": True,
            "trustEmail": True,
            "storeToken": False,
            "addReadTokenRoleOnCreate": False,
            "authenticateByDefault": False,
            "linkOnly": False,
            "firstBrokerLoginFlowAlias": "first broker login",
            "config": {
                "clientId": client_id,
                "clientSecret": client_secret,
                "tokenUrl": "",  # Will be discovered
                "authorizationUrl": "",  # Will be discovered
                "useJwksUrl": "true",
                "syncMode": "FORCE",
                "clientAuthMethod": "client_secret_post",
                "validateSignature": "true",
                "pkceEnabled": "false",
            },
        }

        # First, try to import from discovery URL
        logger.info(f"Discovering OIDC configuration from {discovery_url}")
        try:
            disc_response = requests.get(discovery_url, verify=self.verify_ssl)
            disc_response.raise_for_status()
            disc_data = disc_response.json()

            idp_config["config"]["authorizationUrl"] = disc_data.get(
                "authorization_endpoint", ""
            )
            idp_config["config"]["tokenUrl"] = disc_data.get("token_endpoint", "")
            idp_config["config"]["userInfoUrl"] = disc_data.get("userinfo_endpoint", "")
            idp_config["config"]["logoutUrl"] = disc_data.get(
                "end_session_endpoint", ""
            )
            idp_config["config"]["jwksUrl"] = disc_data.get("jwks_uri", "")
            idp_config["config"]["issuer"] = disc_data.get("issuer", "")
            logger.info("OIDC discovery successful")
        except Exception as e:
            logger.warning(
                f"OIDC discovery failed: {e}. Manual configuration may be needed."
            )

        response = requests.post(
            f"{self.base_url}/admin/realms/{realm}/identity-provider/instances",
            headers=self._headers(),
            json=idp_config,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        logger.info(f"Created OIDC IDP: {alias}")
        return self.get_idp(realm, alias)

    def create_saml_idp(
        self,
        realm: str,
        alias: str,
        display_name: str,
        entity_id: str,
        sso_url: str,
        logout_url: str | None = None,
        signing_certificate: str | None = None,
    ) -> dict:
        """Create a SAML Identity Provider."""
        idp_config = {
            "alias": alias,
            "displayName": display_name,
            "providerId": "saml",
            "enabled": True,
            "trustEmail": True,
            "storeToken": False,
            "addReadTokenRoleOnCreate": False,
            "authenticateByDefault": False,
            "linkOnly": False,
            "firstBrokerLoginFlowAlias": "first broker login",
            "config": {
                "entityId": entity_id,
                "idpEntityId": entity_id,
                "singleSignOnServiceUrl": sso_url,
                "singleLogoutServiceUrl": logout_url or "",
                "nameIDPolicyFormat": "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
                "principalType": "SUBJECT",
                "syncMode": "FORCE",
                "wantAuthnRequestsSigned": "false",
                "wantAssertionsSigned": "false",
                "wantAssertionsEncrypted": "false",
                "validateSignature": "false" if not signing_certificate else "true",
                "postBindingAuthnRequest": "false",
                "postBindingResponse": "false",
                "postBindingLogout": "false",
            },
        }

        if signing_certificate:
            idp_config["config"]["signingCertificate"] = signing_certificate

        response = requests.post(
            f"{self.base_url}/admin/realms/{realm}/identity-provider/instances",
            headers=self._headers(),
            json=idp_config,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        logger.info(f"Created SAML IDP: {alias}")
        return self.get_idp(realm, alias)

    def get_idp_mappers(self, realm: str, alias: str) -> list[dict]:
        """Get IDP mappers."""
        response = requests.get(
            f"{self.base_url}/admin/realms/{realm}/identity-provider/instances/{alias}/mappers",
            headers=self._headers(),
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        return response.json()

    def create_idp_mapper(self, realm: str, alias: str, mapper: dict) -> dict:
        """Create an IDP mapper."""
        mapper_config = {
            "name": mapper["name"],
            "identityProviderAlias": alias,
            "identityProviderMapper": mapper["identityProviderMapper"],
            "config": mapper["config"],
        }

        response = requests.post(
            f"{self.base_url}/admin/realms/{realm}/identity-provider/instances/{alias}/mappers",
            headers=self._headers(),
            json=mapper_config,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        logger.info(f"Created mapper: {mapper['name']}")
        return response.json() if response.text else {}

    def delete_idp(self, realm: str, alias: str) -> None:
        """Delete an IDP."""
        response = requests.delete(
            f"{self.base_url}/admin/realms/{realm}/identity-provider/instances/{alias}",
            headers=self._headers(),
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        logger.info(f"Deleted IDP: {alias}")

    def get_users_count(self, realm: str) -> int:
        """Get total user count."""
        response = requests.get(
            f"{self.base_url}/admin/realms/{realm}/users/count",
            headers=self._headers(),
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        return response.json()

    def get_federated_identity_count(self, realm: str, idp_alias: str) -> int:
        """Count users with federated identity for an IDP."""
        # This is an approximation - we'd need to iterate all users
        # For now, return -1 to indicate we need database query
        return -1


# =============================================================================
# DATABASE MIGRATOR
# =============================================================================


class DatabaseMigrator:
    """Handles database-level IDP migration."""

    def __init__(
        self, db_host: str, db_port: int, db_name: str, db_user: str, db_password: str
    ):
        self.db_host = db_host
        self.db_port = db_port
        self.db_name = db_name
        self.db_user = db_user
        self.db_password = db_password
        try:
            import psycopg2

            self.psycopg2 = psycopg2
        except ImportError:
            logger.error("psycopg2 not installed. Run: pip install psycopg2-binary")
            raise

    def _connect(self):
        """Create database connection."""
        return self.psycopg2.connect(
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            user=self.db_user,
            password=self.db_password,
        )

    def backup_database(self, backup_dir: str = "./backups") -> str:
        """Create a pg_dump backup of the database."""
        backup_path = Path(backup_dir)
        backup_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = backup_path / f"keycloak_backup_{timestamp}.sql"

        logger.info(f"Creating database backup: {backup_file}")

        import os

        env = os.environ.copy()
        env["PGPASSWORD"] = self.db_password

        cmd = [
            "pg_dump",
            "-h",
            self.db_host,
            "-p",
            str(self.db_port),
            "-U",
            self.db_user,
            "-d",
            self.db_name,
            "-f",
            str(backup_file),
        ]

        try:
            subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
            logger.info(f"Backup created successfully: {backup_file}")
            return str(backup_file)
        except subprocess.CalledProcessError as e:
            logger.error(f"Backup failed: {e.stderr}")
            raise
        except FileNotFoundError:
            logger.warning("pg_dump not found locally, trying kubectl exec...")
            return self._backup_via_kubectl(backup_file)

    def _backup_via_kubectl(self, backup_file: Path) -> str:
        """Backup database via kubectl exec into postgres pod."""
        # Find the postgres pod
        cmd = [
            "kubectl",
            "get",
            "pods",
            "-n",
            "rig-system",
            "-l",
            "cnpg.io/cluster=rig-db",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Could not find postgres pod: {result.stderr}")

        pod_name = result.stdout.strip()
        logger.info(f"Found postgres pod: {pod_name}")

        # Execute pg_dump in the pod
        cmd = [
            "kubectl",
            "exec",
            "-n",
            "rig-system",
            pod_name,
            "--",
            "pg_dump",
            "-U",
            self.db_user,
            "-d",
            self.db_name,
        ]

        with open(backup_file, "w") as f:
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {result.stderr}")

        logger.info(f"Backup created via kubectl: {backup_file}")
        return str(backup_file)

    def get_realm_id(self, realm_name: str) -> str:
        """Get the internal realm ID."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM realm WHERE name = %s", (realm_name,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Realm not found: {realm_name}")
                return row[0]

    def get_idp_info(self, realm_id: str, alias: str) -> dict | None:
        """Get IDP information from database."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT internal_id, provider_alias, provider_id, enabled
                    FROM identity_provider
                    WHERE realm_id = %s AND provider_alias = %s
                    """,
                    (realm_id, alias),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "internal_id": row[0],
                    "provider_alias": row[1],
                    "provider_id": row[2],
                    "enabled": row[3],
                }

    def get_federated_identities_count(self, realm_id: str, idp_alias: str) -> int:
        """Count federated identities for an IDP."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM federated_identity
                    WHERE realm_id = %s AND identity_provider = %s
                    """,
                    (realm_id, idp_alias),
                )
                return cur.fetchone()[0]

    def get_federated_identities_sample(
        self, realm_id: str, idp_alias: str, limit: int = 5
    ) -> list[dict]:
        """Get sample of federated identities."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT fi.user_id, fi.federated_user_id, fi.federated_username, ue.username
                    FROM federated_identity fi
                    LEFT JOIN user_entity ue ON fi.user_id = ue.id
                    WHERE fi.realm_id = %s AND fi.identity_provider = %s
                    LIMIT %s
                    """,
                    (realm_id, idp_alias, limit),
                )
                return [
                    {
                        "user_id": row[0],
                        "federated_user_id": row[1],
                        "federated_username": row[2],
                        "keycloak_username": row[3],
                    }
                    for row in cur.fetchall()
                ]

    def swap_idps(
        self, realm_id: str, old_alias: str, new_alias: str, dry_run: bool = False
    ) -> None:
        """
        Swap IDPs at database level (safe version - preserves old IDP).

        1. Rename old IDP to {old_alias}-obsolete (keep as fallback)
        2. Update old IDP mappers to use new alias
        3. Disable the old IDP
        4. Rename new IDP to take over old alias
        5. Update new IDP mappers to use old alias

        Note: federated_identity entries are NOT modified - they reference the alias
        which will be taken over by the renamed new IDP.
        """
        obsolete_alias = f"{old_alias}-obsolete"

        old_idp = self.get_idp_info(realm_id, old_alias)
        new_idp = self.get_idp_info(realm_id, new_alias)

        if not old_idp:
            raise ValueError(f"Old IDP not found: {old_alias}")
        if not new_idp:
            raise ValueError(f"New IDP not found: {new_alias}")

        # Check if obsolete alias already exists
        obsolete_idp = self.get_idp_info(realm_id, obsolete_alias)
        if obsolete_idp:
            raise ValueError(
                f"Obsolete IDP already exists: {obsolete_alias}. Delete it first or use a different name."
            )

        fed_count = self.get_federated_identities_count(realm_id, old_alias)

        logger.info("=" * 60)
        logger.info("IDP SWAP SUMMARY (SAFE MODE)")
        logger.info("=" * 60)
        logger.info(
            f"Old IDP: {old_alias} -> {obsolete_alias} (will be disabled, kept as fallback)"
        )
        logger.info(
            f"  Provider: {old_idp['provider_id']}, ID: {old_idp['internal_id']}"
        )
        logger.info(f"New IDP: {new_alias} -> {old_alias} (will take over)")
        logger.info(
            f"  Provider: {new_idp['provider_id']}, ID: {new_idp['internal_id']}"
        )
        logger.info(f"Federated identities to preserve: {fed_count}")
        logger.info("=" * 60)

        if dry_run:
            logger.info("[DRY RUN] Would execute:")
            logger.info(
                f"  1. UPDATE identity_provider SET provider_alias = '{obsolete_alias}', "
                f"provider_display_name = 'SSO Rijk (OBSOLETE)', enabled = false "
                f"WHERE internal_id = '{old_idp['internal_id']}'"
            )
            logger.info(
                f"  2. UPDATE identity_provider_mapper SET idp_alias = '{obsolete_alias}' "
                f"WHERE idp_alias = '{old_alias}'"
            )
            logger.info(
                f"  3. UPDATE identity_provider SET provider_alias = '{old_alias}', "
                f"provider_display_name = 'SSO Rijk', enabled = true, authenticate_by_default = true "
                f"WHERE internal_id = '{new_idp['internal_id']}'"
            )
            logger.info(
                f"  4. UPDATE identity_provider_mapper SET idp_alias = '{old_alias}' "
                f"WHERE idp_alias = '{new_alias}'"
            )
            return

        with self._connect() as conn:
            with conn.cursor() as cur:
                logger.info(
                    f"Step 1: Renaming '{old_alias}' to '{obsolete_alias}' and disabling..."
                )
                cur.execute(
                    """UPDATE identity_provider
                       SET provider_alias = %s, provider_display_name = 'SSO Rijk (OBSOLETE)', enabled = false
                       WHERE internal_id = %s""",
                    (obsolete_alias, old_idp["internal_id"]),
                )
                logger.info(f"  Updated {cur.rowcount} IDP")

                logger.info(
                    f"Step 2: Updating old IDP mapper references to '{obsolete_alias}'..."
                )
                cur.execute(
                    "UPDATE identity_provider_mapper SET idp_alias = %s WHERE idp_alias = %s",
                    (obsolete_alias, old_alias),
                )
                logger.info(f"  Updated {cur.rowcount} mappers")

                logger.info(
                    f"Step 3: Renaming '{new_alias}' to '{old_alias}' and enabling as default..."
                )
                cur.execute(
                    """UPDATE identity_provider
                       SET provider_alias = %s, provider_display_name = 'SSO Rijk',
                           enabled = true, authenticate_by_default = true
                       WHERE internal_id = %s""",
                    (old_alias, new_idp["internal_id"]),
                )
                logger.info(f"  Updated {cur.rowcount} IDP")

                logger.info(
                    f"Step 4: Updating new IDP mapper references to '{old_alias}'..."
                )
                cur.execute(
                    "UPDATE identity_provider_mapper SET idp_alias = %s WHERE idp_alias = %s",
                    (old_alias, new_alias),
                )
                logger.info(f"  Updated {cur.rowcount} mappers")

                conn.commit()
                logger.info("IDP swap completed successfully!")
                logger.info(
                    f"Old IDP is now '{obsolete_alias}' (disabled) - can be deleted manually later"
                )


# =============================================================================
# BOOTSTRAP YAML UPDATER
# =============================================================================


def update_bootstrap_yaml(
    yaml_path: str,
    old_alias: str,
    new_alias: str,
    dry_run: bool = False,
) -> bool:
    """
    Update bootstrap.yaml after IDP swap to reflect the new configuration.

    1. Remove the old OIDC IDP entry (now obsolete)
    2. Rename the SAML IDP from new_alias to old_alias
    3. Enable the SAML IDP

    Args:
        yaml_path: Path to bootstrap.yaml
        old_alias: The alias that was swapped (e.g., 'sso-rijk')
        new_alias: The alias that took over (e.g., 'sso-rijk-direct')
        dry_run: If True, only show what would be changed

    Returns:
        True if update was successful
    """
    try:
        from ruamel.yaml import YAML
    except ImportError:
        logger.error("ruamel.yaml not installed. Run: pip install ruamel.yaml")
        return False

    yaml = YAML()
    yaml.preserve_quotes = True

    path = Path(yaml_path)
    if not path.exists():
        logger.error(f"Bootstrap YAML not found: {yaml_path}")
        return False

    # Read the YAML
    with path.open("r") as f:
        config = yaml.load(f)

    if not config or "identityProviders" not in config:
        logger.error("Invalid bootstrap YAML: missing identityProviders section")
        return False

    idps = config["identityProviders"]
    old_idp_index = None
    new_idp_index = None

    # Find the IDPs
    for i, idp in enumerate(idps):
        if idp.get("alias") == old_alias:
            old_idp_index = i
        elif idp.get("alias") == new_alias:
            new_idp_index = i

    if old_idp_index is None:
        logger.warning(
            f"Old IDP '{old_alias}' not found in bootstrap.yaml (may already be removed)"
        )
    if new_idp_index is None:
        logger.error(f"New IDP '{new_alias}' not found in bootstrap.yaml")
        return False

    if dry_run:
        logger.info("[DRY RUN] Would update bootstrap.yaml:")
        if old_idp_index is not None:
            logger.info(f"  1. Remove OIDC IDP entry: '{old_alias}'")
        logger.info(f"  2. Rename SAML IDP: '{new_alias}' -> '{old_alias}'")
        logger.info("  3. Set enabled: true, authenticateByDefault: true")
        logger.info("  4. Update displayName: 'SSO Rijk'")
        return True

    # Update the new IDP (SAML) to take over the old alias
    new_idp = idps[new_idp_index]
    new_idp["alias"] = old_alias
    new_idp["displayName"] = "SSO Rijk"
    new_idp["enabled"] = True
    new_idp["authenticateByDefault"] = True

    # Remove comment about being disabled (update the description in the YAML)
    # The comment handling is tricky with ruamel.yaml, so we'll leave comments as-is

    # Remove the old IDP entry (OIDC)
    if old_idp_index is not None:
        # Adjust index if we're removing an item before new_idp_index
        del idps[old_idp_index]
        logger.info(f"Removed old OIDC IDP entry: '{old_alias}'")

    # Write the updated YAML
    # Create backup first
    backup_path = path.with_suffix(".yaml.bak")
    import shutil

    shutil.copy(path, backup_path)
    logger.info(f"Created backup: {backup_path}")

    with path.open("w") as f:
        yaml.dump(config, f)

    logger.info("Updated bootstrap.yaml:")
    logger.info("  - Removed old OIDC IDP entry")
    logger.info(f"  - Renamed '{new_alias}' to '{old_alias}'")
    logger.info("  - Enabled IDP with authenticateByDefault: true")

    return True


# =============================================================================
# COMMANDS
# =============================================================================


def cmd_add_idp(args):
    """Add a new IDP."""
    kc = KeycloakClient(
        args.keycloak_url,
        args.admin_user,
        args.admin_password,
        verify_ssl=not args.insecure,
    )

    # Check if IDP already exists
    existing = kc.get_idp(args.realm, args.alias)
    if existing:
        logger.error(
            f"IDP '{args.alias}' already exists. Delete it first or use a different alias."
        )
        sys.exit(1)

    if args.type == "oidc":
        if not args.discovery_url or not args.client_id or not args.client_secret:
            logger.error(
                "OIDC IDP requires --discovery-url, --client-id, and --client-secret"
            )
            sys.exit(1)

        kc.create_oidc_idp(
            realm=args.realm,
            alias=args.alias,
            display_name=args.display_name or args.alias,
            discovery_url=args.discovery_url,
            client_id=args.client_id,
            client_secret=args.client_secret,
        )
    elif args.type == "saml":
        if not args.entity_id or not args.sso_url:
            logger.error("SAML IDP requires --entity-id and --sso-url")
            sys.exit(1)

        kc.create_saml_idp(
            realm=args.realm,
            alias=args.alias,
            display_name=args.display_name or args.alias,
            entity_id=args.entity_id,
            sso_url=args.sso_url,
            logout_url=args.logout_url,
            signing_certificate=args.signing_cert,
        )

    logger.info(f"\nIDP '{args.alias}' created successfully!")
    logger.info(
        f"Next step: python {sys.argv[0]} add-mappers --idp-alias {args.alias} --type {args.type}"
    )


def cmd_add_mappers(args):
    """Add mappers to an IDP."""
    kc = KeycloakClient(
        args.keycloak_url,
        args.admin_user,
        args.admin_password,
        verify_ssl=not args.insecure,
    )

    # Check IDP exists
    idp = kc.get_idp(args.realm, args.idp_alias)
    if not idp:
        logger.error(f"IDP '{args.idp_alias}' not found")
        sys.exit(1)

    # Determine mapper type from IDP or argument
    idp_type = args.type or idp.get("providerId")
    if idp_type == "oidc":
        mappers = OIDC_MAPPERS
    elif idp_type == "saml":
        mappers = SAML_MAPPERS
    else:
        logger.error(
            f"Unknown IDP type: {idp_type}. Specify --type oidc or --type saml"
        )
        sys.exit(1)

    logger.info(
        f"Adding {len(mappers)} {idp_type.upper()} mappers to '{args.idp_alias}'..."
    )

    # Check existing mappers
    existing = kc.get_idp_mappers(args.realm, args.idp_alias)
    existing_names = {m.get("name") for m in existing}

    for mapper in mappers:
        if mapper["name"] in existing_names:
            logger.info(f"  Skipping '{mapper['name']}' (already exists)")
            continue
        kc.create_idp_mapper(args.realm, args.idp_alias, mapper)

    logger.info("\nMappers added successfully!")
    logger.info(f"Next step: Test login via '{args.idp_alias}', then run swap command")


def cmd_swap(args):
    """Swap IDPs at database level."""
    # Initialize clients
    kc = KeycloakClient(
        args.keycloak_url,
        args.admin_user,
        args.admin_password,
        verify_ssl=not args.insecure,
    )
    db = DatabaseMigrator(
        args.db_host, args.db_port, args.db_name, args.db_user, args.db_password
    )

    # Verify both IDPs exist
    old_idp = kc.get_idp(args.realm, args.old_alias)
    new_idp = kc.get_idp(args.realm, args.new_alias)

    if not old_idp:
        logger.error(f"Old IDP '{args.old_alias}' not found")
        sys.exit(1)
    if not new_idp:
        logger.error(f"New IDP '{args.new_alias}' not found")
        sys.exit(1)

    realm_id = db.get_realm_id(args.realm)

    # Show preview
    fed_count = db.get_federated_identities_count(realm_id, args.old_alias)
    sample = db.get_federated_identities_sample(realm_id, args.old_alias, 3)

    logger.info("\n" + "=" * 60)
    logger.info("MIGRATION PREVIEW")
    logger.info("=" * 60)
    logger.info(f"Old IDP: {args.old_alias} ({old_idp.get('providerId')})")
    logger.info(f"New IDP: {args.new_alias} ({new_idp.get('providerId')})")
    logger.info(f"Realm: {args.realm}")
    logger.info(f"Federated identities to preserve: {fed_count}")
    if sample:
        logger.info("\nSample federated identities:")
        for s in sample:
            logger.info(
                f"  - {s['keycloak_username']}: {s['federated_user_id'][:60]}..."
            )

    # Create backup
    if not args.skip_backup and not args.dry_run:
        logger.info("\nCreating database backup...")
        try:
            backup_file = db.backup_database(args.backup_dir)
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            if not args.force:
                logger.error("Use --force to proceed without backup (not recommended)")
                sys.exit(1)
            logger.warning("Proceeding without backup...")
            backup_file = None

    # Confirm
    if not args.dry_run and not args.yes:
        logger.warning("\n" + "!" * 60)
        logger.warning("THIS WILL MODIFY THE KEYCLOAK DATABASE!")
        logger.warning("!" * 60)
        confirm = input("\nType 'yes' to proceed: ")
        if confirm.lower() != "yes":
            logger.info("Migration cancelled.")
            return

    # Execute swap
    db.swap_idps(realm_id, args.old_alias, args.new_alias, dry_run=args.dry_run)

    # Update bootstrap.yaml if path provided
    if args.bootstrap_yaml:
        logger.info("\n" + "-" * 60)
        logger.info("Updating bootstrap.yaml...")
        logger.info("-" * 60)
        update_bootstrap_yaml(
            yaml_path=args.bootstrap_yaml,
            old_alias=args.old_alias,
            new_alias=args.new_alias,
            dry_run=args.dry_run,
        )

    if not args.dry_run:
        logger.info("\n" + "=" * 60)
        logger.info("MIGRATION COMPLETE!")
        logger.info("=" * 60)
        logger.info("\nNext steps:")
        logger.info("1. Restart Keycloak to clear caches:")
        logger.info("   kubectl rollout restart deployment/keycloak -n rig-system")
        logger.info("2. Test login with an existing user")
        logger.info("3. Verify user attributes are populated correctly")
        if args.bootstrap_yaml:
            logger.info("4. Commit the updated bootstrap.yaml to git")
        if not args.skip_backup:
            logger.info(f"5. If issues, restore from backup: {backup_file}")


def cmd_status(args):
    """Show current IDP status."""
    kc = KeycloakClient(
        args.keycloak_url,
        args.admin_user,
        args.admin_password,
        verify_ssl=not args.insecure,
    )

    logger.info(f"\nChecking IDPs in realm '{args.realm}'...")

    for alias in [args.old_alias, args.new_alias]:
        idp = kc.get_idp(args.realm, alias)
        if idp:
            mappers = kc.get_idp_mappers(args.realm, alias)
            logger.info(f"\n{alias}:")
            logger.info(f"  Provider: {idp.get('providerId')}")
            logger.info(f"  Enabled: {idp.get('enabled')}")
            logger.info(f"  Mappers: {len(mappers)}")
            for m in mappers:
                logger.info(
                    f"    - {m.get('name')} ({m.get('identityProviderMapper')})"
                )
        else:
            logger.info(f"\n{alias}: NOT FOUND")


# =============================================================================
# MAIN
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="SSO-Rijk IDP Migration Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check status
  python migrate_sso_idp.py status --keycloak-url https://keycloak.kind --admin-password admin

  # Add OIDC IDP (for testing with digilab)
  python migrate_sso_idp.py add-idp --type oidc \\
    --alias sso-rijk-new \\
    --discovery-url https://keycloak.apps.digilab.network/realms/algoritmes/.well-known/openid-configuration \\
    --client-id opi --client-secret xxx

  # Add SAML IDP (for production SSO-Rijk)
  python migrate_sso_idp.py add-idp --type saml \\
    --alias sso-rijk-new \\
    --entity-id https://engine.spbroker-prd.bzk.sson.overheid-i.nl/authentication/idp/metadata \\
    --sso-url https://engine.spbroker-prd.bzk.sson.overheid-i.nl/authentication/idp/single-sign-on

  # Add mappers
  python migrate_sso_idp.py add-mappers --idp-alias sso-rijk-new --type oidc

  # Swap IDPs (dry run)
  python migrate_sso_idp.py swap --old-alias sso-rijk --new-alias sso-rijk-new --dry-run

  # Swap IDPs (for real)
  python migrate_sso_idp.py swap --old-alias sso-rijk --new-alias sso-rijk-new
        """,
    )

    # Common arguments
    parser.add_argument(
        "--keycloak-url", default="https://keycloak.kind", help="Keycloak base URL"
    )
    parser.add_argument("--admin-user", default="admin", help="Keycloak admin username")
    parser.add_argument(
        "--admin-password", required=True, help="Keycloak admin password"
    )
    parser.add_argument("--realm", default="rig-platform", help="Realm name")
    parser.add_argument(
        "--insecure", action="store_true", help="Disable SSL verification"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # status command
    status_parser = subparsers.add_parser("status", help="Show IDP status")
    status_parser.add_argument("--old-alias", default="sso-rijk", help="Old IDP alias")
    status_parser.add_argument(
        "--new-alias", default="sso-rijk-new", help="New IDP alias"
    )

    # add-idp command
    add_idp_parser = subparsers.add_parser("add-idp", help="Add a new IDP")
    add_idp_parser.add_argument(
        "--type", choices=["oidc", "saml"], required=True, help="IDP type"
    )
    add_idp_parser.add_argument("--alias", required=True, help="IDP alias")
    add_idp_parser.add_argument("--display-name", help="Display name")
    # OIDC options
    add_idp_parser.add_argument("--discovery-url", help="OIDC discovery URL")
    add_idp_parser.add_argument("--client-id", help="OIDC client ID")
    add_idp_parser.add_argument("--client-secret", help="OIDC client secret")
    # SAML options
    add_idp_parser.add_argument("--entity-id", help="SAML entity ID")
    add_idp_parser.add_argument("--sso-url", help="SAML SSO URL")
    add_idp_parser.add_argument("--logout-url", help="SAML logout URL")
    add_idp_parser.add_argument("--signing-cert", help="SAML signing certificate")

    # add-mappers command
    mappers_parser = subparsers.add_parser("add-mappers", help="Add mappers to an IDP")
    mappers_parser.add_argument("--idp-alias", required=True, help="IDP alias")
    mappers_parser.add_argument(
        "--type",
        choices=["oidc", "saml"],
        help="Mapper type (auto-detected from IDP if not specified)",
    )

    # swap command
    swap_parser = subparsers.add_parser("swap", help="Swap IDPs at database level")
    swap_parser.add_argument(
        "--old-alias", default="sso-rijk", help="Old IDP alias to replace"
    )
    swap_parser.add_argument(
        "--new-alias",
        default="sso-rijk-direct",
        help="New IDP alias (default: sso-rijk-direct)",
    )
    swap_parser.add_argument("--db-host", default="localhost", help="Database host")
    swap_parser.add_argument("--db-port", type=int, default=5432, help="Database port")
    swap_parser.add_argument("--db-name", default="keycloak", help="Database name")
    swap_parser.add_argument("--db-user", default="keycloak", help="Database user")
    swap_parser.add_argument("--db-password", help="Database password")
    swap_parser.add_argument(
        "--backup-dir", default="./backups", help="Backup directory"
    )
    swap_parser.add_argument(
        "--skip-backup", action="store_true", help="Skip database backup"
    )
    swap_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done"
    )
    swap_parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation"
    )
    swap_parser.add_argument(
        "--force", action="store_true", help="Proceed even if backup fails"
    )
    swap_parser.add_argument(
        "--bootstrap-yaml",
        help="Path to bootstrap.yaml to update after swap (removes old IDP, renames new IDP)",
    )

    args = parser.parse_args()

    if args.command == "status":
        cmd_status(args)
    elif args.command == "add-idp":
        cmd_add_idp(args)
    elif args.command == "add-mappers":
        cmd_add_mappers(args)
    elif args.command == "swap":
        cmd_swap(args)


if __name__ == "__main__":
    main()
