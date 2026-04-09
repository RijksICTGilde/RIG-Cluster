#!/usr/bin/env python3
"""Provision APM user with Kibana space, Elasticsearch role, user, and API key."""

import argparse
import base64
import json
import os
import subprocess
import sys
from typing import Any

import requests

# Disable SSL warnings for development
requests.packages.urllib3.disable_warnings()

# Configuration
ELASTICSEARCH_URL = "https://localhost:9200"
KIBANA_URL = "https://localhost:5601"
APM_SERVER_URL = "https://localhost:8200"

ADMIN_USERNAME = "elastic"
NAMESPACE = "rig-system"

def get_secret(name: str, key: str, namespace: str) -> str:
    """Fetch secret from Kubernetes."""
    try:
        result = subprocess.run(
            ["kubectl", "get", "secret", name, "-n", namespace, "-o", f"jsonpath={{.data.{key}}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return base64.b64decode(result.stdout).decode()
    except Exception:
        return ""

def get_auth_header(username: str, password: str) -> dict[str, str]:
    """Generate basic auth header."""
    credentials = f"{username}:{password}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def create_kibana_space(username: str, admin_password: str) -> dict[str, Any]:
    """Create a Kibana space for the user."""
    space_id = f"{username}-space"
    space_name = f"{username} Space"

    payload = {
        "id": space_id,
        "name": space_name,
        "description": f"APM space for {username}",
        "disabledFeatures": [],
    }

    response = requests.post(
        f"{KIBANA_URL}/api/spaces/space",
        headers={
            **get_auth_header(ADMIN_USERNAME, admin_password),
            "kbn-xsrf": "true",
            "Content-Type": "application/json",
        },
        json=payload,
        verify=False,
    )

    if response.status_code in [200, 409]:  # 409 means already exists
        print(f"Kibana space created: {space_id}")
        return {"space_id": space_id, "space_name": space_name}
    else:
        print(f"Failed to create Kibana space: {response.status_code}")
        print(f"Response: {response.text}")
        sys.exit(1)


def create_elasticsearch_role(username: str, space_id: str, admin_password: str) -> str:
    """Create an Elasticsearch role with APM permissions."""
    role_name = f"{username}-apm-role"

    # Restrict permissions to user-specific indices only
    # Our naming convention is: <type>-apm-<username>
    # Also include the hidden data streams: .ds-<type>-apm-<username>-*
    user_index_patterns = [
        f"apm-{username}*",
        f"traces-apm-{username}*",
        f"logs-apm-{username}*",
        f"metrics-apm-{username}*",
        f".ds-apm-{username}-*",
        f".ds-traces-apm-{username}-*",
        f".ds-logs-apm-{username}-*",
        f".ds-metrics-apm-{username}-*",
    ]

    payload = {
        "cluster": ["monitor", "manage_own_api_key"],
        "indices": [
            {
                "names": user_index_patterns,
                "privileges": ["read", "view_index_metadata"],
                "allow_restricted_indices": True,
            }
        ],
        "applications": [
            {
                "application": "kibana-.kibana",
                "privileges": ["feature_apm.all", "feature_apm.read"],
                "resources": [f"space:{space_id}"],
            }
        ],
    }

    response = requests.put(
        f"{ELASTICSEARCH_URL}/_security/role/{role_name}",
        headers={
            **get_auth_header(ADMIN_USERNAME, admin_password),
            "Content-Type": "application/json",
        },
        json=payload,
        verify=False,
    )

    if response.status_code in [200, 201]:
        print(f"Elasticsearch role created: {role_name}")
        return role_name
    else:
        print(f"Failed to create Elasticsearch role: {response.status_code}")
        print(f"Response: {response.text}")
        sys.exit(1)


def create_elasticsearch_user(username: str, role_name: str, password: str, admin_password: str) -> dict[str, str]:
    """Create an Elasticsearch user."""
    payload = {
        "password": password,
        "roles": [role_name],
        "full_name": f"APM User {username}",
    }

    response = requests.put(
        f"{ELASTICSEARCH_URL}/_security/user/{username}",
        headers={
            **get_auth_header(ADMIN_USERNAME, admin_password),
            "Content-Type": "application/json",
        },
        json=payload,
        verify=False,
    )

    if response.status_code in [200, 201]:
        print(f"Elasticsearch user created: {username}")
        return {"username": username, "password": password}
    else:
        print(f"Failed to create Elasticsearch user: {response.status_code}")
        print(f"Response: {response.text}")
        sys.exit(1)


def create_apm_agent_key(username: str, space_id: str, admin_password: str) -> dict[str, str]:
    """Create an APM agent key using administrative credentials to ensure it's verifiable by APM Server."""
    payload = {
        "name": f"{username}-apm-agent-key",
        "role_descriptors": {
            "apm_writer": {
                "cluster": ["monitor", "manage_ilm", "read_ilm", "cluster:admin/xpack/monitoring/bulk"],
                "indices": [
                    {
                        "names": [
                            f"apm-{username}*",
                            f"traces-apm-{username}*",
                            f"logs-apm-{username}*",
                            f"metrics-apm-{username}*",
                        ],
                        "privileges": ["create_doc", "create_index", "auto_configure"],
                    }
                ],
            }
        },
        "metadata": {
            "application": "apm",
            "username": username,
            "space": space_id,
        },
    }

    response = requests.post(
        f"{ELASTICSEARCH_URL}/_security/api_key",
        headers={
            **get_auth_header(ADMIN_USERNAME, admin_password),
            "Content-Type": "application/json",
        },
        json=payload,
        verify=False,
    )

    if response.status_code in [200, 201]:
        data = response.json()
        api_key_id = data["id"]
        api_key_secret = data["api_key"]
        encoded_key = data.get("encoded") or base64.b64encode(f"{api_key_id}:{api_key_secret}".encode()).decode()
        print(f"APM agent key created: {api_key_id}")
        return {
            "api_key_id": api_key_id,
            "api_key_secret": api_key_secret,
            "encoded_key": encoded_key,
        }
    else:
        print(f"Failed to create APM agent key: {response.status_code}")
        print(f"Response: {response.text}")
        sys.exit(1)


def generate_password() -> str:
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(20))


def create_kubernetes_secret(username: str, user_token: str, kibana_username: str, kibana_password: str, namespace: str) -> None:
    """Create a Kubernetes secret with APM configuration."""
    secret_name = f"{username}-apm-config"
    apm_server_url = f"http://{username}-apm.{namespace}.svc.cluster.local:8200"

    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": namespace,
        },
        "type": "Opaque",
        "stringData": {
            "apm-server-url": apm_server_url,
            "secret-token": user_token,
            "kibana-username": kibana_username,
            "kibana-password": kibana_password,
        },
    }

    manifest_file = f"secret-{username}-apm.yaml"
    with open(manifest_file, "w") as f:
        json.dump(secret_manifest, f, indent=2)

    try:
        subprocess.run(["kubectl", "apply", "-f", manifest_file], check=True, capture_output=True)
        print(f"Kubernetes secret created: {secret_name} in namespace {namespace}")
        os.remove(manifest_file)
    except subprocess.CalledProcessError as e:
        print(f"Failed to create Kubernetes secret: {e}")
        print(f"stderr: {e.stderr.decode()}")
        if os.path.exists(manifest_file):
            os.remove(manifest_file)
        sys.exit(1)


