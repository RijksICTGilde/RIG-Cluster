import glob
import logging
import os
from typing import TYPE_CHECKING, Any

import yaml

from opi.core.cluster_config import get_extensions
from opi.extensions.registry_rewrite import RegistryRewriteExtension

if TYPE_CHECKING:
    from opi.extensions.base import ManifestExtension

logger = logging.getLogger(__name__)

# Registry mapping extension type names to their classes
EXTENSION_TYPES: dict[str, type[ManifestExtension]] = {
    "registry-rewrite": RegistryRewriteExtension,
}

# Path to extension YAML definitions, relative to working directory (same pattern as MANIFESTS_PATH)
_EXTENSIONS_DIR = "extensions"


class ExtensionPipeline:
    """Runs a sequence of manifest extensions over generated manifest files."""

    def __init__(self, extensions: list[ManifestExtension]) -> None:
        self._extensions = extensions

    @property
    def has_extensions(self) -> bool:
        return len(self._extensions) > 0

    def process_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Run all extensions on a single manifest dict."""
        for extension in self._extensions:
            manifest = extension.process(manifest)
        return manifest

    def process_directory(self, target_path: str) -> None:
        """Process all regular YAML manifest files in a directory.

        Skips SOPS files (.sops.yaml, .to-sops.yaml) and kustomization files.
        """
        if not self.has_extensions:
            return

        skip_suffixes = (".sops.yaml", ".to-sops.yaml")
        skip_names = ("kustomization.yaml", "decrypt-sops.yaml")

        for file_path in glob.glob(os.path.join(target_path, "*.yaml")):
            filename = os.path.basename(file_path)
            if filename in skip_names or any(filename.endswith(s) for s in skip_suffixes):
                continue

            try:
                with open(file_path) as f:
                    manifest = yaml.safe_load(f)

                if not isinstance(manifest, dict):
                    continue

                processed = self.process_manifest(manifest)

                with open(file_path, "w") as f:
                    yaml.dump(processed, f, default_flow_style=False, sort_keys=False)

            except Exception:
                logger.exception(f"Failed to process manifest extension for {file_path}")
                raise


def _load_extension_definition(name: str) -> dict[str, Any]:
    """Load an extension YAML definition by name."""
    file_path = os.path.join(_EXTENSIONS_DIR, f"{name}.yaml")
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Extension definition not found: {file_path}")

    with open(file_path) as f:
        definition = yaml.safe_load(f)

    if not isinstance(definition, dict) or "type" not in definition:
        raise ValueError(f"Extension definition {name} must have a 'type' field")

    return definition


def load_extensions(cluster_name: str) -> ExtensionPipeline:
    """Load and instantiate all extensions configured for a cluster."""
    extension_names = get_extensions(cluster_name)
    extensions: list[ManifestExtension] = []

    for name in extension_names:
        definition = _load_extension_definition(name)
        ext_type = definition["type"]
        ext_class = EXTENSION_TYPES.get(ext_type)
        if ext_class is None:
            raise ValueError(f"Unknown extension type '{ext_type}' in '{name}'")

        config = definition.get("config", {})
        extensions.append(ext_class(config))
        logger.info(f"Loaded extension '{name}' (type: {ext_type})")

    return ExtensionPipeline(extensions)


_REWRITE_MAPPINGS_CACHE: dict[str, list[dict[str, str]]] = {}


def get_registry_rewrite_mappings(cluster_name: str) -> list[dict[str, str]]:
    """Return the cluster's registry-rewrite mappings (cached), or [] if none configured."""
    if cluster_name not in _REWRITE_MAPPINGS_CACHE:
        mappings: list[dict[str, str]] = []
        for name in get_extensions(cluster_name):
            definition = _load_extension_definition(name)
            if definition.get("type") == "registry-rewrite":
                mappings = definition.get("config", {}).get("mappings", [])
                break
        _REWRITE_MAPPINGS_CACHE[cluster_name] = mappings
    return _REWRITE_MAPPINGS_CACHE[cluster_name]
