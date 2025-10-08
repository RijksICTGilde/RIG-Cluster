"""
SOPS (Secrets OPerationS) connector for handling encryption and decryption operations.

This module provides functionality for:
- Generating age key pairs for SOPS encryption
- Encrypting and decrypting content using SOPS
- Managing project-specific SOPS keys
- Interacting with Kubernetes secrets for SOPS keys
"""

import asyncio
import logging
import os
import tempfile
from typing import Any

from opi.connectors.kubectl import KubectlConnector
from opi.core.config import settings

logger = logging.getLogger(__name__)


# TODO: maybe this handler has to go.. it conflicts with the utils/sops.py
class SopsHandler:
    """
    Connector for handling SOPS encryption/decryption operations.

    This connector manages:
    - Age key pair generation for project-specific encryption
    - SOPS encryption/decryption operations
    - Integration with Kubernetes secrets for key storage
    """

    def __init__(self, kubectl_connector: KubectlConnector | None = None):
        """
        Initialize the SOPS connector.

        Args:
            kubectl_connector: Optional KubectlConnector for Kubernetes operations
        """
        self.kubectl = kubectl_connector
        self.logger = logging.getLogger(__name__)

    # TODO maybe this  in age.py as well
    async def generate_age_key_pair(self) -> tuple[str, str]:
        """
        Generate a new age key pair for SOPS encryption.

        Returns:
            Tuple of (private_key, public_key)

        Raises:
            RuntimeError: If key generation fails
        """
        self.logger.info("Generating new age key pair for SOPS")

        try:
            # Use age-keygen to generate a new key pair
            result = await self._run_command(["age-keygen"])

            if result.returncode != 0:
                raise RuntimeError(f"Failed to generate age key pair: {result.stderr}")

            # Parse the output to extract private and public keys
            output_lines = result.stdout.strip().split("\n")
            private_key = None
            public_key = None

            for line in output_lines:
                line = line.strip()
                if line.startswith("AGE-SECRET-KEY-"):
                    private_key = line
                elif line.startswith("# public key: age"):
                    public_key = line.replace("# public key: ", "")

            if not private_key or not public_key:
                raise RuntimeError("Failed to parse generated age key pair")

            self.logger.info("Successfully generated age key pair")
            self.logger.debug(f"Public key: {public_key}")

            return private_key, public_key

        except Exception as e:
            self.logger.error(f"Error generating age key pair: {e}")
            raise RuntimeError(f"Failed to generate age key pair: {e}") from e

    async def encrypt_content(self, content: str, public_key: str) -> str:
        """
        Encrypt content using SOPS with the provided public key.

        Args:
            content: Content to encrypt
            public_key: Age public key for encryption

        Returns:
            Encrypted content as string

        Raises:
            RuntimeError: If encryption fails
        """
        self.logger.debug("Encrypting content with SOPS")

        try:
            from opi.core.config import settings

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, dir=settings.TEMP_DIR
            ) as temp_file:
                temp_file.write(content)
                temp_file_path = temp_file.name

            try:
                # Encrypt the file using SOPS
                cmd = ["sops", "--encrypt", "--age", public_key, temp_file_path]

                result = await self._run_command(cmd)

                if result.returncode != 0:
                    raise RuntimeError(f"SOPS encryption failed: {result.stderr}")

                encrypted_content = result.stdout
                self.logger.debug("Successfully encrypted content with SOPS")

                return encrypted_content

            finally:
                # Clean up temporary file
                os.unlink(temp_file_path)

        except Exception as e:
            self.logger.error(f"Error encrypting content: {e}")
            raise RuntimeError(f"Failed to encrypt content: {e}") from e

    async def decrypt_content(self, encrypted_content: str, private_key: str) -> str:
        """
        Decrypt SOPS-encrypted content using the provided private key.

        Args:
            encrypted_content: SOPS-encrypted content
            private_key: Age private key for decryption

        Returns:
            Decrypted content as string

        Raises:
            RuntimeError: If decryption fails
        """
        self.logger.debug("Decrypting content with SOPS")

        try:
            from opi.core.config import settings

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, dir=settings.TEMP_DIR
            ) as temp_file:
                temp_file.write(encrypted_content)
                temp_file_path = temp_file.name

            try:
                # Set the age key in environment for SOPS
                env = os.environ.copy()
                env["SOPS_AGE_KEY"] = private_key

                # Decrypt the file using SOPS
                cmd = ["sops", "--decrypt", temp_file_path]

                result = await self._run_command(cmd, env=env)

                if result.returncode != 0:
                    raise RuntimeError(f"SOPS decryption failed: {result.stderr}")

                decrypted_content = result.stdout
                self.logger.debug("Successfully decrypted content with SOPS")

                return decrypted_content

            finally:
                # Clean up temporary file
                os.unlink(temp_file_path)

        except Exception as e:
            self.logger.error(f"Error decrypting content: {e}")
            raise RuntimeError(f"Failed to decrypt content: {e}") from e

    async def store_project_sops_key_in_namespace(self, namespace: str, private_key: str, public_key: str) -> bool:
        """
        Store a project-specific SOPS key pair in a Kubernetes namespace.

        Args:
            namespace: Target namespace
            private_key: Age private key
            public_key: Age public key

        Returns:
            True if successful, False otherwise
        """
        if not self.kubectl:
            self.logger.error("KubectlConnector not available for storing SOPS key")
            return False

        self.logger.info(f"Storing project-specific SOPS key in namespace: {namespace}")

        try:
            # Construct the full key contents format for the AGE key file
            # This matches the format that AGE key files use
            full_key_contents = f"# created: project-specific key\n# public key: {public_key}\n{private_key}\n"

            # Create secret using the generic secret template
            template_path = "manifests/generic-secret.yaml.to-sops.jinja"
            variables = {
                "name": "sops-age-key",
                "namespace": namespace,
                "secret_type": "sops",
                "secret_k8s_type": "Opaque",
                "secret_pairs": {"key": full_key_contents},
            }

            result = await self.kubectl.apply_manifest(template_path, variables, namespace)

            if result:
                self.logger.info(f"Successfully stored SOPS key in namespace: {namespace}")
                self.logger.debug(f"Public key for project: {public_key}")
                return True
            else:
                self.logger.error(f"Failed to store SOPS key in namespace: {namespace}")
                return False

        except Exception as e:
            self.logger.error(f"Error storing SOPS key in namespace {namespace}: {e}")
            return False

    async def store_project_sops_key_in_gitops(
        self, project_name: str, private_key: str, public_key: str, git_connector
    ) -> bool:
        """
        Store a project-specific SOPS key in the GitOps repository, encrypted with ArgoCD manager's key.

        Args:
            project_name: Name of the project
            private_key: Age private key
            public_key: Age public key
            git_connector: GitConnector for the GitOps repository

        Returns:
            True if successful, False otherwise
        """
        self.logger.info(f"Storing encrypted SOPS key backup for project: {project_name}")

        try:
            # Use SOPS keys from config instead of retrieving from ArgoCD namespace
            manager_public_key = settings.SOPS_AGE_PUBLIC_KEY
            manager_private_key_clean = settings.SOPS_AGE_PRIVATE_KEY

            if not manager_public_key or not manager_private_key_clean:
                self.logger.error(
                    "SOPS keys not available in config (SOPS_AGE_PUBLIC_KEY or SOPS_AGE_PRIVATE_KEY missing)"
                )
                return False

            # Construct the full key contents for the project
            project_key_contents = (
                f"# created: project-specific key for {project_name}\n# public key: {public_key}\n{private_key}\n"
            )

            # Encrypt the project key using Age encryption (not SOPS)
            from opi.utils.age import encrypt_age_content

            encrypted_key_contents = await encrypt_age_content(project_key_contents, manager_private_key_clean)

            if not encrypted_key_contents:
                self.logger.error(f"Failed to encrypt project key for backup: {project_name}")
                return False

            # Store in GitOps repository at projects/{project_name}/age-key.txt.age
            key_file_path = f"projects/{project_name}/age-key.txt.age"

            await git_connector.write_file_without_commit(key_file_path, encrypted_key_contents)

            self.logger.info(f"Successfully stored encrypted SOPS key backup at: {key_file_path}")
            return True

        except Exception as e:
            self.logger.error(f"Error storing SOPS key backup for project {project_name}: {e}")
            return False

    async def retrieve_project_sops_key_from_gitops(self, project_name: str, git_connector) -> tuple[str, str] | None:
        """
        Retrieve and decrypt a project-specific SOPS key from the GitOps repository.

        Args:
            project_name: Name of the project
            git_connector: GitConnector for the GitOps repository

        Returns:
            Tuple of (private_key, public_key) if successful, None otherwise
        """
        self.logger.info(f"Retrieving encrypted SOPS key backup for project: {project_name}")

        try:
            # Use SOPS keys from config instead of retrieving from ArgoCD namespace
            manager_private_key_clean = settings.SOPS_AGE_PRIVATE_KEY

            if not manager_private_key_clean:
                self.logger.error("SOPS private key not available in config (SOPS_AGE_PRIVATE_KEY missing)")
                return None

            # Read encrypted key file from GitOps repository
            key_file_path = f"projects/{project_name}/age-key.txt.age"

            try:
                encrypted_contents = await git_connector.read_file(key_file_path)
                if not encrypted_contents:
                    self.logger.warning(f"No encrypted SOPS key backup found at: {key_file_path}")
                    return None
            except Exception:
                self.logger.warning(f"Could not read encrypted SOPS key backup from: {key_file_path}")
                return None

            # Decrypt the key contents using Age (not SOPS)
            from opi.utils.age import decrypt_age_content

            decrypted_contents = await decrypt_age_content(encrypted_contents, manager_private_key_clean)

            if not decrypted_contents:
                self.logger.error(f"Failed to decrypt SOPS key backup for project: {project_name}")
                return None

            # Extract project keys from decrypted contents
            project_private_key, project_public_key = self._extract_keys_from_age_contents(decrypted_contents)

            if project_private_key and project_public_key:
                self.logger.info(f"Successfully retrieved SOPS key backup for project: {project_name}")
                return project_private_key, project_public_key
            else:
                self.logger.error(f"Could not parse decrypted SOPS key for project: {project_name}")
                return None

        except Exception as e:
            self.logger.error(f"Error retrieving SOPS key backup for project {project_name}: {e}")
            return None

    def _extract_keys_from_age_contents(self, age_contents: str) -> tuple[str, str]:
        """
        Extract private and public keys from AGE key file contents.

        Args:
            age_contents: Full AGE key file contents

        Returns:
            Tuple of (private_key, public_key)
        """
        try:
            lines = age_contents.strip().split("\n")
            private_key = None
            public_key = None

            for line in lines:
                line = line.strip()
                if line.startswith("AGE-SECRET-KEY-"):
                    private_key = line
                elif line.startswith("# public key: age"):
                    public_key = line.replace("# public key: ", "")

            return private_key or "", public_key or ""

        except Exception as e:
            self.logger.error(f"Error extracting keys from age contents: {e}")
            return "", ""

    async def retrieve_project_sops_key_from_namespace(self, namespace: str) -> str | None:
        """
        Retrieve a project-specific SOPS private key from a Kubernetes namespace.

        Args:
            namespace: Source namespace

        Returns:
            Private key string if found, None otherwise
        """
        if not self.kubectl:
            self.logger.error("KubectlConnector not available for retrieving SOPS key")
            return None

        self.logger.debug(f"Retrieving project-specific SOPS key from namespace: {namespace}")

        try:
            private_key = await self.kubectl.get_sops_secret_from_namespace(namespace)

            if private_key:
                self.logger.debug(f"Successfully retrieved SOPS key from namespace: {namespace}")
                return private_key
            else:
                self.logger.warning(f"No SOPS key found in namespace: {namespace}")
                return None

        except Exception as e:
            self.logger.error(f"Error retrieving SOPS key from namespace {namespace}: {e}")
            return None

    async def _run_command(self, cmd: list[str], env: dict[str, str] | None = None) -> Any:
        """
        Run a command asynchronously and return the result.

        Args:
            cmd: Command and arguments to run
            env: Optional environment variables

        Returns:
            CompletedProcess result
        """
        if env is None:
            env = os.environ.copy()

        try:
            self.logger.debug(f"Running command: {' '.join(cmd)}")

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
            )

            stdout, stderr = await process.communicate()

            # Create a result object similar to subprocess.CompletedProcess
            class AsyncResult:
                def __init__(self, returncode: int, stdout: str, stderr: str):
                    self.returncode = returncode
                    self.stdout = stdout
                    self.stderr = stderr

            return AsyncResult(
                returncode=process.returncode,
                stdout=stdout.decode("utf-8") if stdout else "",
                stderr=stderr.decode("utf-8") if stderr else "",
            )

        except Exception as e:
            self.logger.error(f"Error running command {' '.join(cmd)}: {e}")
            raise