def deploy_dedicated_apm_server(username: str, admin_password: str) -> str:
    import secrets
    import string
    
    token = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
    
    with open("infrastructure/bootstrap/infrastructure/elastic/controller/base/apm_server_template.yaml.tmp", "r") as f:
        template = f.read()
    
    manifest = template.replace("{username}", username).replace("{token}", token).replace("{elastic_password}", admin_password)
    
    manifest_file = f"apm-server-{username}.yaml"
    with open(manifest_file, "w") as f:
        f.write(manifest)
    
    try:
        subprocess.run(["kubectl", "apply", "-f", manifest_file], check=True)
        print(f"Dedicated APM Server deployed for {username}")
        # Clean up temporary manifest
        os.remove(manifest_file)
        return token
    except Exception as e:
        print(f"Failed to deploy APM Server: {e}")
        sys.exit(1)


def main():
    """Main provisioning workflow."""
    parser = argparse.ArgumentParser(
        description="Provision dedicated APM Server for a user"
    )
    parser.add_argument("username", help="Username for the new APM user")
    parser.add_argument("--password", help="Admin password (will try to fetch from k8s if not provided)")

    args = parser.parse_args()
    username = args.username

    admin_password = args.password or os.environ.get("ELASTIC_PASSWORD") or get_secret("quickstart-es-elastic-user", "elastic", NAMESPACE)

    if not admin_password:
        print("Error: Admin password not found. Provide it via --password or ELASTIC_PASSWORD env var.")
        sys.exit(1)

    password = generate_password()

    print(f"\nProvisioning Dedicated APM for: {username}\n")

    # Step 1: Create Kibana space
    space_info = create_kibana_space(username, admin_password)
    space_id = space_info["space_id"]

    # Step 2: Create Elasticsearch role
    role_name = create_elasticsearch_role(username, space_id, admin_password)

    # Step 3: Create Elasticsearch user
    user_info = create_elasticsearch_user(username, role_name, password, admin_password)

    # Step 4: Deploy Dedicated APM Server
    user_token = deploy_dedicated_apm_server(username, admin_password)

    # Step 5: Create Kubernetes secret with APM configuration
    create_kubernetes_secret(username, user_token, username, password, NAMESPACE)

    # Output summary
    print(f"\nProvisioning complete!\n")
    print("User Credentials:")
    print(f"Username: {username}")
    print(f"Password: {password}")
    print(f"\nKibana Access:")
    print(f"Space: {space_info['space_name']}")
    print(f"URL: {KIBANA_URL}/s/{space_id}/app/apm")

    print(f"\nAuthentication for Dedicated APM Server:")
    print(f"Secret Token: {user_token}")
    print(f"Authorization: Bearer {user_token}")

    print(f"\nDedicated APM Server URL: http://{username}-apm.sandbox.rijksapp.dev")
    print(f"Internal kubernetes service url: {username}-apm.{NAMESPACE}.svc.cluster.local:8200")

    print(f"\nKubernetes Secret: {username}-apm-config (namespace: {NAMESPACE})")
    print(f"- apm-server-url")
    print(f"- secret-token")
    print(f"- kibana-username")
    print(f"- kibana-password\n")




if __name__ == "__main__":
    main()
