"""Manifest generation: render Jinja templates into Kubernetes manifests and the
kustomization that lists them, with support for SOPS-encrypted files (``.to-sops.yaml``).
"""

import glob
import logging
import os
import re
from pathlib import Path
from typing import Any

from ruamel.yaml.error import YAMLError

from opi.core.cluster_config import get_namespace_prefix
from opi.core.config import settings
from opi.utils.manifest_render import compiled_template, render_template_source
from opi.utils.sops import SOPS_SUFFIX, TO_SOPS_SUFFIX, sops_filenames
from opi.utils.yaml_util import (
    dump_yaml_for_go,
    load_yaml_documents_safe,
    load_yaml_round_trip,
)

#: Absolute directory of the Jinja manifest templates, derived from this file and so
#: independent of the cwd and of the caller's depth. ``render_template`` resolves via settings.
MANIFEST_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "manifests")

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

#: Variables the CMP plugin reads or sets itself. A helmfile deployment may not pass them
#: through .cmp-env: that would let it steer the namespace, the key path or the plugin's
#: render. The same list sits as a case pattern in the plugin script in
#: bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml, which is shell and cannot
#: import it -- ``tests/test_cmp_env_contract.py`` pins the two against each other.
CMP_ENV_RESERVED_PREFIXES = ("ARGOCD_", "SOPS_", "KUSTOMIZE_", "HELM_", "HELMFILE_", "KUBE", "BASH", "LD_", "GIT_")
CMP_ENV_RESERVED_NAMES = frozenset({"PATH", "HOME", "IFS", "ENV", "SHELL", "PS4", "PROMPT_COMMAND"})

#: The same key shape as component env vars in opi/schemas/project_v2.json.
CMP_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")

logger = logging.getLogger(__name__)


def cmp_env_rejection(key: str, value: str) -> str | None:
    """Why this pair may not go into ``.cmp-env``, or ``None`` when it may.

    Refuses exactly what the parser in the plugin script refuses, so a mistake shows up on
    save and not only at render time. The message never names the value: that is tenant
    configuration and may be a secret.
    """
    if not CMP_ENV_KEY_PATTERN.match(key):
        return f"'{key}' is not a valid environment variable name"
    if key in CMP_ENV_RESERVED_NAMES or key.startswith(CMP_ENV_RESERVED_PREFIXES):
        return f"'{key}' is reserved by the CMP plugin"
    if any(character in value for character in "\r\n\0"):
        return f"the value of '{key}' contains a newline or NUL byte"
    return None


def _resource_identity(doc: Any) -> tuple[str, str, str, str] | None:  # noqa: ANN401  # a parsed manifest document
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
    seen: dict[tuple[str, str, str, str], str] = {}
    for rel in regular_files:
        path = os.path.join(output_dir, rel)
        if not os.path.isfile(path):
            continue
        try:
            docs = load_yaml_documents_safe(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, YAMLError) as exc:
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
    """Load and render a Jinja2 template, named relative to ``MANIFESTS_PATH``.

    Raises:
        FileNotFoundError: If template file doesn't exist
        RuntimeError: If template rendering fails
    """
    template_path = os.path.join(settings.MANIFESTS_PATH, template_name)

    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Template file not found: {template_path}")

    with open(template_path) as f:
        template_content = f.read()

    result = compiled_template(template_content).render(**variables)

    logger.debug(f"Successfully rendered template: {template_name}")
    return result


