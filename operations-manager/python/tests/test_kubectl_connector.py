"""
Tests for the kubectl connector.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from opi.connectors.kubectl import KubectlConnector, create_kubectl_connector


class TestKubectlConnector(unittest.TestCase):
    """Test cases for the KubectlConnector class."""

    def setUp(self):
        """Set up test fixtures."""
        self.connector = KubectlConnector()

        # Create a temporary test manifest file
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manifest_path = os.path.join(self.temp_dir.name, "test_manifest.yaml")

        # Create a simple test manifest with template variables
        with open(self.manifest_path, "w") as f:
            f.write("""apiVersion: v1
kind: Namespace
metadata:
  name: {{ .Values.namespace }}
  labels:
    argocd.argoproj.io/managed-by: {{ .Values.manager }}
    created-by: operations-manager
""")

        # Variables for templating
        self.variables = {"namespace": "test-project", "manager": "rig-system"}

    def tearDown(self):
        """Tear down test fixtures."""
        self.temp_dir.cleanup()

    def test_create_kubectl_connector(self):
        """Test creating a kubectl connector."""
        connector = create_kubectl_connector()
        self.assertIsInstance(connector, KubectlConnector)

    def test_template_manifest(self):
        """Test templating a manifest with variables."""
        # Read the test manifest
        with open(self.manifest_path) as f:
            manifest_content = f.read()

        # Apply templating
        result = self.connector.template_manifest(manifest_content, self.variables)

        # Check that variables were properly replaced
        self.assertIn("name: test-project", result)
        self.assertIn("argocd.argoproj.io/managed-by: rig-system", result)

    @patch("opi.connectors.kubectl.KubectlConnector._run_kubectl_command")
    async def test_apply_manifest(self, mock_run_cmd):
        """Test applying a manifest."""
        # Mock the kubectl command execution
        mock_run_cmd.return_value = ("namespace/test-project created", "", 0)

        # Apply the manifest
        result = await self.connector.apply_manifest(self.manifest_path, self.variables)

        # Check that the command was successful
        self.assertTrue(result)

        # Check that the kubectl command was called with the right arguments
        mock_run_cmd.assert_called_once()
        args = mock_run_cmd.call_args[0][0]
        self.assertEqual(args[0], "apply")
        self.assertEqual(args[1], "-f")
        # The third argument is the temporary file path, which we can't check exactly

    @patch("opi.connectors.kubectl.KubectlConnector._run_kubectl_command")
    async def test_apply_manifest_failure(self, mock_run_cmd):
        """Test applying a manifest with a failure."""
        # Mock the kubectl command execution to simulate a failure
        mock_run_cmd.return_value = ("", "Error: unable to recognize", 1)

        # Apply the manifest
        result = await self.connector.apply_manifest(self.manifest_path, self.variables)

        # Check that the command failed
        self.assertFalse(result)


if __name__ == "__main__":
    # Run the tests
    unittest.main()
