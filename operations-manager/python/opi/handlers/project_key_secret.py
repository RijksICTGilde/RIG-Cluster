"""The project key as a Kubernetes Secret, and the RBAC the ArgoCD CMP needs to read it.

Not about the sops CLI: that, the key generation and the file names are ``opi/utils/sops.py``.
"""

import logging
from typing import TYPE_CHECKING

from opi.connectors.kubectl import KubectlExecutionError
from opi.core.cluster_config import get_argo_namespace, is_platform_namespace
from opi.core.config import format_age_key_file, settings
from opi.utils.connect_retry import retry_on_connect_error

if TYPE_CHECKING:
    from opi.connectors.kubectl import KubectlConnector

logger = logging.getLogger(__name__)

# A freshly created namespace's Capsule tenant RBAC (the binding that grants our
# service account admin in the namespace) takes ~1s to propagate; the first secret
# apply then fails with a transient Forbidden. Retry on that race before treating a
# failure as a real, alert-worthy error.
_SOPS_STORE_BUDGET_SECONDS = 60.0

#: The Secret and Role for the project key. The Role is applied every time, even if the
#: Secret is already present: without it the CMP cannot read the Secret and ArgoCD does not render.
SOPS_SECRET_NAME = "sops-age-key"  # noqa: S105  # the Secret's name, not a secret itself
CMP_SOPS_ROLE_NAME = "cmp-sops-key-reader"
CMP_SERVICE_ACCOUNT = "argocd-cmp"


def _rbac_still_propagating(error: BaseException) -> bool:
    """Whether a failed secret apply is the Capsule-RBAC race and not a real refusal."""
    return isinstance(error, KubectlExecutionError) and "forbidden" in str(error).lower()


class ProjectKeySecretHandler:
    """Writes a project's AGE key pair into its namespace, and keeps the CMP able to read it."""

    def __init__(self, kubectl_connector: KubectlConnector | None = None):
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
        # A project-specific key in this namespace overrides the platform key, so ArgoCD
        # stops rendering platform applications. This is the second guard after the
        # reserved project name check.
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
        """Make the CMP sidecar able to read the key Secret of this namespace.

        Idempotent: an equal Role and RoleBinding change nothing.

        Raises:
            ValueError: if namespace is a platform namespace
            KubectlExecutionError: if the Role or RoleBinding cannot be applied
        """
        # Same second guard as in store_project_sops_key_in_namespace above: the namespace
        # comes from a project file. In a platform namespace, "sops-age-key" is the platform
        # key, which the platform sidecar reads from its own mount; the tenant sidecar must
        # have no access. Same exception as SKIP_NAMESPACES in the migration task in
        # Taskfile.yaml.
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
