"""Test registry configuration at deployment component level."""

from opi.utils.naming import generate_registry_secret_name


def test_registries_in_project_data():
    """Test that registries list exists in project data."""
    project_data = {
        "registries": [
            {
                "name": "my-registry",
                "url": "registry.example.com",
                "username": "testuser",
                "password": "encrypted-password",
            }
        ]
    }

    registries = project_data.get("registries", [])

    assert len(registries) == 1
    assert registries[0]["name"] == "my-registry"
    assert registries[0]["url"] == "registry.example.com"
    assert registries[0]["username"] == "testuser"


def test_registry_from_deployment_component():
    """Test that registry reference is read from deployment component level."""
    # This test validates the logic that would be used in project_manager.py
    # The registry field should be on deployments[].components[].registry

    project_data = {
        "registries": [
            {"name": "github-packages", "url": "ghcr.io", "username": "myuser", "password": "encrypted-pat"},
            {"name": "docker-hub", "url": "docker.io", "username": "mycompany", "password": "encrypted-token"},
        ],
        "components": [{"name": "frontend", "type": "deployment", "ports": {"inbound": [8080]}}],
        "deployments": [
            {
                "name": "production",
                "cluster": "local",
                "namespace": "my-project",
                "components": [
                    {
                        "reference": "frontend",
                        "image": "ghcr.io/myorg/frontend:v1.2.3",
                        "registry": "github-packages",  # Registry at deployment component level
                    }
                ],
            }
        ],
    }

    # Simulate what project_manager does
    deployment = project_data["deployments"][0]
    components = deployment["components"]

    # Get registry reference from deployment component
    component = components[0]
    registry_ref = component.get("registry")

    assert registry_ref == "github-packages", "Registry should be specified at deployment component level"

    # Find the registry configuration directly from project data (as project_manager does)
    registries = project_data.get("registries", [])
    registry_config = None

    for registry in registries:
        if registry.get("name") == registry_ref:
            registry_config = registry
            break

    assert registry_config is not None, "Registry configuration should be found"
    assert registry_config["url"] == "ghcr.io"
    assert registry_config["username"] == "myuser"


def test_multiple_registries_same_url():
    """Test that multiple registries can have the same URL with different credentials."""
    project_data = {
        "registries": [
            {"name": "github-org-packages", "url": "ghcr.io", "username": "org-bot", "password": "encrypted-org-token"},
            {
                "name": "github-user-packages",
                "url": "ghcr.io",
                "username": "myuser",
                "password": "encrypted-user-token",
            },
        ],
        "deployments": [
            {
                "name": "production",
                "components": [
                    {
                        "reference": "frontend",
                        "image": "ghcr.io/myorg/frontend:latest",
                        "registry": "github-org-packages",
                    },
                    {
                        "reference": "backend",
                        "image": "ghcr.io/myuser/backend:latest",
                        "registry": "github-user-packages",
                    },
                ],
            }
        ],
    }

    registries = project_data.get("registries", [])

    assert len(registries) == 2

    # Both registries have same URL
    assert registries[0]["url"] == "ghcr.io"
    assert registries[1]["url"] == "ghcr.io"

    # But different names and credentials
    assert registries[0]["name"] == "github-org-packages"
    assert registries[1]["name"] == "github-user-packages"
    assert registries[0]["username"] == "org-bot"
    assert registries[1]["username"] == "myuser"

    # Verify components reference correct registries
    deployment = project_data["deployments"][0]
    assert deployment["components"][0]["registry"] == "github-org-packages"
    assert deployment["components"][1]["registry"] == "github-user-packages"


def test_registry_optional_on_deployment_component():
    """Test that registry field is optional on deployment components."""
    project_data = {
        "registries": [
            {"name": "private-registry", "url": "registry.example.com", "username": "user", "password": "pass"}
        ],
        "deployments": [
            {
                "name": "production",
                "components": [
                    {
                        "reference": "nginx",
                        "image": "nginx:alpine",
                        # No registry field - public image
                    },
                    {"reference": "custom-app", "image": "registry.example.com/app:v1", "registry": "private-registry"},
                ],
            }
        ],
    }

    deployment = project_data["deployments"][0]

    # First component has no registry (public image)
    assert deployment["components"][0].get("registry") is None

    # Second component has registry (private image)
    assert deployment["components"][1].get("registry") == "private-registry"


