"""
Manifest generation utilities for creating Kubernetes manifests with templating and kustomization support.

This module provides functionality to:
- Generate individual manifests from templates with variable substitution
- Create manifests with SOPS encryption support (.to-sops.yaml naming)
- Generate kustomization.yaml and decrypt-sops.yaml files from collected manifests
- Support for both regular and SOPS-encrypted manifest workflows
"""

import glob
import logging
import os
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from ruamel.yaml import YAML

from opi.core.cluster_config import get_namespace_prefix
from opi.core.config import settings

#: Label the platform puts on a Secret or ConfigMap that must stay OUT of the config-hash
#: the ArgoCD CMP plugin injects as ``checksum/config`` on every pod template.
#:
#: Services never write this themselves: they declare ``include_in_config_hash=False`` on
#: their SecretFileSpec and the shared writer translates that here. The string is also
#: matched by the plugin script in bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml,
#: which is a shell/yq filter and cannot import it -- ``tests/test_config_hash_label.py``
#: pins the two against each other so they cannot drift apart unnoticed.
CONFIG_HASH_IGNORE_LABEL_KEY = "zad.rig/config-hash"
CONFIG_HASH_IGNORE_LABEL_VALUE = "ignore"

logger = logging.getLogger(__name__)


def _resource_identity(doc: Any) -> tuple[str, str, str, str] | None:
    """Kustomize resource identity ``(apiVersion, kind, namespace, name)`` for a manifest doc.

    Returns ``None`` for docs that carry no resource identity (empty docs, lists, or docs
    without a ``kind``/``metadata.name``) - those cannot collide the way kustomize rejects.
    """
    if not isinstance(doc, dict):
        return None
    kind = doc.get("kind")
    metadata = doc.get("metadata")
    if not kind or not isinstance(metadata, dict):
        return None
    name = metadata.get("name")
    if not name:
        return None
    api_version = doc.get("apiVersion", "")
    namespace = metadata.get("namespace", "") or ""
    return (str(api_version), str(kind), str(namespace), str(name))


