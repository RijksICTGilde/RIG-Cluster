import logging
from typing import Any

from opi.extensions.base import ManifestExtension

logger = logging.getLogger(__name__)

# Kubernetes resource kinds that contain a pod spec at spec.template.spec
_POD_SPEC_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job"}
# CronJob has the pod spec nested one level deeper
_CRONJOB_KIND = "CronJob"


class RegistryRewriteExtension(ManifestExtension):
    """Rewrites container image registry URLs and adds imagePullSecrets.

    For each mapping in config, rewrites image prefixes and ensures
    the corresponding imagePullSecret is present on the pod spec.
    """

    def process(self, manifest: dict[str, Any]) -> dict[str, Any]:
        kind = manifest.get("kind", "")
        pod_spec = self._get_pod_spec(manifest, kind)
        if pod_spec is None:
            return manifest

        secrets_to_add: set[str] = set()

        for container_list_key in ("containers", "initContainers"):
            for container in pod_spec.get(container_list_key, []):
                image = container.get("image", "")
                new_image, secret = self._rewrite_image(image)
                if new_image != image:
                    container["image"] = new_image
                    logger.info(f"Rewrote image: {image} -> {new_image}")
                    if secret:
                        secrets_to_add.add(secret)

        if secrets_to_add:
            self._add_pull_secrets(pod_spec, secrets_to_add)

        return manifest

    def _rewrite_image(self, image: str) -> tuple[str, str | None]:
        """Rewrite an image URL based on configured mappings.

        Returns (new_image, imagePullSecret) or (original_image, None) if no match.
        """
        for mapping in self.config.get("mappings", []):
            from_prefix = mapping["from"]
            if image.startswith(from_prefix + "/") or image == from_prefix:
                new_image = mapping["to"] + image[len(from_prefix) :]
                return new_image, mapping.get("imagePullSecret")
        return image, None

    def _get_pod_spec(self, manifest: dict[str, Any], kind: str) -> dict[str, Any] | None:
        """Extract the pod spec from a manifest, or None if not applicable."""
        if kind == "Pod":
            return manifest.get("spec")
        if kind in _POD_SPEC_KINDS:
            return manifest.get("spec", {}).get("template", {}).get("spec")
        if kind == _CRONJOB_KIND:
            return manifest.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
        return None

    def _add_pull_secrets(self, pod_spec: dict[str, Any], secrets: set[str]) -> None:
        """Add imagePullSecrets to a pod spec, deduplicating with existing ones."""
        existing = pod_spec.get("imagePullSecrets", [])
        existing_names = {s["name"] for s in existing}
        for secret_name in secrets:
            if secret_name not in existing_names:
                existing.append({"name": secret_name})
        pod_spec["imagePullSecrets"] = existing