def test_registry_with_secret_name():
    """Test that secretName is used directly without creating credentials."""
    registry_config = {
        "name": "odcn-registry",
        "url": "rcr.rijksapps.nl/rig",
        "secretName": "rcr-pull-secret",
    }

    # Simulate the secretName branch in project_manager.py
    secret_name_ref = registry_config.get("secretName")
    assert secret_name_ref == "rcr-pull-secret"

    # When secretName is present, use it directly as the secret name
    secret_name = secret_name_ref
    assert secret_name == "rcr-pull-secret"

    # No username/password needed
    assert "username" not in registry_config
    assert "password" not in registry_config


def test_registry_with_secret_name_takes_precedence():
    """Test that secretName takes precedence when both credentials and secretName are provided."""
    registry_config = {
        "name": "my-registry",
        "url": "registry.example.com",
        "username": "admin",
        "password": "secret",
        "secretName": "existing-pull-secret",
    }

    secret_name_ref = registry_config.get("secretName")
    assert secret_name_ref is not None, "secretName should be present"

    # secretName takes precedence - no RegistrySecret should be created
    secret_name = secret_name_ref
    assert secret_name == "existing-pull-secret"


def test_mixed_registries_secret_name_and_credentials():
    """Test mixing credential-based and secretName-based registries."""
    project_data = {
        "registries": [
            {
                "name": "sandbox-registry",
                "url": "registry.sandbox.rijksapp.dev",
                "username": "admin",
                "password": "plain:admin1234",
            },
            {
                "name": "odcn-registry",
                "url": "rcr.rijksapps.nl/rig",
                "secretName": "rcr-pull-secret",
            },
        ],
        "deployments": [
            {
                "name": "production",
                "components": [
                    {
                        "reference": "frontend",
                        "image": "registry.sandbox.rijksapp.dev/rig/myproject/frontend:v1",
                        "registry": "sandbox-registry",
                    },
                    {
                        "reference": "backend",
                        "image": "rcr.rijksapps.nl/rig/myproject/backend:v1",
                        "registry": "odcn-registry",
                    },
                ],
            }
        ],
    }

    deployment = project_data["deployments"][0]
    registries = project_data.get("registries", [])
    deployment_name = deployment["name"]

    # Simulate the project_manager logic: build image_pull_secrets_map
    image_pull_secrets_map: dict[str, str] = {}

    for component in deployment["components"]:
        registry_ref = component.get("registry")
        if not registry_ref:
            continue

        registry_config = next((r for r in registries if r.get("name") == registry_ref), None)
        assert registry_config is not None

        secret_name_ref = registry_config.get("secretName")
        secret_name = secret_name_ref or generate_registry_secret_name(deployment_name, registry_config["name"])

        image_pull_secrets_map[component["image"]] = secret_name

    # Credential-based registry gets a generated name
    assert image_pull_secrets_map[
        "registry.sandbox.rijksapp.dev/rig/myproject/frontend:v1"
    ] == generate_registry_secret_name("production", "sandbox-registry")

    # secretName-based registry uses the pre-existing secret directly
    assert image_pull_secrets_map["rcr.rijksapps.nl/rig/myproject/backend:v1"] == "rcr-pull-secret"


def test_registry_url_with_path():
    """Test that registry URLs with paths work correctly."""
    registry_config = {
        "name": "odcn-registry",
        "url": "rcr.rijksapps.nl/rig/micro-gc0",
        "secretName": "rcr-pull-secret",
    }

    # URL includes org/project path
    assert "/" in registry_config["url"]
    assert registry_config["url"] == "rcr.rijksapps.nl/rig/micro-gc0"

    # Component image starts with registry URL
    image_url = "rcr.rijksapps.nl/rig/micro-gc0/test:0.1"
    assert image_url.startswith(registry_config["url"])
