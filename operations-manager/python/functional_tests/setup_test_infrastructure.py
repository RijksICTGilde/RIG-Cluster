#!/usr/bin/env python3
"""
Setup script for functional test infrastructure.

This script helps set up the required Git repositories and infrastructure
for running functional tests.
"""

import asyncio
import logging
import os
import sys

# Add the parent directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from opi.connectors.git import create_git_repository
from opi.core.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class TestInfrastructureSetup:
    """Helper class to set up test infrastructure."""

    def __init__(self):
        self.settings = get_settings()

    async def setup_argo_applications_repository(self):
        """Create the ArgoCD applications repository if it doesn't exist."""
        print("=== Setting Up ArgoCD Applications Repository ===\n")

        # Extract repository name from URL
        repo_url = self.settings.GIT_ARGO_APPLICATIONS_URL
        print(f"Repository URL: {repo_url}")

        # Parse URL to extract repo name
        if "argo-applications.git" in repo_url:
            repo_name = "argo-applications"
        else:
            print("❌ Could not determine repository name from URL")
            return False

        print(f"Repository name: {repo_name}")
        print(f"SSH key: {self.settings.GIT_ARGO_APPLICATIONS_KEY}")
        print()

        try:
            print("→ Attempting to create repository...")
            success = await create_git_repository(
                server_host="localhost",
                repo_name=repo_name,
                ssh_key_path=self.settings.GIT_ARGO_APPLICATIONS_KEY,
                ssh_port=2222,
                ssh_user="git",
            )

            if success:
                print("✅ Successfully created ArgoCD applications repository")
                return True
            else:
                print("❌ Failed to create ArgoCD applications repository")
                return False

        except Exception as e:
            print(f"❌ Error creating repository: {e}")
            return False

    async def setup_project_repository(self):
        """Create the project repository from simple-example.yaml if it doesn't exist."""
        print("=== Setting Up Project Repository ===\n")

        # Read the simple-example.yaml to get repository info
        project_file = os.path.join(os.path.dirname(__file__), "..", "..", "..", "projects", "simple-example.yaml")

        try:
            from ruamel.yaml import YAML

            yaml = YAML()
            with open(project_file) as f:
                project_data = yaml.load(f)

            repositories = project_data.get("repositories", [])
            if not repositories:
                print("❌ No repositories found in project file")
                return False

            main_repo = repositories[0]
            repo_url = main_repo.get("url")
            print(f"Project repository URL: {repo_url}")

            # Extract repo name (assuming format like ssh://git@host:port/srv/git/repo-name.git)
            if "your-project.git" in repo_url:
                repo_name = "your-project"
            else:
                print("❌ Could not determine repository name from project file")
                return False

            print(f"Repository name: {repo_name}")
            print()

            print("→ Attempting to create project repository...")
            success = await create_git_repository(
                server_host="localhost",
                repo_name=repo_name,
                ssh_key_path=self.settings.GIT_SERVER_KEY_PATH,
                ssh_port=2222,
                ssh_user="git",
            )

            if success:
                print("✅ Successfully created project repository")
                return True
            else:
                print("❌ Failed to create project repository")
                return False

        except Exception as e:
            print(f"❌ Error setting up project repository: {e}")
            return False

    async def validate_ssh_connectivity(self):
        """Validate SSH connectivity to the Git server."""
        print("=== Validating SSH Connectivity ===\n")

        ssh_key = self.settings.GIT_ARGO_APPLICATIONS_KEY
        print(f"SSH key: {ssh_key}")
        print("Target: git@localhost:2222")
        print()

        try:
            # Test SSH connection
            import subprocess

            cmd = [
                "ssh",
                "-i",
                ssh_key,
                "-p",
                "2222",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=5",
                "git@localhost",
                "echo 'SSH connection successful'",
            ]

            print("→ Testing SSH connection...")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            if result.returncode == 0:
                print("✅ SSH connectivity successful")
                return True
            else:
                print(f"❌ SSH connection failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            print("❌ SSH connection timed out")
            return False
        except Exception as e:
            print(f"❌ Error testing SSH: {e}")
            return False

    async def check_git_server_status(self):
        """Check if Git server is running."""
        print("=== Checking Git Server Status ===\n")

        try:
            import socket

            print("→ Testing connection to localhost:2222...")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex(("localhost", 2222))
            sock.close()

            if result == 0:
                print("✅ Git server is running on localhost:2222")
                return True
            else:
                print("❌ Git server is not accessible on localhost:2222")
                print("   Make sure your Git server is running")
                return False

        except Exception as e:
            print(f"❌ Error checking Git server: {e}")
            return False


async def main():
    """Main setup function."""
    print("🔧 Setting Up Functional Test Infrastructure\n")

    setup = TestInfrastructureSetup()

    # Step 1: Check Git server
    server_ok = await setup.check_git_server_status()
    print()

    if not server_ok:
        print("⚠️  Git server is not running. Please start your Git server first.")
        print("   Example: Start your local Git daemon on port 2222")
        return False

    # Step 2: Validate SSH
    ssh_ok = await setup.validate_ssh_connectivity()
    print()

    if not ssh_ok:
        print("⚠️  SSH connectivity failed. Please check:")
        print("   - SSH key exists and has correct permissions (chmod 600)")
        print("   - Git server accepts SSH connections")
        print("   - SSH key is added to the Git server")
        return False

    # Step 3: Create repositories
    print("Creating required repositories...\n")

    argo_ok = await setup.setup_argo_applications_repository()
    print()

    project_ok = await setup.setup_project_repository()
    print()

    if argo_ok and project_ok:
        print("🎉 Test infrastructure setup completed successfully!")
        print("\nYou can now run functional tests:")
        print("  python functional_tests/test_argocd_application_creation.py")
        print("  python functional_tests/run_all.py")
        return True
    else:
        print("💥 Test infrastructure setup failed!")
        print("   Some repositories could not be created.")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