class ManifestGenerator:
    """Generator for Kubernetes manifests with templating and kustomization support."""

    def __init__(self) -> None:
        logger.debug("ManifestGenerator initialized")

    def template_manifest(self, manifest_content: str, variables: dict[str, Any]) -> str:
        """Render the Jinja2 variables in a manifest; raises ``RuntimeError`` when it fails."""
        logger.debug(f"Templating manifest with Jinja2 variables: {variables.keys()}")

        try:
            result = render_template_source(manifest_content, variables)

            logger.debug("Successfully templated manifest with Jinja2")
            return result
        except Exception as e:
            error_msg = f"Error templating manifest with Jinja2: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    def create_manifest_file(
        self, template_path: str, values: dict[str, Any], output_dir: str, output_filename: str, use_sops: bool = False
    ) -> str:
        """Create one manifest file from a template, and return its full path.

        ``output_filename`` carries no extension: ``use_sops`` decides between
        ``.to-sops.yaml`` (encrypted later) and plain ``.yaml``.

        Raises:
            FileNotFoundError: If template file doesn't exist
            RuntimeError: If manifest creation fails
        """
        logger.debug(f"Creating manifest from template: {template_path}")

        try:
            if not os.path.exists(template_path):
                raise FileNotFoundError(f"Template file not found: {template_path}")

            with open(template_path) as f:
                template_content = f.read()

            processed_manifest = self.template_manifest(template_content, values)

            if use_sops:
                final_filename = sops_filenames(output_filename).plaintext
                logger.debug(f"Creating SOPS manifest: {final_filename}")
            else:
                final_filename = f"{output_filename}.yaml"
                logger.debug(f"Creating regular manifest: {final_filename}")

            os.makedirs(output_dir, exist_ok=True)

            output_path = os.path.join(output_dir, final_filename)
            with open(output_path, "w") as f:
                f.write(processed_manifest)

            logger.info(f"Successfully created manifest: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"Error creating manifest file: {e}")
            raise RuntimeError(f"Failed to create manifest: {e}") from e

    def create_multiple_manifests(self, manifest_configs: list[dict[str, Any]], output_dir: str) -> list[str]:
        """Create several manifest files, and return the paths of the ones that succeeded.

        Each entry of ``manifest_configs`` holds ``template_path``, ``values``,
        ``output_filename`` and optionally ``use_sops``. A config that fails is logged and
        skipped; the others are still created.
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

            except (OSError, RuntimeError) as e:
                logger.error(f"Failed to create manifest from config {config}: {e}")
                continue

        logger.info(f"Successfully created {len(created_files)} out of {len(manifest_configs)} manifests")
        return created_files

    def collect_manifest_files(
        self, directory: str, include_subfolders: bool = False, project_name: str | None = None
    ) -> tuple[list[str], list[str]]:
        """Collect the YAML manifests in a directory as ``(sops_files, regular_files)``.

        Expected folder structure: given_path/project_name/deployment_name/. ``project_name``
        is stripped from the returned relative paths; it may be ``None`` for ArgoCD and other
        special cases that do not have that level.
        """
        logger.debug(
            f"Collecting manifest files from directory: {directory} (include_subfolders: {include_subfolders})"
        )

        try:
            all_files = []

            if include_subfolders:
                yaml_pattern = os.path.join(directory, "**", "*.yaml")
                all_files.extend(glob.glob(yaml_pattern, recursive=True))

                yml_pattern = os.path.join(directory, "**", "*.yml")
                all_files.extend(glob.glob(yml_pattern, recursive=True))
            else:
                yaml_pattern = os.path.join(directory, "*.yaml")
                all_files.extend(glob.glob(yaml_pattern))

                yml_pattern = os.path.join(directory, "*.yml")
                all_files.extend(glob.glob(yml_pattern))

            base_path = Path(directory)
            relative_files = []
            # Sorted: glob follows the directory order, which differs per checkout and
            # reshuffled the kustomization resources on every run.
            for file_path in sorted(all_files):
                try:
                    rel_path = Path(file_path).relative_to(base_path)
                    relative_files.append(str(rel_path))
                except ValueError:
                    # Not under the base directory, so it has no relative path here.
                    continue

            sops_files = [f for f in relative_files if f.endswith((SOPS_SUFFIX, TO_SOPS_SUFFIX))]
            regular_files = [
                f
                for f in relative_files
                if f.endswith((".yaml", ".yml")) and not f.endswith((SOPS_SUFFIX, TO_SOPS_SUFFIX))
            ]

            if project_name:

                def remove_project_from_path(file_path: str) -> str:
                    path_parts = file_path.split("/")
                    if len(path_parts) > 1 and path_parts[0] == project_name:
                        return "/".join(path_parts[1:])
                    return file_path

                sops_files = [remove_project_from_path(f) for f in sops_files]
                regular_files = [remove_project_from_path(f) for f in regular_files]
                logger.debug(f"Removed project_name '{project_name}' from file paths")

            logger.info(
                f"Found {len(relative_files)} YAML files: {len(sops_files)} SOPS files, {len(regular_files)} regular files"
            )

            return sops_files, regular_files

        except (OSError, ValueError) as e:
            logger.error(f"Error collecting manifest files: {e}")
            return [], []

    def _determine_namespace_with_prefix(
        self, namespace: str | None, deployment: dict[str, Any] | None = None
    ) -> str | None:
        """The namespace with its cluster prefix; unchanged when ``deployment`` names no cluster."""
        if namespace is None:
            return None

        if deployment:
            cluster_name = deployment.get("cluster")
            if not cluster_name:
                return namespace
            try:
                prefix = get_namespace_prefix(cluster_name)

                if not namespace.startswith(prefix):
                    logger.debug(f"Adding prefix '{prefix}' to namespace '{namespace}'")
                    return f"{prefix}{namespace}"
                else:
                    logger.debug(f"Namespace '{namespace}' already has correct prefix")
                    return namespace
            except ValueError as e:
                logger.warning(f"Failed to get namespace prefix for cluster '{cluster_name}': {e}")
                return namespace

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
        """Create kustomization.yaml and, when there are SOPS files, decrypt-sops.yaml.

        ``sops_files`` and ``regular_files`` are auto-detected from ``output_dir`` when they
        are ``None``; ``include_subfolders`` and ``project_name`` only steer that detection.

        Returns False on failure, except for a duplicate resource identity: that raises, so
        the deploy stops before the broken kustomization is committed.
        """
        logger.info(f"Creating kustomization files in directory: {output_dir}")

        try:
            if sops_files is None or regular_files is None:
                detected_sops, detected_regular = self.collect_manifest_files(
                    output_dir, include_subfolders, project_name
                )
                sops_files = sops_files or detected_sops
                regular_files = regular_files or detected_regular

            # Right before creating them, so a kustomization file never lists itself as a
            # resource.
            exclude_files = [
                "kustomization.yaml",
                "kustomization.yml",
                "decrypt-sops.yaml",
                "decrypt-sops.yml",
                "generated-configuration.yaml",
            ]
            sops_files = [f for f in sops_files if f not in exclude_files]
            regular_files = [f for f in regular_files if f not in exclude_files]

            # Before commit/push: two files with the same resource identity make kustomize
            # refuse to build the whole directory, and that failure only surfaces later
            # inside the ArgoCD CMP.
            check_duplicate_resource_identities(output_dir, regular_files)

            logger.info(
                f"Kustomization will include {len(sops_files)} SOPS files and {len(regular_files)} regular files"
            )

            kustomization_template_path = os.path.join(settings.MANIFESTS_PATH, "kustomization.yaml.jinja")
            kustomization_data = load_yaml_round_trip(Path(kustomization_template_path).read_text(encoding="utf-8"))

            prefixed_namespace = self._determine_namespace_with_prefix(namespace, deployment)

            if prefixed_namespace:
                kustomization_data["namespace"] = prefixed_namespace

            kustomization_data["resources"] = regular_files or []

            if sops_files:
                kustomization_data["generators"] = ["decrypt-sops.yaml"]
            else:
                kustomization_data.pop("generators", None)

            if helm_charts:
                kustomization_data["helmCharts"] = helm_charts
                logger.info(f"Added {len(helm_charts)} helm charts to kustomization")

            kustomization_path = os.path.join(output_dir, "kustomization.yaml")
            Path(kustomization_path).write_text(dump_yaml_for_go(kustomization_data), encoding="utf-8")

            logger.info(f"Created kustomization.yaml: {kustomization_path}")

            # Create decrypt-sops.yaml if there are SOPS files
            if sops_files:
                decrypt_sops_template_path = os.path.join(settings.MANIFESTS_PATH, "decrypt-sops.yaml.jinja")
                decrypt_sops_data = load_yaml_round_trip(Path(decrypt_sops_template_path).read_text(encoding="utf-8"))

                # decrypt-sops.yaml lists the names the files carry after encryption, so both
                # halves of one pair collapse onto the same name, hence the deduplication.
                decrypt_files = list(dict.fromkeys(sops_filenames(f).encrypted for f in sops_files))

                decrypt_sops_data["files"] = decrypt_files

                decrypt_sops_path = os.path.join(output_dir, "decrypt-sops.yaml")
                Path(decrypt_sops_path).write_text(dump_yaml_for_go(decrypt_sops_data), encoding="utf-8")

                logger.info(f"Created decrypt-sops.yaml: {decrypt_sops_path}")

            return True

        except RuntimeError:
            # A duplicate resource identity must not be swallowed into a False return: the
            # deploy has to stop before the broken kustomization is committed.
            raise
        except (OSError, ValueError) as e:
            logger.error(f"Error creating kustomization files: {e}")
            return False

    def generate_manifests_with_kustomization(
        self, manifest_configs: list[dict[str, Any]], output_dir: str, namespace: str | None = None
    ) -> bool:
        """Create the manifests and their kustomization files in one go; False on any failure."""
        logger.info(f"Generating manifests with kustomization in {output_dir}")

        try:
            created_files = self.create_multiple_manifests(manifest_configs, output_dir)

            if not created_files:
                logger.warning("No manifests were created")
                return False

            kustomization_result = self.create_kustomization_files(output_dir, namespace)

            if not kustomization_result:
                logger.error("Failed to create kustomization files")
                return False

            logger.info(f"Successfully generated {len(created_files)} manifests with kustomization")
            return True

        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f"Error in generate_manifests_with_kustomization: {e}")
            return False
