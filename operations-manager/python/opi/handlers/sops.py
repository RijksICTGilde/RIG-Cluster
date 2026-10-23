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
from typing import TYPE_CHECKING, Any

from opi.core.cluster_config import get_argo_namespace, is_platform_namespace
from opi.core.config import settings
from opi.utils.subprocess_env import child_env

if TYPE_CHECKING:
    from opi.connectors.kubectl import KubectlConnector

logger = logging.getLogger(__name__)

# A freshly created namespace's Capsule tenant RBAC (the binding that grants our
# service account admin in the namespace) takes ~1s to propagate; the first secret
# apply then fails with a transient Forbidden. Retry on that race before treating a
# failure as a real, alert-worthy error.
_SOPS_STORE_MAX_ATTEMPTS = 4
_SOPS_STORE_RETRY_DELAY_SECONDS = 1.5

#: Secret en Role voor het sleutel-secret. De Role gaat alleen mee bij plaatsen, niet
#: bij reprocess (het secret klopt al).
SOPS_SECRET_NAME = "sops-age-key"  # noqa: S105  # naam van het secret, geen secret
CMP_SOPS_ROLE_NAME = "cmp-sops-key-reader"
CMP_SERVICE_ACCOUNT = "argocd-cmp"


def format_age_key_file(private_key: str, public_key: str) -> str:
    """De inhoud van het Secret ``sops-age-key``, in de vorm van een AGE-sleutelbestand.

    Het Kopia-wachtwoord wordt uit deze hele tekst afgeleid, dus wie voorspelt wat er na een
    sleutelvervanging in de namespace komt te staan moet dezelfde vorm gebruiken.
    """
    return f"# created: project-specific key\n# public key: {public_key}\n{private_key}\n"


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
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, dir=settings.SECRET_TEMP_DIR
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
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, dir=settings.SECRET_TEMP_DIR
            ) as temp_file:
                temp_file.write(encrypted_content)
                temp_file_path = temp_file.name

            try:
                # Set the age key in environment for SOPS
                env = child_env({"SOPS_AGE_KEY": private_key})

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
        # Tweede laag naast de weigering van een gereserveerde projectnaam: de
        # projectsleutel hier overschrijft de platformsleutel, waarna ArgoCD de
        # platformapplicaties niet meer rendert.
        if is_platform_namespace(namespace):
            raise ValueError(
                f"Weigert een projectsleutel 'sops-age-key' te schrijven in namespace '{namespace}': "
                f"dat is een namespace van het platform zelf."
            )

        if not self.kubectl:
            self.logger.error("KubectlConnector not available for storing SOPS key")
            return False

        self.logger.info(f"Storing project-specific SOPS key in namespace: {namespace}")

        try:
            full_key_contents = format_age_key_file(private_key, public_key)

            # Create secret using the generic secret template
            template_path = "manifests/generic-secret.yaml.to-sops.jinja"
            variables = {
                "name": SOPS_SECRET_NAME,
                "namespace": namespace,
                "secret_type": "sops",
                "secret_k8s_type": "Opaque",
                "secret_pairs": {"key": full_key_contents},
            }

            # Retry the apply on the transient Capsule-RBAC race and only log an
            # error once we have genuinely given up, so a failure that self-heals
            # within a second does not raise a false alarm.
            from opi.connectors.kubectl import KubectlExecutionError

            last_error = ""
            stored = False
            for attempt in range(1, _SOPS_STORE_MAX_ATTEMPTS + 1):
                try:
                    await self.kubectl.apply_manifest(template_path, variables, namespace)
                    self.logger.info(f"Successfully stored SOPS key in namespace: {namespace}")
                    self.logger.debug(f"Public key for project: {public_key}")
                    stored = True
                    break
                except KubectlExecutionError as apply_error:
                    last_error = str(apply_error)
                    is_transient = "forbidden" in last_error.lower()
                    if attempt < _SOPS_STORE_MAX_ATTEMPTS and is_transient:
                        self.logger.warning(
                            f"SOPS key not stored yet in namespace {namespace} (Capsule RBAC still "
                            f"propagating), attempt {attempt}/{_SOPS_STORE_MAX_ATTEMPTS}, retrying in "
                            f"{_SOPS_STORE_RETRY_DELAY_SECONDS}s"
                        )
                        await asyncio.sleep(_SOPS_STORE_RETRY_DELAY_SECONDS)
                        continue
                    break

            if stored:
                # Buiten de lus, zodat een falende Role het secret niet opnieuw laat
                # schrijven, en met een eigen melding: het secret staat er wel.
                try:
                    await self._apply_cmp_key_reader_role(namespace)
                except KubectlExecutionError as role_error:
                    self.logger.error(
                        f"SOPS key stored, but failed to apply the CMP key reader role "
                        f"({CMP_SOPS_ROLE_NAME}) in namespace {namespace}: {role_error}"
                    )
                    return False
                return True

            self.logger.error(f"Failed to store SOPS key (secret sops-age-key) in namespace {namespace}: {last_error}")
            return False

        except Exception as e:
            self.logger.error(f"Error storing SOPS key in namespace {namespace}: {e}")
            return False

    async def _apply_cmp_key_reader_role(self, namespace: str) -> None:
        """
        Laat de CMP-sidecar precies het sleutel-secret van deze namespace lezen.

        Raises:
            KubectlExecutionError: als de Role of RoleBinding niet toegepast kan worden
        """
        if not self.kubectl:
            raise RuntimeError("KubectlConnector not available for applying the CMP key reader role")

        variables = {
            "role_name": CMP_SOPS_ROLE_NAME,
            "namespace": namespace,
            "secret_name": SOPS_SECRET_NAME,
            "service_account": CMP_SERVICE_ACCOUNT,
            "argo_namespace": get_argo_namespace(settings.CLUSTER_MANAGER),
        }
        await self.kubectl.apply_manifest("manifests/cmp-sops-key-reader.yaml.jinja", variables, namespace)
        self.logger.info(f"Applied CMP key reader role in namespace: {namespace}")

    async def store_project_sops_key_in_gitops(
        self, project_name: str, private_key: str, public_key: str, git_connector
    ) -> bool:
        """
        Store a project-specific SOPS key in the GitOps repository, wrapped with the project-KEK.

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
            # Construct the full key contents for the project
            project_key_contents = (
                f"# created: project-specific key for {project_name}\n# public key: {public_key}\n{private_key}\n"
            )

            from opi.utils.age import wrap_project_key

            encrypted_key_contents = await wrap_project_key(project_key_contents)

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

            from opi.utils.age import unwrap_project_key

            try:
                decrypted_contents = await unwrap_project_key(encrypted_contents)
            except ValueError as e:
                self.logger.error(f"Failed to decrypt SOPS key backup for project {project_name}: {e}")
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
            env = child_env()

        try:
            self.logger.debug(f"Running command: {' '.join(cmd)}")

            from opi.core.metrics import track_subprocess_memory

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
            )

            async with track_subprocess_memory("sops"):
                stdout, stderr = await process.communicate()

            # Create a result object similar to subprocess.CompletedProcess
            class AsyncResult:
                def __init__(self, returncode: int, stdout: str, stderr: str):
                    self.returncode = returncode
                    self.stdout = stdout
                    self.stderr = stderr

            # communicate() has awaited the exit, so returncode is set; the annotation
            # cannot know that, and defaulting to 0 would report a failure as a success.
            return AsyncResult(
                returncode=process.returncode if process.returncode is not None else -1,
                stdout=stdout.decode("utf-8") if stdout else "",
                stderr=stderr.decode("utf-8") if stderr else "",
            )

        except Exception as e:
            self.logger.error(f"Error running command {' '.join(cmd)}: {e}")
            raise
