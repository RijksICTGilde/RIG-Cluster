"""Rendering a deployment that brings its own helm charts or a helmfile."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import tempfile
from typing import TYPE_CHECKING, Any, Protocol

from opi.connectors.git import GitConnector
from opi.core.cluster_config import (
    get_external_dns_target_for_hostname,
    get_ingress_cluster_issuer,
    get_ingress_postfix,
    get_ingress_tls_enabled,
    get_prefixed_namespace,
)
from opi.core.config import settings
from opi.generation.manifests import MANIFEST_TEMPLATES_DIR, cmp_env_rejection
from opi.manager.project.emitters import emit_letsencrypt_issuer
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.catalog.publish_on_web.issuer import effective_issuer
from opi.services.secrets import DatabaseSecret, KeycloakSecret, MinIOSecret, RedisSecret
from opi.utils.age import (
    carries_encrypted_value,
    decrypt_password_smart,
    get_project_public_key,
    has_encrypted_marker,
    project_private_key_or_none,
)
from opi.utils.fs_paths import reject_escaping_symlinks, safe_join, single_segment
from opi.utils.naming import (
    generate_external_hostname,
    generate_helm_values_filename,
    generate_issuer_name,
    generate_public_url,
)
from opi.utils.render_sources import path_inside_clone, require_allowed_render_source
from opi.utils.sops import (
    CMP_ENV_NAME,
    CMP_ENV_PLAINTEXT_FILENAME,
    TO_SOPS_SUFFIX,
    encrypt_to_sops_files_or_fail,
    sops_filenames,
)
from opi.utils.yaml_util import dump_yaml_for_go, dump_yaml_for_go_unfolded, load_yaml_round_trip

if TYPE_CHECKING:
    from opi.connectors.git_repo import GitRepoWorkspace
    from opi.generation.manifests import ManifestGenerator
    from opi.services.project_file_handler import ProjectFileHandler
    from opi.services.secrets import BaseSecret

logger = logging.getLogger(__name__)

# Filenames the render chain gives a meaning of its own: a helmfile source that brings one
# along would overwrite the generated deployment.
HELMFILE_RESERVED_FILENAMES = frozenset(
    {
        "kustomization.yaml",
        "kustomization.yml",
        "decrypt-sops.yaml",
        CMP_ENV_PLAINTEXT_FILENAME,
        ".helmfile-entry",
        *sops_filenames("values"),
        *sops_filenames(CMP_ENV_NAME),
    }
)

_EXTERNAL_DNS_TARGET_ANNOTATION = "external-dns.alpha.kubernetes.io/target"


def _clear_destination(path: str) -> None:
    """Remove ``path`` so a copy that preserves symlinks does not trip over it.

    ``os.symlink`` and a nested symlink in ``copytree`` refuse on an existing destination,
    and on a second rollout the previous round is still there.
    """
    if not os.path.lexists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def deployment_uses_helm_charts(deployment: dict[str, Any]) -> bool:
    """Whether this deployment declares helm-charts instead of components."""
    helm_charts = deployment.get("helm-charts", [])
    return len(helm_charts) > 0


def deployment_uses_helmfile(deployment: dict[str, Any]) -> bool:
    """Whether this deployment declares a helmfile instead of components or helm-charts."""
    helmfiles = deployment.get("helmfile", [])
    return len(helmfiles) > 0


def resolve_nested_aliases(obj: Any, context: dict[str, Any]) -> Any:  # noqa: ANN401  # a project-file value
    """Resolve ``$VAR`` and ``${VAR}`` references in a nested structure.

    A string that is exactly one reference becomes the context value with its own type; a
    string that only contains references becomes a string.
    """
    if isinstance(obj, str):
        # This preserves types (e.g., integers) instead of converting to string
        for key, value in context.items():
            if obj == f"${key}" or obj == f"${{{key}}}":
                return value
        result = obj
        for key, value in context.items():
            result = result.replace(f"${key}", str(value))
            result = result.replace(f"${{{key}}}", str(value))
        return result
    elif isinstance(obj, dict):
        return {k: resolve_nested_aliases(v, context) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [resolve_nested_aliases(item, context) for item in obj]
    return obj


def deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` into ``base`` recursively, ``override`` winning per key."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


async def clone_helm_chart(
    helm_chart: dict[str, Any],
    target_dir: str,
) -> str:
    """Clone a helm chart into ``target_dir/charts`` and return the path to the chart.

    A ``file://`` dependency from Chart.yaml is copied next to the chart and its
    ``repository`` rewritten to ``file://../<name>``, the form kustomize resolves.
    """
    source_type = helm_chart.get("source-type", "git-clone")

    if source_type != "git-clone":
        raise ValueError(f"Unsupported helm chart source-type: {source_type}")

    git_url = helm_chart.get("git-url")
    git_ref = helm_chart.get("git-ref", "main")
    chart_path = helm_chart.get("chart-path", ".")
    chart_name = helm_chart.get("name")

    if not git_url:
        raise ValueError(f"Helm chart '{chart_name}' missing git-url")
    if not chart_name:
        raise ValueError("Helm chart missing required 'name' field")

    require_allowed_render_source(git_url, f"helm chart '{chart_name}'")

    logger.info(f"Cloning helm chart '{chart_name}' from {git_url}#{git_ref}")

    dest_chart_path = ""

    with tempfile.TemporaryDirectory() as temp_dir:
        git_connector = GitConnector(
            repo_url=git_url,
            branch=git_ref,
            working_dir=temp_dir,
        )

        await git_connector.clone()

        source_chart_path = path_inside_clone(temp_dir, chart_path, f"helm chart '{chart_name}'")

        if not os.path.exists(source_chart_path):
            raise FileNotFoundError(f"Chart path not found: {chart_path} in {git_url}")

        reject_escaping_symlinks(source_chart_path)

        charts_dir = os.path.join(target_dir, "charts")
        os.makedirs(charts_dir, exist_ok=True)

        dest_chart_path = safe_join(charts_dir, single_segment(chart_name, "helm-charts[].name"))
        if os.path.exists(dest_chart_path):
            await asyncio.to_thread(shutil.rmtree, dest_chart_path)
        await asyncio.to_thread(shutil.copytree, source_chart_path, dest_chart_path, symlinks=True)

        logger.info(f"Copied helm chart '{chart_name}' to {dest_chart_path}")

        chart_yaml_path = os.path.join(dest_chart_path, "Chart.yaml")
        if os.path.exists(chart_yaml_path):
            with open(chart_yaml_path) as f:
                chart_data = load_yaml_round_trip(f.read())

            dependencies = chart_data.get("dependencies", [])
            for dep in dependencies:
                dep_name = dep.get("name")
                dep_repo = dep.get("repository", "")

                if dep_repo.startswith("file://"):
                    rel_path = dep_repo.replace("file://", "")
                    dep_source = path_inside_clone(
                        temp_dir,
                        os.path.join(chart_path, rel_path),
                        f"dependency '{dep_name}' of helm chart '{chart_name}'",
                    )

                    if os.path.exists(dep_source):
                        reject_escaping_symlinks(dep_source)
                        dep_dest = safe_join(charts_dir, single_segment(dep_name, "Chart.yaml dependencies[].name"))
                        if os.path.exists(dep_dest):
                            await asyncio.to_thread(shutil.rmtree, dep_dest)
                        await asyncio.to_thread(shutil.copytree, dep_source, dep_dest, symlinks=True)

                        dep["repository"] = f"file://../{dep_name}"
                        logger.info(f"Copied dependency chart '{dep_name}' to {dep_dest}")

            with open(chart_yaml_path, "w") as f:
                f.write(dump_yaml_for_go(chart_data))

        await git_connector.close()

    return dest_chart_path


def write_helmfile_custom_files(
    helmfile_def: dict[str, Any],
    helmfile_ref: dict[str, Any],
    target_path: str,
) -> list[str]:
    """Write the custom files of a helmfile deployment; returns the filenames written.

    The files of the deployment reference override those of the definition by name. A name
    in ``HELMFILE_RESERVED_FILENAMES`` is refused: it would overwrite what the render
    chain generates.
    """
    written_files: list[str] = []

    base_files = helmfile_def.get("files", {}) or {}
    deployment_files = helmfile_ref.get("files", {}) or {}

    all_files = {**base_files, **deployment_files}

    if not all_files:
        return written_files

    logger.info(f"Writing {len(all_files)} custom file(s) to {target_path}")

    for filename, content in all_files.items():
        if not isinstance(content, str):
            logger.warning(f"Skipping file '{filename}': content must be a string")
            continue

        # Security: prevent path traversal
        if ".." in filename or filename.startswith("/"):
            logger.warning(f"Skipping file '{filename}': path traversal not allowed")
            continue

        reserved = [part for part in filename.split("/") if part in HELMFILE_RESERVED_FILENAMES]
        if reserved:
            raise ValueError(
                f"Bestand '{filename}' mag niet: '{reserved[0]}' is voorbehouden aan de renderketen "
                "en zou de gegenereerde deployment overschrijven."
            )

        file_path = os.path.join(target_path, filename)

        file_dir = os.path.dirname(file_path)
        if file_dir and file_dir != target_path:
            os.makedirs(file_dir, exist_ok=True)

        with open(file_path, "w") as f:
            f.write(content)

        written_files.append(filename)
        logger.info(f"  Created custom file: {filename}")

    return written_files


async def clone_helmfile_source(
    helmfile_def: dict[str, Any],
    target_path: str,
) -> tuple[str, str]:
    """Clone a helmfile source into ``target_path``; returns that path and the entry point.

    The entry point is the subdirectory holding the helmfile to execute, empty when the
    definition names none.
    """
    helmfile_name = helmfile_def.get("name", "unknown")
    source_url = helmfile_def.get("url")
    source_ref = helmfile_def.get("ref", "main")
    source_path = helmfile_def.get("path", "")
    entry_path = helmfile_def.get("entry", "")  # Subdirectory containing the helmfile

    if not source_url:
        raise ValueError(f"Helmfile '{helmfile_name}' missing required 'url' field")

    require_allowed_render_source(source_url, f"helmfile '{helmfile_name}'")

    with tempfile.TemporaryDirectory() as temp_dir:
        git_connector = GitConnector(
            repo_url=source_url,
            branch=source_ref,
            working_dir=temp_dir,
        )
        await git_connector.clone()

        full_source_path = path_inside_clone(temp_dir, source_path, f"helmfile '{helmfile_name}'")

        if not os.path.exists(full_source_path):
            await git_connector.close()
            raise ValueError(f"Helmfile path '{source_path}' not found in repository for '{helmfile_name}'")

        reject_escaping_symlinks(full_source_path)

        if entry_path:
            full_entry_path = path_inside_clone(full_source_path, entry_path, f"helmfile '{helmfile_name}'")
            if not os.path.exists(full_entry_path):
                await git_connector.close()
                raise ValueError(
                    f"Helmfile entry '{entry_path}' not found within path '{source_path}' for '{helmfile_name}'"
                )

        dest_helmfile_path = target_path
        os.makedirs(dest_helmfile_path, exist_ok=True)

        for item in os.listdir(full_source_path):
            if item == ".git":
                continue
            src_item = os.path.join(full_source_path, item)
            dst_item = os.path.join(dest_helmfile_path, item)
            await asyncio.to_thread(_clear_destination, dst_item)
            if os.path.isdir(src_item) and not os.path.islink(src_item):
                await asyncio.to_thread(shutil.copytree, src_item, dst_item, symlinks=True)
            else:
                await asyncio.to_thread(shutil.copy2, src_item, dst_item, follow_symlinks=False)

        logger.info(f"Copied helmfile source from {full_source_path} to {dest_helmfile_path}")
        if entry_path:
            logger.info(f"Helmfile entry point: {entry_path}")

        await git_connector.close()

    return dest_helmfile_path, entry_path


class HelmDeploymentAccess(Protocol):
    """What the helm rendering reaches from its host: the project file and the secrets map."""

    _manifest_generator: ManifestGenerator
    _project_file_handler: ProjectFileHandler

    async def get_contents(self, *, record_base: bool = True) -> dict[str, Any]: ...

    async def get_deployment_by_name(self, deployment_name: str) -> dict[str, Any] | None: ...

    def _get_secret_from_map[T: BaseSecret](
        self, deployment_name: str, secret_type: str, secret_class: type[T] | None = None
    ) -> T | None: ...


class HelmDeployment:
    """The helm and helmfile rendering of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: HelmDeploymentAccess) -> None:
        self._host = host

    async def get_helm_values_context(self, deployment_name: str) -> dict[str, Any]:
        """The alias-resolution context of a deployment: service credentials plus its web address.

        A port stays an ``int``, so a value that is exactly one reference keeps that type.
        """
        context: dict[str, Any] = {}

        deployment = await self._host.get_deployment_by_name(deployment_name)
        if not deployment:
            logger.warning(f"Deployment '{deployment_name}' not found")
            return context

        cluster_name = deployment.get("cluster", settings.CLUSTER_MANAGER)
        subdomain = get_domain_setting(deployment, DomainSetting.SUBDOMAIN)
        base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
        # record_base=False: a projection that only needs the project's domain block, so it
        # must not become the compare-and-swap base of whoever saves next.
        issuer_config = effective_issuer(await self._host.get_contents(record_base=False), deployment, cluster_name)
        use_https = get_ingress_tls_enabled(cluster_name)

        if base_domain and subdomain:
            hostname = generate_external_hostname(subdomain, base_domain)
        elif subdomain:
            ingress_postfix = get_ingress_postfix(cluster_name)
            hostname = f"{subdomain}.{ingress_postfix}"
        else:
            ingress_postfix = get_ingress_postfix(cluster_name)
            hostname = f"{deployment_name}.{ingress_postfix}"

        if issuer_config:
            if issuer_config in ("letsencrypt", "letsencrypt-staging") and base_domain:
                context["ISSUER"] = generate_issuer_name(base_domain, issuer_config, deployment_name)
            else:
                context["ISSUER"] = issuer_config
        else:
            cluster_issuer = get_ingress_cluster_issuer(cluster_name)
            if cluster_issuer:
                context["ISSUER"] = cluster_issuer

        public_url = generate_public_url(hostname, use_https)
        context["PUBLIC_HOST"] = public_url
        context["PUBLIC_HOSTNAME"] = hostname
        context["HOSTNAME"] = hostname
        if subdomain:
            context["SUBDOMAIN"] = subdomain
        if base_domain:
            context["BASE_DOMAIN"] = base_domain

        logger.debug(f"Added deployment variables: PUBLIC_HOST={public_url}, HOSTNAME={hostname}")

        db_secret = self._host._get_secret_from_map(deployment_name, "database", DatabaseSecret)
        if db_secret:
            context["DATABASE_SERVER_HOST"] = db_secret.host
            # Stays an int, so the YAML keeps its type.
            context["DATABASE_SERVER_PORT"] = db_secret.port
            context["DATABASE_SERVER_USER"] = db_secret.username
            context["DATABASE_PASSWORD"] = db_secret.password
            context["DATABASE_DB"] = db_secret.database
            context["DATABASE_SCHEMA"] = db_secret.schema

        minio_secret = self._host._get_secret_from_map(deployment_name, "minio", MinIOSecret)
        if minio_secret:
            context["OBJECT_STORE_HOST"] = minio_secret.host
            # Stays an int, so the YAML keeps its type.
            context["OBJECT_STORE_PORT"] = minio_secret.port
            context["OBJECT_STORE_URL"] = minio_secret.url
            context["OBJECT_STORE_ENDPOINT_URL"] = minio_secret.endpoint_url
            context["OBJECT_STORE_USER"] = minio_secret.access_key
            context["OBJECT_STORE_PASSWORD"] = minio_secret.secret_key
            context["OBJECT_STORE_BUCKET_NAME"] = minio_secret.bucket_name
            context["OBJECT_STORE_REGION"] = minio_secret.region

        keycloak_secret = self._host._get_secret_from_map(deployment_name, "keycloak", KeycloakSecret)
        if keycloak_secret:
            context["OIDC_DISCOVERY_URL"] = keycloak_secret.discovery_url
            context["OIDC_CLIENT_ID"] = keycloak_secret.client_id
            context["OIDC_CLIENT_SECRET"] = keycloak_secret.client_secret
            context["KEYCLOAK_BASE_URL"] = keycloak_secret.base_url
            context["KEYCLOAK_REALM"] = keycloak_secret.realm

        redis_secret = self._host._get_secret_from_map(deployment_name, "redis", RedisSecret)
        if redis_secret:
            context["REDIS_HOST"] = redis_secret.host
            # Stays an int, so the YAML keeps its type.
            context["REDIS_PORT"] = redis_secret.port
            context["REDIS_PASSWORD"] = redis_secret.password
            context["REDIS_USERNAME"] = redis_secret.username
            context["REDIS_URL"] = redis_secret.url

        logger.debug(f"Built helm values context with {len(context)} variables for deployment '{deployment_name}'")
        return context

    async def process_helm_chart_deployment(
        self,
        deployment: dict[str, Any],
        git_connector: GitRepoWorkspace,
        target_path: str,
        namespace_policy_files: list[str] | None = None,
    ) -> None:
        """Render a helm-charts deployment into ``target_path``.

        The values are written as ``*.to-sops.yaml`` and encrypted at the end of this call, so
        an exception halfway leaves plaintext behind that must not reach git.
        """
        project_data = await self._host.get_contents()
        deployment_name = deployment.get("name")
        if not deployment_name:
            raise ValueError("Deployment missing required 'name' field")

        cluster_name = deployment.get("cluster", settings.CLUSTER_MANAGER)
        prefixed_namespace = get_prefixed_namespace(cluster_name, deployment["namespace"])

        logger.info(f"Processing helm chart deployment: {deployment_name}")

        os.makedirs(target_path, exist_ok=True)

        helm_chart_refs = deployment.get("helm-charts", [])

        context = await self.get_helm_values_context(deployment_name)

        # For the helmCharts section of kustomization.yaml.
        helm_charts_config: list[dict[str, Any]] = []

        for helm_chart_ref in helm_chart_refs:
            chart_reference = helm_chart_ref.get("reference")
            if not chart_reference:
                raise ValueError("Helm chart reference missing required 'reference' field")

            release_name = helm_chart_ref.get("release-name", chart_reference)

            logger.info(f"Processing helm chart reference: {chart_reference}")

            helm_chart_def = self._host._project_file_handler.get_helm_chart_by_name(project_data, chart_reference)
            if not helm_chart_def:
                raise ValueError(f"Helm chart '{chart_reference}' not found in project definition")

            chart_path = await clone_helm_chart(helm_chart_def, target_path)

            base_values = await self._host._project_file_handler.extract_helm_chart_values(
                project_data, chart_reference
            )

            deployment_values = await self._host._project_file_handler.extract_deployment_helm_chart_values(
                project_data, deployment_name, chart_reference
            )

            merged_values = deep_merge_dicts(base_values, deployment_values)

            resolved_values = resolve_nested_aliases(merged_values, context)

            # Use naming convention that matches CMP plugin pattern: *-helm-values.sops.yaml
            values_file_sops = generate_helm_values_filename(deployment_name, chart_reference, encrypted=True)
            values_file_to_sops = sops_filenames(values_file_sops).plaintext
            values_path = os.path.join(target_path, values_file_to_sops)

            with open(values_path, "w") as f:
                f.write(dump_yaml_for_go(resolved_values))

            logger.info(f"Created helm values file (to be encrypted): {values_path}")

            # Reference the final .sops.yaml name (after encryption)
            helm_charts_config.append(
                {
                    "name": chart_reference,
                    "releaseName": release_name,
                    "namespace": prefixed_namespace,
                    "valuesFile": values_file_sops,
                    "repo": f"file://./charts/{chart_reference}",
                }
            )

        secret_files = await self.create_deployment_secrets(
            deployment_name,
            target_path,
            prefixed_namespace,
            cluster_name,
        )
        logger.info(f"Created {len(secret_files)} service secret manifests for helm deployment")

        # The namespace-wide policies are already on disk; they only still have to reach the
        # kustomization, which is built here from a list and not from a directory scan.
        regular_files: list[str] = list(namespace_policy_files or [])

        issuer_config = effective_issuer(project_data, deployment, cluster_name)
        base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)

        # An ``issuer_config`` that already carries a domain suffix is used as it stands, so
        # only the bare "letsencrypt" and "letsencrypt-staging" generate an Issuer.
        if issuer_config and issuer_config in ("letsencrypt", "letsencrypt-staging") and base_domain:
            regular_files.extend(
                emit_letsencrypt_issuer(
                    self._host._manifest_generator,
                    project_data=project_data,
                    deployment_name=deployment_name,
                    base_domain=base_domain,
                    issuer_config=issuer_config,
                    cluster_name=cluster_name,
                    namespace=prefixed_namespace,
                    output_dir=target_path,
                )
            )

        await self.create_helm_kustomization(
            target_path,
            prefixed_namespace,
            helm_charts_config,
            secret_files,
            regular_files,
        )

        public_key = get_project_public_key(project_data)
        if not public_key:
            raise ValueError(
                f"No public key found for project, cannot encrypt helm values for deployment: {deployment_name}. "
                "This would commit secrets in plain text to git!"
            )

        logger.info(f"Encrypting helm values files for deployment: {deployment_name}")

        to_sops_pattern = os.path.join(target_path, f"*{TO_SOPS_SUFFIX}")
        to_sops_files = glob.glob(to_sops_pattern)
        logger.info(f"Found {len(to_sops_files)} .to-sops.yaml files to encrypt:")
        for file_path in to_sops_files:
            logger.info(f"  - {os.path.basename(file_path)}")

        await asyncio.to_thread(
            encrypt_to_sops_files_or_fail,
            target_path,
            public_key,
            f"helm values-secrets voor deployment '{deployment_name}' (namespace '{prefixed_namespace}')",
            private_key=project_private_key_or_none(project_data),
        )

        logger.info("All helm values files successfully encrypted")
        logger.info(f"Helm chart deployment processing complete for: {deployment_name}")

    async def process_helmfile_deployment(
        self,
        deployment: dict[str, Any],
        git_connector: GitRepoWorkspace,
        target_path: str,
        namespace_policy_files: list[str] | None = None,
    ) -> None:
        """Render a helmfile deployment into ``target_path``.

        The CMP plugin takes it from there: it detects ``helmfile.yaml``, decrypts
        ``values.sops.yaml`` and runs ``helmfile template`` over it. The values are written as
        ``*.to-sops.yaml`` and encrypted at the end of this call, so an exception halfway
        leaves plaintext behind that must not reach git.
        """
        project_data = await self._host.get_contents()
        deployment_name = deployment.get("name")
        if not deployment_name:
            raise ValueError("Deployment missing required 'name' field")

        cluster_name = deployment.get("cluster", settings.CLUSTER_MANAGER)
        prefixed_namespace = get_prefixed_namespace(cluster_name, deployment["namespace"])

        logger.info(f"Processing helmfile deployment: {deployment_name}")

        os.makedirs(target_path, exist_ok=True)

        helmfile_refs = deployment.get("helmfile", [])

        context = await self.get_helm_values_context(deployment_name)

        context["NAMESPACE"] = prefixed_namespace

        # The chart renders the ingresses of a helmfile deployment itself, so the annotation from
        # manifests/ingress.yaml.jinja never reaches them. Without a target external-dns writes a
        # CNAME to the cluster router hostname, and such a reference across the zone boundary does
        # not survive DNSSEC validation at Google (docs.rijksapp.nl gave SERVFAIL, EDE 12). The
        # chart renders more hostnames than this one (docs and static-docs) from the same
        # annotation map, which holds as long as they sit in the same zone.
        helmfile_hostname = context.get("PUBLIC_HOSTNAME")
        external_dns_target = (
            get_external_dns_target_for_hostname(cluster_name, helmfile_hostname) if helmfile_hostname else None
        )

        for helmfile_ref in helmfile_refs:
            helmfile_reference = helmfile_ref.get("reference")
            if not helmfile_reference:
                raise ValueError("Helmfile reference missing required 'reference' field")

            logger.info(f"Processing helmfile reference: {helmfile_reference}")

            helmfile_def = self._host._project_file_handler.get_helmfile_by_name(project_data, helmfile_reference)
            if not helmfile_def:
                raise ValueError(f"Helmfile '{helmfile_reference}' not found in project definition")

            _, entry_path = await clone_helmfile_source(helmfile_def, target_path)

            if entry_path:
                helmfile_config_path = os.path.join(target_path, ".helmfile-entry")
                with open(helmfile_config_path, "w") as f:
                    f.write(entry_path)
                logger.info(f"Created helmfile entry config: {helmfile_config_path} -> {entry_path}")

            # The env vars for the CMP reach git as a SOPS file; the plugin decrypts them with
            # the project key and then reads them as KEY=value lines.
            cmp_env_names = sops_filenames(CMP_ENV_NAME)
            cmp_env_to_sops_path = os.path.join(target_path, cmp_env_names.plaintext)
            cmp_env_sops_path = os.path.join(target_path, cmp_env_names.encrypted)
            cmp_env_plaintext_path = os.path.join(target_path, CMP_ENV_PLAINTEXT_FILENAME)
            cmp_env_vars: dict[str, str] = {}

            env_vars = helmfile_ref.get("env-vars", {})
            if env_vars and isinstance(env_vars, dict):
                # Only fetched when an encrypted value is actually present, so a
                # project without one keeps working even if no key is configured.
                env_private_key: str | None = None
                for key, value in env_vars.items():
                    if carries_encrypted_value(value):
                        if env_private_key is None:
                            env_private_key = project_private_key_or_none(project_data)
                        if not env_private_key:
                            raise ValueError(
                                f"Helmfile deployment {deployment_name} has AGE-encrypted env var "
                                f"'{key}' but the project age-private-key could not be read"
                            )
                        plain_value = await decrypt_password_smart(value, env_private_key)
                    else:
                        if has_encrypted_marker(value):
                            # Neither encrypted nor plain: on one line with a literal \n it
                            # passes the schema and cmp_env_rejection, and would reach the
                            # CMP as readable text.
                            raise ValueError(
                                f"Helmfile deployment {deployment_name} has env var '{key}' that holds an "
                                f"AGE marker without being an encrypted value; encrypt the whole value"
                            )
                        plain_value = str(value)

                    # The plugin reads the env vars as KEY=value lines and refuses the same.
                    # Checked after decryption: only there is what the plugin finally gets visible.
                    rejection = cmp_env_rejection(key, plain_value)
                    if rejection:
                        raise ValueError(
                            f"Helmfile deployment {deployment_name} cannot pass this env var to the CMP: {rejection}"
                        )
                    cmp_env_vars[key] = plain_value

            if cmp_env_vars:
                # A fixed key order, so that the skip-if-unchanged of SOPS really sees an
                # unchanged map as unchanged.
                with open(cmp_env_to_sops_path, "w") as f:
                    f.write(dump_yaml_for_go_unfolded({key: cmp_env_vars[key] for key in sorted(cmp_env_vars)}))
                logger.info(f"Created CMP environment file (to be encrypted): {cmp_env_to_sops_path}")
            elif os.path.exists(cmp_env_sops_path):
                # Removed env vars have to disappear from the render as well.
                os.remove(cmp_env_sops_path)
                logger.info(f"Removed CMP environment file, deployment has no env-vars: {cmp_env_sops_path}")

            if os.path.exists(cmp_env_plaintext_path):
                os.remove(cmp_env_plaintext_path)
                logger.info(f"Removed plain-text CMP environment file: {cmp_env_plaintext_path}")

            base_values = await self._host._project_file_handler.extract_helmfile_values(
                project_data, helmfile_reference
            )

            deployment_values = await self._host._project_file_handler.extract_deployment_helmfile_values(
                project_data, deployment_name, helmfile_reference
            )

            merged_values = deep_merge_dicts(base_values, deployment_values)

            # Merged in as the base, not over it: existing annotations stay, and a target from
            # the project values wins, like every other value on this path.
            if external_dns_target:
                merged_values = deep_merge_dicts(
                    {"cluster": {"ingress": {"annotations": {_EXTERNAL_DNS_TARGET_ANNOTATION: external_dns_target}}}},
                    merged_values,
                )
                logger.info(
                    f"Set external-dns target '{external_dns_target}' on helmfile values for "
                    f"{deployment_name} ({helmfile_hostname})"
                )

            resolved_values = resolve_nested_aliases(merged_values, context)

            # CMP plugin looks for values.sops.yaml in helmfile directories
            values_file_to_sops = sops_filenames("values").plaintext
            values_path = os.path.join(target_path, values_file_to_sops)

            with open(values_path, "w") as f:
                f.write(dump_yaml_for_go(resolved_values))

            logger.info(f"Created helmfile values file (to be encrypted): {values_path}")

            custom_files = write_helmfile_custom_files(helmfile_def, helmfile_ref, target_path)
            if custom_files:
                logger.info(f"Wrote {len(custom_files)} custom file(s) for helmfile deployment")

        secret_files = await self.create_deployment_secrets(
            deployment_name,
            target_path,
            prefixed_namespace,
            cluster_name,
        )
        logger.info(f"Created {len(secret_files)} service secret manifests for helmfile deployment")

        # The namespace-wide policies are already on disk; they only still have to reach the
        # kustomization, which is built here from a list and not from a directory scan.
        regular_files: list[str] = list(namespace_policy_files or [])

        issuer_config = effective_issuer(project_data, deployment, cluster_name)
        base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)

        if issuer_config and issuer_config in ("letsencrypt", "letsencrypt-staging") and base_domain:
            regular_files.extend(
                emit_letsencrypt_issuer(
                    self._host._manifest_generator,
                    project_data=project_data,
                    deployment_name=deployment_name,
                    base_domain=base_domain,
                    issuer_config=issuer_config,
                    cluster_name=cluster_name,
                    namespace=prefixed_namespace,
                    output_dir=target_path,
                )
            )

        # Create kustomization.yaml for additional resources (Issuer, NetworkPolicy, Secrets).
        # The CMP plugin runs both kustomize build and helmfile template, so additional resources
        # are applied alongside helmfile output.
        sops_files = [sops_filenames(f).encrypted for f in secret_files]

        if regular_files or sops_files:
            logger.info(
                f"Creating kustomization.yaml for helmfile deployment with "
                f"{len(regular_files)} resources and {len(sops_files)} SOPS files"
            )
            self._host._manifest_generator.create_kustomization_files(
                output_dir=target_path,
                namespace=prefixed_namespace,
                sops_files=sops_files,
                regular_files=regular_files,
                # No helm charts: helmfile renders those itself.
                helm_charts=[],
            )
            logger.info(f"Created kustomization.yaml with resources: {regular_files}, sops: {sops_files}")
        else:
            logger.debug("No additional resources for kustomization.yaml, skipping creation")

        public_key = get_project_public_key(project_data)
        if not public_key:
            raise ValueError(
                f"No public key found for project, cannot encrypt helm values for deployment: {deployment_name}. "
                "This would commit secrets in plain text to git!"
            )

        logger.info(f"Encrypting helmfile values files for deployment: {deployment_name}")

        to_sops_pattern = os.path.join(target_path, f"*{TO_SOPS_SUFFIX}")
        to_sops_files = glob.glob(to_sops_pattern)
        logger.info(f"Found {len(to_sops_files)} .to-sops.yaml files to encrypt:")
        for file_path in to_sops_files:
            logger.info(f"  - {os.path.basename(file_path)}")

        await asyncio.to_thread(
            encrypt_to_sops_files_or_fail,
            target_path,
            public_key,
            f"helmfile values-secrets voor deployment '{deployment_name}' (namespace '{prefixed_namespace}')",
            private_key=project_private_key_or_none(project_data),
        )

        logger.info("All helmfile values files successfully encrypted")
        logger.info(f"Helmfile deployment processing complete for: {deployment_name}")

    async def create_deployment_secrets(
        self,
        deployment_name: str,
        target_path: str,
        namespace: str,
        cluster: str,
    ) -> list[str]:
        """Write a Secret manifest per service of a deployment; returns the ``*.to-sops.yaml`` names.

        Reads the secrets map that the service managers filled, so a service whose manager did
        not run in this pass gets no manifest.
        """
        created_files: list[str] = []

        secret_template_path = os.path.join(MANIFEST_TEMPLATES_DIR, "generic-secret.yaml.to-sops.jinja")

        keycloak_secret = self._host._get_secret_from_map(deployment_name, "keycloak", KeycloakSecret)
        if keycloak_secret:
            keycloak_secret_data = keycloak_secret.to_k8s_secret_data()
            secret_vars = {
                "name": KeycloakSecret.get_secret_name(deployment_name),
                "namespace": namespace,
                "secret_pairs": keycloak_secret_data,
            }
            manifest_name = f"{KeycloakSecret.get_secret_name(deployment_name)}-secret"
            self._host._manifest_generator.create_manifest_file(
                template_path=secret_template_path,
                values=secret_vars,
                output_dir=target_path,
                output_filename=manifest_name,
                use_sops=True,
            )
            created_files.append(sops_filenames(manifest_name).plaintext)
            logger.info(f"Created Keycloak secret manifest: {manifest_name}")

        db_secret = self._host._get_secret_from_map(deployment_name, "database", DatabaseSecret)
        if db_secret:
            db_secret_data = db_secret.to_k8s_secret_data()
            secret_vars = {
                "name": DatabaseSecret.get_secret_name(deployment_name),
                "namespace": namespace,
                "secret_pairs": db_secret_data,
            }
            manifest_name = f"{DatabaseSecret.get_secret_name(deployment_name)}-secret"
            self._host._manifest_generator.create_manifest_file(
                template_path=secret_template_path,
                values=secret_vars,
                output_dir=target_path,
                output_filename=manifest_name,
                use_sops=True,
            )
            created_files.append(sops_filenames(manifest_name).plaintext)
            logger.info(f"Created Database secret manifest: {manifest_name}")

        minio_secret = self._host._get_secret_from_map(deployment_name, "minio", MinIOSecret)
        if minio_secret:
            minio_secret_data = minio_secret.to_k8s_secret_data()
            secret_vars = {
                "name": MinIOSecret.get_secret_name(deployment_name),
                "namespace": namespace,
                "secret_pairs": minio_secret_data,
            }
            manifest_name = f"{MinIOSecret.get_secret_name(deployment_name)}-secret"
            self._host._manifest_generator.create_manifest_file(
                template_path=secret_template_path,
                values=secret_vars,
                output_dir=target_path,
                output_filename=manifest_name,
                use_sops=True,
            )
            created_files.append(sops_filenames(manifest_name).plaintext)
            logger.info(f"Created MinIO secret manifest: {manifest_name}")

        redis_secret = self._host._get_secret_from_map(deployment_name, "redis", RedisSecret)
        if redis_secret:
            redis_secret_data = redis_secret.to_k8s_secret_data()
            secret_vars = {
                "name": RedisSecret.get_secret_name(deployment_name),
                "namespace": namespace,
                "secret_pairs": redis_secret_data,
            }
            manifest_name = f"{RedisSecret.get_secret_name(deployment_name)}-secret"
            self._host._manifest_generator.create_manifest_file(
                template_path=secret_template_path,
                values=secret_vars,
                output_dir=target_path,
                output_filename=manifest_name,
                use_sops=True,
            )
            created_files.append(sops_filenames(manifest_name).plaintext)
            logger.info(f"Created Redis secret manifest: {manifest_name}")

        return created_files

    async def create_helm_kustomization(
        self,
        target_path: str,
        namespace: str,
        helm_charts: list[dict[str, Any]],
        secret_files: list[str] | None = None,
        regular_files: list[str] | None = None,
    ) -> None:
        """Write kustomization.yaml and decrypt-sops.yaml for a helm-charts deployment.

        Only Secret manifests go into the KSOPS list: the helm values files are decrypted by
        the CMP plugin, not by KSOPS.
        """
        all_sops_files: list[str] = []

        if secret_files:
            all_sops_files.extend(secret_files)

        self._host._manifest_generator.create_kustomization_files(
            output_dir=target_path,
            namespace=namespace,
            sops_files=all_sops_files or None,
            regular_files=regular_files or [],
            helm_charts=helm_charts,
        )
