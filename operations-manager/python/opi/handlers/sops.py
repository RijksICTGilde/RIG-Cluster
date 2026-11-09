"""
SOPS (Secrets OPerationS) connector for the project-specific SOPS key.

This module provides functionality for:
- Managing project-specific SOPS keys
- Interacting with Kubernetes secrets for SOPS keys
"""

import logging
from typing import TYPE_CHECKING

from opi.connectors.kubectl import KubectlExecutionError
from opi.core.cluster_config import get_argo_namespace, is_platform_namespace
from opi.core.config import settings
from opi.utils.connect_retry import retry_on_connect_error

if TYPE_CHECKING:
    from opi.connectors.kubectl import KubectlConnector

logger = logging.getLogger(__name__)

# A freshly created namespace's Capsule tenant RBAC (the binding that grants our
# service account admin in the namespace) takes ~1s to propagate; the first secret
# apply then fails with a transient Forbidden. Retry on that race before treating a
# failure as a real, alert-worthy error.
_SOPS_STORE_BUDGET_SECONDS = 60.0

#: Secret en Role voor het sleutel-secret. De Role gaat mee bij elke verwerking, ook als
#: het secret al klopt: zonder Role leest de CMP het secret niet en rendert ArgoCD niet.
SOPS_SECRET_NAME = "sops-age-key"  # noqa: S105  # naam van het secret, geen secret
CMP_SOPS_ROLE_NAME = "cmp-sops-key-reader"
CMP_SERVICE_ACCOUNT = "argocd-cmp"


def _rbac_still_propagating(error: BaseException) -> bool:
    """Whether a failed secret apply is the Capsule-RBAC race and not a real refusal."""
    return isinstance(error, KubectlExecutionError) and "forbidden" in str(error).lower()


def format_age_key_file(private_key: str, public_key: str) -> str:
    """De inhoud van het Secret ``sops-age-key``, in de vorm van een AGE-sleutelbestand.

    Het Kopia-wachtwoord wordt uit deze hele tekst afgeleid, dus wie voorspelt wat er na een
    sleutelvervanging in de namespace komt te staan moet dezelfde vorm gebruiken.
    """
    return f"# created: project-specific key\n# public key: {public_key}\n{private_key}\n"


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

            async def _apply() -> None:
                await self.kubectl.apply_manifest(template_path, variables, namespace)

            try:
                await retry_on_connect_error(
                    _apply,
                    _SOPS_STORE_BUDGET_SECONDS,
                    f"SOPS key in namespace {namespace}",
                    retry_on=_rbac_still_propagating,
                )
            except KubectlExecutionError as apply_error:
                # The caller turns this False into its own ERROR and raises, so the
                # give-up itself stays a warning.
                self.logger.warning(
                    f"Failed to store SOPS key (secret sops-age-key) in namespace {namespace}: {apply_error}"
                )
                return False

            self.logger.info(f"Successfully stored SOPS key in namespace: {namespace}")
            self.logger.debug(f"Public key for project: {public_key}")

            # Separate, so a failing Role does not make us write the secret again, and with
            # its own message: the secret is there.
            try:
                await self.ensure_cmp_key_reader(namespace)
            except KubectlExecutionError as role_error:
                self.logger.error(
                    f"SOPS key stored, but failed to apply the CMP key reader role "
                    f"({CMP_SOPS_ROLE_NAME}) in namespace {namespace}: {role_error}"
                )
                return False
            return True

        except Exception as e:
            self.logger.error(f"Error storing SOPS key in namespace {namespace}: {e}")
            return False

    async def ensure_cmp_key_reader(self, namespace: str) -> None:
        """
        Laat de CMP-sidecar precies het sleutel-secret van deze namespace lezen.

        Idempotent: een gelijke Role en RoleBinding verandert niets.

        Raises:
            ValueError: als de namespace van het platform zelf is
            KubectlExecutionError: als de Role of RoleBinding niet toegepast kan worden
        """
        # Zelfde tweede laag als bij store_project_sops_key_in_namespace hierboven: de
        # namespace komt uit een projectbestand, en in een platformnamespace is
        # 'sops-age-key' de PLATFORMsleutel. Die leest de platform-sidecar uit zijn eigen
        # mount; de tenant-sidecar hoort daar niets te mogen. Zelfde uitzondering als
        # SKIP_NAMESPACES in de migratietaak in Taskfile.yaml.
        if is_platform_namespace(namespace):
            raise ValueError(
                f"Weigert de Role '{CMP_SOPS_ROLE_NAME}' te zetten in namespace '{namespace}': "
                f"dat is een namespace van het platform zelf."
            )

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
