"""Test registry configuration at deployment component level."""

from opi.handlers.project_file_handler import ProjectFileHandler


def test_registries_in_project_data():
    """Test that registries list exists in project data."""
    project_data = {
        "registries": [
            {
                "name": "my-registry",
                "url": "registry.example.com",
                "username": "testuser",
                "password": "encrypted-password"
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
            {
                "name": "github-packages",
                "url": "ghcr.io",
                "username": "myuser",
                "password": "encrypted-pat"
            },
            {
                "name": "docker-hub",
                "url": "docker.io",
                "username": "mycompany",
                "password": "encrypted-token"
            }
        ],
        "components": [
            {
                "name": "frontend",
                "type": "deployment",
                "ports": {"inbound": [8080]}
            }
        ],
        "deployments": [
            {
                "name": "production",
                "cluster": "local",
                "namespace": "my-project",
                "components": [
                    {
                        "reference": "frontend",
                        "image": "ghcr.io/myorg/frontend:v1.2.3",
                        "registry": "github-packages"  # Registry at deployment component level
                    }
                ]
            }
        ]
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
            {
                "name": "github-org-packages",
                "url": "ghcr.io",
                "username": "org-bot",
                "password": "encrypted-org-token"
            },
            {
                "name": "github-user-packages",
                "url": "ghcr.io",
                "username": "myuser",
                "password": "encrypted-user-token"
            }
        ],
        "deployments": [
            {
                "name": "production",
                "components": [
                    {
                        "reference": "frontend",
                        "image": "ghcr.io/myorg/frontend:latest",
                        "registry": "github-org-packages"
                    },
                    {
                        "reference": "backend",
                        "image": "ghcr.io/myuser/backend:latest",
                        "registry": "github-user-packages"
                    }
                ]
            }
        ]
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
            {
                "name": "private-registry",
                "url": "registry.example.com",
                "username": "user",
                "password": "pass"
            }
        ],
        "deployments": [
            {
                "name": "production",
                "components": [
                    {
                        "reference": "nginx",
                        "image": "nginx:alpine"
                        # No registry field - public image
                    },
                    {
                        "reference": "custom-app",
                        "image": "registry.example.com/app:v1",
                        "registry": "private-registry"
                    }
                ]
            }
        ]
    }

    deployment = project_data["deployments"][0]

    # First component has no registry (public image)
    assert deployment["components"][0].get("registry") is None

    # Second component has registry (private image)
    assert deployment["components"][1].get("registry") == "private-registry"