def check_duplicate_resource_identities(output_dir: str, regular_files: list[str]) -> None:
    """Raise if two on-disk manifests declare the same kustomize resource identity.

    Kustomize refuses to build a directory that lists two resources with the same
    ``(apiVersion, kind, namespace, name)`` - "may not add resource with an already
    registered id". That failure happens inside the ArgoCD CMP and is invisible to OPI, so
    this catches the whole class before the manifests are committed and pushed.

    SOPS files are handled by the caller (they are encrypted and go through a generator);
    entries that are not on disk (e.g. path-rewritten ones) are skipped. A malformed
    manifest is a different failure that kustomize surfaces on its own, so parse errors are
    skipped here rather than masking the real error.

    Raises:
        RuntimeError: When two distinct files declare the same resource identity.
    """
    yaml = YAML(typ="safe")
    seen: dict[tuple[str, str, str, str], str] = {}
    for rel in regular_files:
        path = os.path.join(output_dir, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                docs = list(yaml.load_all(f))
        except Exception as exc:
            logger.debug("Skipping duplicate-identity scan for %s: %s", rel, exc)
            continue
        for doc in docs:
            identity = _resource_identity(doc)
            if identity is None:
                continue
            previous = seen.get(identity)
            if previous is not None and previous != rel:
                api_version, kind, namespace, name = identity
                ns_part = f"{namespace}/" if namespace else ""
                raise RuntimeError(
                    f"Duplicate Kubernetes resource identity in {output_dir}: "
                    f"'{previous}' and '{rel}' both declare {kind}.{api_version} {ns_part}{name}. "
                    "Kustomize cannot render a directory with two resources of the same id. "
                    "This often means a marked-for-deletion manifest was left next to a recreated one."
                )
            seen[identity] = rel


def render_template(template_name: str, variables: dict[str, Any]) -> str:
    """
    Load and render a Jinja2 template from the manifests directory.

    Args:
        template_name: Name of the template file (relative to MANIFESTS_PATH)
        variables: Dictionary of variables for template substitution

    Returns:
        Rendered template as a string

    Raises:
        FileNotFoundError: If template file doesn't exist
        RuntimeError: If template rendering fails
    """
    template_path = os.path.join(settings.MANIFESTS_PATH, template_name)

    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Template file not found: {template_path}")

    with open(template_path) as f:
        template_content = f.read()

    # Create Jinja2 environment with whitespace control
    # Note: autoescape=False is intentional for YAML template generation
    # Use FileSystemLoader to support {% include %} for sidecar templates
    env = Environment(
        loader=FileSystemLoader(settings.MANIFESTS_PATH),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,  # preserve an included partial's final newline so the next line doesn't merge onto it
        autoescape=False,
    )
    template = env.from_string(template_content)

    # Render the template with variables
    result = template.render(**variables)

    logger.debug(f"Successfully rendered template: {template_name}")
    return result


class ManifestGenerator:
    """Generator for Kubernetes manifests with templating and kustomization support."""

    def __init__(self):
        """
        Initialize the ManifestGenerator.

        Args:
            kubectl_connector: Optional kubectl connector for templating (if not provided, basic templating is used)
        """
        logger.debug("ManifestGenerator initialized")

    def template_manifest(self, manifest_content: str, variables: dict[str, Any]) -> str:
        """
        Replace Jinja2 template variables in a manifest.

        This implementation handles Jinja2 variable templating.
        Variables in the manifest should be in the format {{ variable }} or {{ nested.variable }}.

        Args:
            manifest_content: The content of the manifest file
            variables: Dictionary of variables to replace, can include nested dictionaries

        Returns:
            The processed manifest content with variables replaced
        """
        logger.debug(f"Templating manifest with Jinja2 variables: {variables.keys()}")

        try:
            # Create Jinja2 environment with whitespace control
            # trim_blocks removes newlines after block tags
            # lstrip_blocks removes leading whitespace from line start to block tag
            # Use FileSystemLoader to support {% include %} for sidecar templates
            env = Environment(
                loader=FileSystemLoader(settings.MANIFESTS_PATH),
                trim_blocks=True,
                lstrip_blocks=True,
                keep_trailing_newline=True,
            )
            template = env.from_string(manifest_content)

            # Render the template with variables
            result = template.render(**variables)

            logger.debug("Successfully templated manifest with Jinja2")
            return result
        except Exception as e:
            error_msg = f"Error templating manifest with Jinja2: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

    def create_manifest_file(
        self, template_path: str, values: dict[str, Any], output_dir: str, output_filename: str, use_sops: bool = False
    ) -> str:
        """
        Create a single manifest file from a template with variable substitution.

        Args:
            template_path: Path to the manifest template file
            values: Dictionary of values for template substitution
            output_dir: Directory where the manifest should be created (must exist)
            output_filename: Name of the output file (without extension)
            use_sops: If True, creates .to-sops.yaml file for later encryption

        Returns:
            The full path to the created manifest file

        Raises:
            FileNotFoundError: If template file doesn't exist
            RuntimeError: If manifest creation fails
        """
        logger.debug(f"Creating manifest from template: {template_path}")

        try:
            # Check if template exists
            if not os.path.exists(template_path):
                raise FileNotFoundError(f"Template file not found: {template_path}")

            # Read the template
            with open(template_path) as f:
                template_content = f.read()

            # Process the template with values
            processed_manifest = self.template_manifest(template_content, values)

            # Determine output filename based on SOPS usage
            if use_sops:
                final_filename = f"{output_filename}.to-sops.yaml"
                logger.debug(f"Creating SOPS manifest: {final_filename}")
            else:
                final_filename = f"{output_filename}.yaml"
                logger.debug(f"Creating regular manifest: {final_filename}")

            # Ensure output directory exists
            os.makedirs(output_dir, exist_ok=True)

            # Write the manifest file
            output_path = os.path.join(output_dir, final_filename)
            with open(output_path, "w") as f:
                f.write(processed_manifest)

            logger.info(f"Successfully created manifest: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"Error creating manifest file: {e}")
            raise RuntimeError(f"Failed to create manifest: {e}")

    def create_multiple_manifests(self, manifest_configs: list[dict[str, Any]], output_dir: str) -> list[str]:
        """
        Create multiple manifest files from a list of configurations.

        Args:
            manifest_configs: List of dictionaries, each containing:
                - template_path: Path to template file
                - values: Values for substitution
                - output_filename: Output filename (without extension)
                - use_sops: Optional, whether to use SOPS (default False)
            output_dir: Directory where manifests should be created

        Returns:
            List of paths to created manifest files
        """
        logger.info(f"Creating {len(manifest_configs)} manifests in {output_dir}")

        created_files = []

        for config in manifest_configs:
            try:
                template_path = config["template_path"]
                values = config["values"]
                output_filename = config["output_filename"]
                use_sops = config.get("use_sops", False)

                manifest_path = self.create_manifest_file(
                    template_path=template_path,
                    values=values,
                    output_dir=output_dir,
                    output_filename=output_filename,
                    use_sops=use_sops,
                )

                created_files.append(manifest_path)

            except Exception as e:
                logger.error(f"Failed to create manifest from config {config}: {e}")
                # Continue with other manifests even if one fails
                continue

        logger.info(f"Successfully created {len(created_files)} out of {len(manifest_configs)} manifests")
        return created_files

    def collect_manifest_files(
        self, directory: str, include_subfolders: bool = False, project_name: str | None = None
    ) -> tuple[list[str], list[str]]:
        """
        Collect all YAML manifest files in a directory and categorize them.

        Expected folder structure: given_path/project_name/deployment_name/

        Args:
            directory: Directory to scan for manifest files
            include_subfolders: If True, recursively scan subfolders
            project_name: Project name to remove from file paths (required for standard structure,
                         may be None for ArgoCD or other special cases)

        Returns:
            Tuple of (sops_files, regular_files) containing relative paths with project_name removed
        """
        logger.debug(
            f"Collecting manifest files from directory: {directory} (include_subfolders: {include_subfolders})"
        )

        try:
            all_files = []

            if include_subfolders:
                # Recursively find all YAML files in directory and subdirectories
                yaml_pattern = os.path.join(directory, "**", "*.yaml")
                all_files.extend(glob.glob(yaml_pattern, recursive=True))

                # Also find .yml files
                yml_pattern = os.path.join(directory, "**", "*.yml")
                all_files.extend(glob.glob(yml_pattern, recursive=True))
            else:
                # Find YAML files only in the immediate directory
                yaml_pattern = os.path.join(directory, "*.yaml")
                all_files.extend(glob.glob(yaml_pattern))

                # Also find .yml files
                yml_pattern = os.path.join(directory, "*.yml")
                all_files.extend(glob.glob(yml_pattern))

            # Convert to relative paths from the base directory
            base_path = Path(directory)
            relative_files = []
            for file_path in all_files:
                try:
                    rel_path = Path(file_path).relative_to(base_path)
                    relative_files.append(str(rel_path))
                except ValueError:
                    # Skip files that aren't under the base directory
                    continue

            # Separate SOPS files from regular files first
            # Include both .sops.yaml and .to-sops.yaml files as SOPS files
            sops_files = [f for f in relative_files if f.endswith((".sops.yaml", ".to-sops.yaml"))]
            regular_files = [
                f
                for f in relative_files
                if (f.endswith((".yaml", ".yml"))) and not f.endswith(".sops.yaml") and not f.endswith(".to-sops.yaml")
            ]

            # Remove project_name from paths if provided (for structure: given_path/project_name/deployment_name/)
            # TODO: why would we do this?
            if project_name:

                def remove_project_from_path(file_path: str) -> str:
                    """Remove project_name directory from the beginning of the path."""
                    path_parts = file_path.split("/")
                    if len(path_parts) > 1 and path_parts[0] == project_name:
                        # Remove the project_name part and rejoin
                        return "/".join(path_parts[1:])
                    return file_path

                sops_files = [remove_project_from_path(f) for f in sops_files]
                regular_files = [remove_project_from_path(f) for f in regular_files]
                logger.debug(f"Removed project_name '{project_name}' from file paths")

            logger.info(
                f"Found {len(relative_files)} YAML files: {len(sops_files)} SOPS files, {len(regular_files)} regular files"
            )

            return sops_files, regular_files

        except Exception as e:
            logger.error(f"Error collecting manifest files: {e}")
            return [], []

    # TODO: this logic is in the cluster config and should be retrieved from there
    def _determine_namespace_with_prefix(
        self, namespace: str | None, deployment: dict[str, Any] | None = None
    ) -> str | None:
        """
        Determine the namespace with the correct cluster prefix.

        Args:
            namespace: The namespace parameter passed to create_kustomization_files
            deployment: Optional deployment data containing cluster information

        Returns:
            Namespace with correct prefix, or None if namespace is None
        """
        if namespace is None:
            return None

        # If deployment is provided, extract cluster information
        if deployment:
            cluster_name = deployment.get("cluster")
            if not cluster_name:
                return namespace
            try:
                prefix = get_namespace_prefix(cluster_name)

                # Check if namespace already has the correct prefix
                if not namespace.startswith(prefix):
                    logger.debug(f"Adding prefix '{prefix}' to namespace '{namespace}'")
                    return f"{prefix}{namespace}"
                else:
                    logger.debug(f"Namespace '{namespace}' already has correct prefix")
                    return namespace
            except ValueError as e:
                logger.warning(f"Failed to get namespace prefix for cluster '{cluster_name}': {e}")
                return namespace

        # If no deployment context, return namespace as-is
        return namespace

    def create_kustomization_files(
        self,
        output_dir: str,
        namespace: str | None = None,
        sops_files: list[str] | None = None,
        regular_files: list[str] | None = None,
        include_subfolders: bool = False,
        project_name: str | None = None,
        deployment: dict[str, Any] | None = None,
        helm_charts: list[dict[str, Any]] | None = None,
    ) -> bool:
        """
        Create kustomization.yaml and decrypt-sops.yaml files using YAML templates.

        Args:
            output_dir: Directory where kustomization files should be created
            namespace: Optional namespace for the kustomization
            sops_files: Optional list of SOPS files (if None, will be auto-detected)
            regular_files: Optional list of regular files (if None, will be auto-detected)
            include_subfolders: If True, recursively scan subfolders when auto-detecting files
            project_name: If provided, removes this project directory from file paths
            deployment: Optional deployment data containing cluster information for namespace prefixing
            helm_charts: Optional list of helm chart configurations for helmCharts section

        Returns:
            True if files were created successfully, False otherwise
        """
        logger.info(f"Creating kustomization files in directory: {output_dir}")

        try:
            # Auto-detect files if not provided
            if sops_files is None or regular_files is None:
                detected_sops, detected_regular = self.collect_manifest_files(
                    output_dir, include_subfolders, project_name
                )
                sops_files = sops_files or detected_sops
                regular_files = regular_files or detected_regular

            # Apply final exclusion filter right before creating kustomization files
            # This prevents kustomization files from including themselves as resources
            exclude_files = [
                "kustomization.yaml",
                "kustomization.yml",
                "decrypt-sops.yaml",
                "decrypt-sops.yml",
                "generated-configuration.yaml",
            ]
            sops_files = [f for f in sops_files if f not in exclude_files]
            regular_files = [f for f in regular_files if f not in exclude_files]

            # Failsafe before commit/push: two files with the same resource identity make
            # kustomize refuse to build the whole directory, and that failure only surfaces
            # later inside the ArgoCD CMP. Catch the whole class here with a clear message.
            check_duplicate_resource_identities(output_dir, regular_files)

            logger.info(
                f"Kustomization will include {len(sops_files)} SOPS files and {len(regular_files)} regular files"
            )

            # Load kustomization.yaml template
            yaml = YAML()
            kustomization_template_path = os.path.join(settings.MANIFESTS_PATH, "kustomization.yaml.jinja")
            with open(kustomization_template_path) as f:
                kustomization_data = yaml.load(f)

            # Determine namespace with correct prefix
            prefixed_namespace = self._determine_namespace_with_prefix(namespace, deployment)

            # Update kustomization data
            if prefixed_namespace:
                kustomization_data["namespace"] = prefixed_namespace

            # Set resources to regular files
            kustomization_data["resources"] = regular_files or []

            # Handle generators for SOPS files
            if sops_files:
                kustomization_data["generators"] = ["decrypt-sops.yaml"]
            else:
                # Remove generators if no SOPS files
                kustomization_data.pop("generators", None)

            # Add helmCharts section if provided
            if helm_charts:
                kustomization_data["helmCharts"] = helm_charts
                logger.info(f"Added {len(helm_charts)} helm charts to kustomization")

            # Write kustomization.yaml
            kustomization_path = os.path.join(output_dir, "kustomization.yaml")
            with open(kustomization_path, "w") as f:
                yaml.dump(kustomization_data, f)

            logger.info(f"Created kustomization.yaml: {kustomization_path}")

            # Create decrypt-sops.yaml if there are SOPS files
            if sops_files:
                decrypt_sops_template_path = os.path.join(settings.MANIFESTS_PATH, "decrypt-sops.yaml.jinja")
                with open(decrypt_sops_template_path) as f:
                    decrypt_sops_data = yaml.load(f)

                # Convert .to-sops.yaml files to .sops.yaml for decrypt configuration
                # The decrypt-sops.yaml needs to reference the final encrypted filenames
                decrypt_files = []
                for f in sops_files:
                    if f.endswith(".to-sops.yaml"):
                        # Convert .to-sops.yaml to .sops.yaml for decrypt configuration
                        decrypt_files.append(f.replace(".to-sops.yaml", ".sops.yaml"))
                    else:
                        # Keep .sops.yaml files as-is
                        decrypt_files.append(f)

                # Remove duplicates that can occur when both .to-sops.yaml and .sops.yaml exist
                decrypt_files = list(dict.fromkeys(decrypt_files))

                # Update files list
                decrypt_sops_data["files"] = decrypt_files

                # Write decrypt-sops.yaml
                decrypt_sops_path = os.path.join(output_dir, "decrypt-sops.yaml")
                with open(decrypt_sops_path, "w") as f:
                    yaml.dump(decrypt_sops_data, f)

                logger.info(f"Created decrypt-sops.yaml: {decrypt_sops_path}")

            return True

        except RuntimeError:
            # A duplicate resource identity is a real, actionable error - let it propagate
            # so the deploy stops before the broken kustomization is committed, rather than
            # being swallowed into a False return.
            raise
        except Exception as e:
            logger.error(f"Error creating kustomization files: {e}")
            return False

    def generate_manifests_with_kustomization(
        self, manifest_configs: list[dict[str, Any]], output_dir: str, namespace: str | None = None
    ) -> bool:
        """
        Generate multiple manifests and create kustomization files in one operation.

        Args:
            manifest_configs: List of manifest configurations
            output_dir: Directory where all files should be created
            namespace: Optional namespace for kustomization

        Returns:
            True if all operations succeeded, False otherwise
        """
        logger.info(f"Generating manifests with kustomization in {output_dir}")

        try:
            # Create all manifests
            created_files = self.create_multiple_manifests(manifest_configs, output_dir)

            if not created_files:
                logger.warning("No manifests were created")
                return False

            # Create kustomization files
            kustomization_result = self.create_kustomization_files(output_dir, namespace)

            if not kustomization_result:
                logger.error("Failed to create kustomization files")
                return False

            logger.info(f"Successfully generated {len(created_files)} manifests with kustomization")
            return True

        except Exception as e:
            logger.error(f"Error in generate_manifests_with_kustomization: {e}")
            return False


def create_manifest_generator(kubectl_connector=None) -> ManifestGenerator:
    """
    Create and return a ManifestGenerator instance.

    Args:
        kubectl_connector: Optional kubectl connector for advanced templating

    Returns:
        ManifestGenerator instance
    """
    logger.debug("Creating ManifestGenerator")
    return ManifestGenerator()
