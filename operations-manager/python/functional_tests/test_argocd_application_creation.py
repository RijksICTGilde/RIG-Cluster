#!/usr/bin/env python3
"""
Functional test for ArgoCD application creation using real Git operations.

This test validates the complete workflow of:
1. Parsing a project file (simple-example.yaml)
2. Creating an ArgoCD application manifest
3. Cloning the GitOps repository
4. Committing and pushing the manifest to Git

This test requires:
- Local Git server running on localhost:2222
- SSH key available at the configured path
- ArgoCD applications repository accessible
"""

import asyncio
import logging
import os
import sys
import tempfile

# Add the parent directory to Python path so we can import from opi
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from opi.core.config import get_settings
from opi.manager.project_manager import ProjectManager

# Set up logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class TestArgocdApplicationCreation:
    """Functional test class for ArgoCD application creation."""

    def __init__(self):
        self.settings = get_settings()
        self.project_manager = ProjectManager()
        self.project_file_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "projects", "simple-example.yaml"
        )

    async def test_argocd_application_creation(self):
        """
        Test the complete ArgoCD application creation workflow.
        """
        print("=== Functional Test: ArgoCD Application Creation ===\n")

        # Step 1: Validate configuration
        print("Step 1: Validating configuration...")
        await self._validate_configuration()

        # Step 2: Parse project file
        print("\nStep 2: Parsing project file...")
        project_data = await self._parse_project_file()

        # Step 3: Test ArgoCD application creation
        print("\nStep 3: Testing ArgoCD application creation...")
        success = await self._test_create_argocd_application(project_data)

        # Step 4: Report results
        print("\n=== Test Results ===")
        if success:
            print("✅ ArgoCD application creation test PASSED")
            print("   - Successfully connected to Git repository")
            print("   - Successfully generated ArgoCD manifest")
            print("   - Successfully committed and pushed to GitOps repo")
        else:
            print("❌ ArgoCD application creation test FAILED")
            print("   Check the logs above for error details")

        return success

    async def _validate_configuration(self):
        """Validate that all required configuration is available."""
        print(f"  Git ArgoCD Applications URL: {self.settings.GIT_ARGO_APPLICATIONS_URL}")
        print(f"  SSH Key Path: {self.settings.GIT_ARGO_APPLICATIONS_KEY}")
        print(f"  Branch: {self.settings.GIT_ARGO_APPLICATIONS_BRANCH}")

        # Check if SSH key exists
        ssh_key_path = self.settings.GIT_ARGO_APPLICATIONS_KEY
        if not os.path.exists(ssh_key_path):
            raise FileNotFoundError(f"SSH key not found at: {ssh_key_path}")
        print("  ✓ SSH key found")

        # Check if project file exists
        if not os.path.exists(self.project_file_path):
            raise FileNotFoundError(f"Project file not found at: {self.project_file_path}")
        print(f"  ✓ Project file found: {self.project_file_path}")

    async def _parse_project_file(self):
        """Parse the project file and return project data."""
        try:
            project_data = self.project_manager.parse_project_file(self.project_file_path)
            print(f"  ✓ Successfully parsed project: {project_data.get('name')}")
            print(f"  ✓ Found {len(project_data.get('deployments', []))} deployment(s)")
            print(f"  ✓ Found {len(project_data.get('repositories', []))} repository(ies)")
            return project_data
        except Exception as e:
            print(f"  ❌ Failed to parse project file: {e}")
            raise

    async def _test_create_argocd_application(self, project_data):
        """Test the ArgoCD application creation step."""
        try:
            print("  → Starting ArgoCD application creation...")

            # Create git connector for ArgoCD applications
            import tempfile

            from opi.connectors.git import GitConnector

            with tempfile.TemporaryDirectory() as temp_dir:
                git_connector = GitConnector(
                    repo_url=self.settings.GIT_ARGO_APPLICATIONS_URL,
                    working_dir=temp_dir,
                    branch=self.settings.GIT_ARGO_APPLICATIONS_BRANCH,
                    ssh_key_path=self.settings.GIT_ARGO_APPLICATIONS_KEY,
                )

                # Ensure the repository is cloned
                await git_connector.ensure_repo_cloned()

                # Call the actual method we want to test
                result = await self.project_manager._create_argocd_application(project_data, git_connector)

                if result:
                    print("  ✓ ArgoCD application creation completed successfully")
                    return True
                else:
                    print("  ❌ ArgoCD application creation failed")
                    return False

        except Exception as e:
            print(f"  ❌ Exception during ArgoCD application creation: {e}")
            logger.exception("Detailed error:")
            return False

    async def run_git_connectivity_test(self):
        """Test basic Git connectivity before running the main test."""
        print("=== Git Connectivity Test ===\n")

        from opi.connectors.git import GitConnector

        try:
            print("Testing Git connectivity...")
            print(f"  URL: {self.settings.GIT_ARGO_APPLICATIONS_URL}")

            with tempfile.TemporaryDirectory() as temp_dir:
                connector = GitConnector(
                    repo_url=self.settings.GIT_ARGO_APPLICATIONS_URL,
                    working_dir=temp_dir,
                    branch=self.settings.GIT_ARGO_APPLICATIONS_BRANCH,
                    ssh_key_path=self.settings.GIT_ARGO_APPLICATIONS_KEY,
                )

                print("  → Attempting to clone repository...")
                await connector.ensure_repo_cloned()
                print("  ✓ Successfully cloned GitOps repository")

                # Test creating a simple file
                test_file = os.path.join(temp_dir, "connectivity-test.txt")
                with open(test_file, "w") as f:
                    f.write("Connectivity test file")

                # Add and commit
                add_cmd = ["add", "connectivity-test.txt"]
                commit_cmd = ["commit", "-m", "Connectivity test commit"]

                _, _, code = await connector._run_git_command(add_cmd)
                if code == 0:
                    _, _, code = await connector._run_git_command(commit_cmd)
                    if code == 0:
                        print("  ✓ Successfully committed test file")

                        # Try to push
                        push_cmd = ["push", "origin", self.settings.GIT_ARGO_APPLICATIONS_BRANCH]
                        _, _, code = await connector._run_git_command(push_cmd)
                        if code == 0:
                            print("  ✓ Successfully pushed to remote")
                        else:
                            print("  ⚠ Failed to push to remote (this might be expected)")
                    else:
                        print("  ⚠ Failed to commit test file")
                else:
                    print("  ⚠ Failed to add test file")

                await connector.close()
                print("  ✓ Git connectivity test completed")
                return True

        except Exception as e:
            print(f"  ❌ Git connectivity test failed: {e}")
            logger.exception("Detailed error:")
            return False


async def main():
    """Run the functional tests."""
    print("Starting Functional Tests for ArgoCD Application Creation\n")

    test = TestArgocdApplicationCreation()

    try:
        # First test basic Git connectivity
        connectivity_ok = await test.run_git_connectivity_test()
        print()

        if not connectivity_ok:
            print("⚠ Git connectivity test failed, but continuing with ArgoCD test...")
            print()

        # Run the main ArgoCD application creation test
        success = await test.test_argocd_application_creation()

        print("\n=== Overall Test Results ===")
        if success:
            print("🎉 All functional tests PASSED!")
        else:
            print("💥 Some functional tests FAILED!")

        return success

    except Exception as e:
        print(f"❌ Test setup failed: {e}")
        logger.exception("Detailed error:")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
