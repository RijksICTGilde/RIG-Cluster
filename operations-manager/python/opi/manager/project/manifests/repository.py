"""Writing a project's manifests into the deployment repositories, one round per repository."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import posixpath
from typing import TYPE_CHECKING, Any, Protocol

from opi.connectors.git_repo import GitRepoConflictError
from opi.core.cluster_config import get_prefixed_namespace
from opi.generation.manifests import MANIFEST_TEMPLATES_DIR
from opi.manager.project.emitters import emit_namespace_network_policies
from opi.manager.project.helm_deployment import deployment_uses_helm_charts, deployment_uses_helmfile
from opi.manager.project.manifests.application import ApplicationManifests, ApplicationManifestsAccess
from opi.manager.project.manifests.pruning import (
    prune_obsolete_project_manifests,
    prune_obsolete_service_manifests,
    select_obsolete_component_manifests,
)
from opi.services.catalog.base import ProjectManifestContext
from opi.services.catalog.image_registries.manifest_pass import apply_rules_to_directory
from opi.services.catalog.image_registries.resolution import build_rules
from opi.services.project import Project
from opi.services.project_utils import project_level_deployment
from opi.services.registry import project_manifest_services
from opi.services.tasks import progress_steps as steps
from opi.utils.age import get_project_public_key, project_private_key_or_none
from opi.utils.naming import (
    generate_deployment_manifest_path,
    generate_project_level_manifest_path,
)
from opi.utils.sops import TO_SOPS_SUFFIX, encrypt_to_sops_files_or_fail, sops_filenames

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from opi.connectors.git_repo import GitRepoWorkspace

logger = logging.getLogger(__name__)


class RepositoryManifestsAccess(ApplicationManifestsAccess, Protocol):
    """What the repository round reaches from its host, on top of what the writer needs.

    The three ``_project_level_*`` attributes are written here and read by the ArgoCD waits,
    so they stay on the host.
    """

    _project_level_manifests_changed: bool
    _project_level_path: str | None
    _project_level_repo: str | None

    async def get_repositories(self) -> list[dict[str, Any]]: ...

    async def get_deployments(
        self,
        cluster_filter: bool = True,
        deployment_name: str | None = None,
        deployment_names: list[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    async def get_deployment_workspace(self, name: str, repo_config: dict[str, str]) -> GitRepoWorkspace: ...

    async def _collect_deployment_aliases(self, deployment_name: str) -> dict[str, dict[str, dict[str, str]]]: ...

    async def _clear_blocking_sync_operations(
        self, deployments: list[dict[str, Any]], *, reset_running_sync: bool = False
    ) -> None: ...

    async def _process_helm_chart_deployment(
        self,
        deployment: dict[str, Any],
        git_connector: GitRepoWorkspace,
        target_path: str,
        namespace_policy_files: list[str] | None = None,
    ) -> None: ...

    async def _process_helmfile_deployment(
        self,
        deployment: dict[str, Any],
        git_connector: GitRepoWorkspace,
        target_path: str,
        namespace_policy_files: list[str] | None = None,
    ) -> None: ...

    def _step(
        self, name: str, subject: str | None = None, retried_on: type[Exception] | None = None
    ) -> AbstractContextManager[None]: ...


class RepositoryManifests:
    """The repository round of ``ProjectManager``, built per call around its host."""

    def __init__(self, host: RepositoryManifestsAccess) -> None:
        self._host = host

    async def process_application_manifests(
        self,
        deployment_name: str | None = None,
        deployment_names: list[str] | None = None,
        reset_running_sync: bool = False,
    ) -> None:
        """
        Process application manifests for the in-scope deployments.

        Args:
            deployment_name: Optional single deployment to process only.
            deployment_names: Optional explicit set of deployments to process
                (takes precedence over ``deployment_name``). Use this to keep a
                scoped op (e.g. an upsert of one deployment) from regenerating
                every deployment's manifest folder -- which would otherwise
                rewrite unrelated deployments and collide with a concurrent
                delete.
            reset_running_sync: Break off a sync that is still running, however
                young. Comes from a refresh; see ``_clear_blocking_sync_operations``.

        Returns: None
        """
        project_name = await self._host.get_name()
        logger.info(f"Processing application manifests for {project_name}")

        repositories = await self._host.get_repositories()
        if not repositories:
            logger.warning("No repositories defined in project data")
            return

        # Grouped by repository and already filtered to the requested scope:
        # get_deployments honours both deployment_name and deployment_names.
        deployments = await self._host.get_deployments(
            cluster_filter=True, deployment_name=deployment_name, deployment_names=deployment_names
        )
        if deployment_names is not None or deployment_name:
            logger.info(f"Processing application manifests only for: {deployment_names or [deployment_name]}")

        deployments_by_repo = {}
        for deployment in deployments:
            repo_name = deployment.get("repository")
            if repo_name not in deployments_by_repo:
                deployments_by_repo[repo_name] = []
            deployments_by_repo[repo_name].append(deployment)

        for repo_name, repo_deployments in deployments_by_repo.items():
            logger.info(f"Processing repository: {repo_name} with {len(repo_deployments)} deployments")

            repo_info = Project.locate(repositories, name=repo_name)
            if not repo_info:
                raise Exception(f"Repository configuration not found: {repo_name}")

            await self.process_repository_manifests(repo_info, repo_deployments, reset_running_sync=reset_running_sync)

        logger.info(f"Successfully processed all application manifests for {project_name}")

    async def process_repository_manifests(
        self,
        repo_config: dict[str, Any],
        deployments: list[dict[str, Any]],
        reset_running_sync: bool = False,
    ) -> None:
        """
        Process manifests for a specific repository.

        Args:
            repo_config: Repository configuration
            deployments: List of deployments for this repository
            reset_running_sync: Break off a sync that is still running, however young.
                Comes from a refresh; see ``_clear_blocking_sync_operations``.

        Returns:
            None
        """
        project_data = await self._host.get_contents()
        project_name = project_data.get("name")

        workspace = await self._host.get_deployment_workspace(repo_config["name"], repo_config)
        owner = project_level_deployment(await self._host.get_deployments(cluster_filter=True))

        async def _generate() -> None:
            """Write this repository's manifests into this round's working directory."""
            for deployment in deployments:
                with self._host._step(steps.DEPLOYMENT_CONFIG_BUILD, subject=str(deployment["name"])):
                    await self.process_deployment_manifests(deployment, workspace)

            # The PROJECT level is namespace-wide, so it lives in exactly ONE repository,
            # even for a project whose deployments span two. Which one is decided by
            # project_level_deployment, the same function the application uses.
            if owner is not None and owner.get("repository") == repo_config.get("name"):
                with self._host._step(steps.PROJECT_CONFIG_BUILD):
                    await self.process_project_manifests(
                        repo_config,
                        str(owner["cluster"]),
                        get_prefixed_namespace(str(owner["cluster"]), str(owner["namespace"])),
                        workspace,
                    )
                self._host._project_level_repo = repo_config.get("name")

            # Just BEFORE the write and not after: ArgoCD does not retry a revision that
            # failed once, so terminating after the push leaves the blockade in place for
            # exactly the commit meant to clear it. A failure here never holds back the write
            # (``terminate_stuck_operation`` absorbs a broken kubectl itself).
            await self._host._clear_blocking_sync_operations(deployments, reset_running_sync=reset_running_sync)

        names = ", ".join(str(deployment["name"]) for deployment in deployments)
        # Fetching and pushing each get their own step, so a hanging or failing remote shows
        # up on the progress page at that phase, not at the last service or manifest. The
        # fetch lives inside mutate_and_push because it belongs under the lock. A conflict
        # is retried there, so it does not fail the push step.
        await workspace.mutate_and_push(
            f"Update manifests for {project_name} ({names})",
            _generate,
            on_prepare=lambda: self._host._step(steps.CONFIG_FETCH, subject=str(repo_config["name"])),
            on_push=lambda: self._host._step(
                steps.CONFIG_PUBLISH, subject=str(repo_config["name"]), retried_on=GitRepoConflictError
            ),
        )

        # Whether the PROJECT LEVEL really changed this round. If not, the guard in
        # wait_for_project_level_application need not wait for a new sync. Only the repository
        # that owns the project level can measure that; for any other it stays True, because
        # that one knows nothing about it. The write's paths are repository-relative and
        # normalized while ``_project_level_path`` is not (``./cluster/...`` for a
        # repositories[].path of '.'), so both sides go through normpath.
        if owner is not None and owner.get("repository") == repo_config.get("name") and self._host._project_level_path:
            prefix = f"{posixpath.normpath(self._host._project_level_path)}/"
            self._host._project_level_manifests_changed = bool(
                workspace.last_write
                and any(posixpath.normpath(path).startswith(prefix) for path in workspace.last_write.paths)
            )

        logger.info(f"Successfully processed repository: {repo_config['name']}")

    async def process_project_manifests(
        self,
        repo_config: dict[str, Any],
        cluster_name: str,
        namespace: str,
        git_connector: GitRepoWorkspace,
    ) -> None:
        """Write the PROJECT level of the deployments repository: ``<cluster>/<project>/_project/``.

        The generic emitter: what ends up there is decided by the services through
        ``contribute_project_manifests``.
        """
        project_data = await self._host.get_contents()
        project_name = await self._host.get_name()

        # The same function as the ArgoCD application that points at this directory, so the
        # directory and the reference to it cannot drift apart.
        project_dir = generate_project_level_manifest_path(cluster_name, project_name, repo_config.get("path", ""))
        # Remembered for _project_level_already_applied: this is the directory ArgoCD has to
        # be proven to have applied already.
        self._host._project_level_path = project_dir
        target_path = os.path.join(await git_connector.get_working_dir(), project_dir)
        os.makedirs(target_path, exist_ok=True)

        ctx = ProjectManifestContext(
            project_name=project_name,
            project_data=project_data,
            cluster=cluster_name,
            namespace=namespace,
        )

        created_files: list[str] = []
        for service in project_manifest_services():
            for spec in service.contribute_project_manifests(ctx):
                self._host._manifest_generator.create_manifest_file(
                    template_path=os.path.join(MANIFEST_TEMPLATES_DIR, spec.template_path),
                    values=spec.values,
                    output_dir=target_path,
                    output_filename=spec.filename,
                    use_sops=spec.encrypt,
                )
                if spec.encrypt:
                    created_files.append(sops_filenames(spec.filename).plaintext)
                else:
                    created_files.append(f"{spec.filename}.yaml")
                logger.info(f"Created project manifest '{spec.filename}' for project '{project_name}'")

        prune_obsolete_project_manifests(target_path, created_files)

        sops_files, regular_files = self._host._manifest_generator.collect_manifest_files(
            target_path, include_subfolders=False
        )
        await ApplicationManifests(self._host).create_kustomization_file(
            git_connector, namespace, sops_files, regular_files, project_dir
        )

        public_key = get_project_public_key(project_data)
        if not public_key:
            raise RuntimeError(f"Geen SOPS public key voor het projectniveau van '{project_name}'")
        await asyncio.to_thread(
            encrypt_to_sops_files_or_fail,
            target_path,
            public_key,
            f"projectbrede secrets van '{project_name}' (namespace '{namespace}')",
            private_key=project_private_key_or_none(project_data),
        )

    async def process_deployment_manifests(
        self,
        deployment: dict[str, Any],
        git_connector: GitRepoWorkspace,
    ) -> None:
        """
        Process manifests for a specific deployment.

        Supports two deployment types:
        1. Component-based deployments: Traditional OPI components with deployment/service/ingress manifests
        2. Helm chart deployments: External Helm charts rendered via Kustomize helmCharts

        Args:
            deployment: Deployment configuration
            git_connector: the repository's working directory

        Returns:
            None
        """
        project_data = await self._host.get_contents()
        project_name = await self._host.get_name()
        deployment_name = deployment["name"]
        cluster_name = deployment["cluster"]

        repo_path = await self.get_repository_path(deployment["repository"]) or ""
        deployment_path = generate_deployment_manifest_path(cluster_name, project_name, deployment_name, repo_path)

        prefixed_namespace = get_prefixed_namespace(cluster_name, deployment["namespace"])
        target_path = os.path.join(await git_connector.get_working_dir(), deployment_path)

        logger.info(f"Processing deployment: {deployment_name} at path: {deployment_path}")

        # For all three kinds of deployment: a helm or helmfile pod in particular carries no
        # deployment label and used to fall outside every policy.
        namespace_policy_files = emit_namespace_network_policies(
            self._host._manifest_generator,
            project_data=project_data,
            project_name=project_name,
            deployment=deployment,
            cluster_name=cluster_name,
            namespace=prefixed_namespace,
            output_dir=target_path,
        )

        if deployment_uses_helmfile(deployment):
            logger.info(f"Deployment '{deployment_name}' uses helmfile - processing as helmfile deployment")
            await self._host._process_helmfile_deployment(
                deployment, git_connector, target_path, namespace_policy_files
            )
            return

        if deployment_uses_helm_charts(deployment):
            logger.info(f"Deployment '{deployment_name}' uses helm-charts - processing as helm deployment")
            await self._host._process_helm_chart_deployment(
                deployment, git_connector, target_path, namespace_policy_files
            )
            return

        logger.info(f"Deployment '{deployment_name}' uses components - processing as standard deployment")

        # Collected before any manifest is written, so a deployment-level secret can carry the
        # aliases of every component.
        self._host._deployment_aliases[deployment_name] = await self._host._collect_deployment_aliases(deployment_name)

        try:
            created_files = await ApplicationManifests(self._host).create_application_manifests(
                deployment, git_connector, deployment_path
            )
        except Exception:
            await ApplicationManifests(self._host).rollback_subdomain_if_needed()
            raise

        # Part of this directory's desired state, so the prunes below leave them alone.
        created_files.extend(namespace_policy_files)

        # Prune obsolete per-component manifests: any file this run did not regenerate
        # (removed component, or a renamed/dropped file of a surviving component such as
        # an ingress whose path changed). The kustomization is rebuilt from the on-disk
        # glob, so leftovers would otherwise be re-adopted and keep dead workloads in
        # ArgoCD. Symmetric with the removed-service cleanup in DeleteProjectManager.
        self.prune_obsolete_component_manifests(deployment, target_path, created_files)

        # Prune obsolete deployment-wide, service-owned manifests (RC-15). A service (e.g.
        # cross-domain-access) writes <deployment>-<service>-* files; when the service is
        # switched off or stops contributing, this removes the leftover so ArgoCD prunes
        # the resource. Symmetric with the component prune above and non-overlapping (those
        # files carry no component prefix).
        prune_obsolete_service_manifests(deployment, target_path, created_files)

        # The registry rewrite for images that do not pass through the component loop, such
        # as a sidecar with a fixed value in its template. Idempotent on the rest.
        apply_rules_to_directory(target_path, build_rules(project_data, cluster_name))

        sops_files, regular_files = self._host._manifest_generator.collect_manifest_files(
            target_path, include_subfolders=False
        )
        logger.info(f"Found {len(sops_files)} SOPS files and {len(regular_files)} regular files for kustomization")
        await ApplicationManifests(self._host).create_kustomization_file(
            git_connector, prefixed_namespace, sops_files, regular_files, deployment_path, deployment
        )

        # After the kustomization, so decrypt-sops.yaml can still name the original
        # .to-sops.yaml filenames.
        public_key = get_project_public_key(project_data)
        logger.info(f"Encrypting .to-sops.yaml files for deployment: {deployment_name}")
        logger.info(f"Using SOPS public key for namespace: {prefixed_namespace}")
        logger.info(f"SOPS encryption target path: {target_path}")

        to_sops_pattern = os.path.join(target_path, f"*{TO_SOPS_SUFFIX}")
        to_sops_files = glob.glob(to_sops_pattern)
        logger.info(f"Found {len(to_sops_files)} .to-sops.yaml files for final encryption:")
        for file_path in to_sops_files:
            logger.info(f"  - {os.path.basename(file_path)}")

        if not public_key:
            raise RuntimeError(
                f"Geen SOPS public key voor deployment '{deployment_name}'; "
                "dit zou secrets in platte tekst naar git committen."
            )

        await asyncio.to_thread(
            encrypt_to_sops_files_or_fail,
            target_path,
            public_key,
            f"secrets voor deployment '{deployment_name}' (namespace '{prefixed_namespace}')",
            private_key=project_private_key_or_none(project_data),
        )
        logger.info("All .to-sops.yaml files successfully encrypted")

    async def get_repository_path(self, repository_name: str) -> str | None:
        project_data = await self._host.get_contents()
        repositories = project_data.get("repositories") or []
        repositories: list[dict[str, str]] = repositories
        repo = Project.locate(repositories, name=repository_name)
        return repo.get("path", "") if repo else ""

    def prune_obsolete_component_manifests(
        self, deployment: dict[str, Any], target_path: str, generated_files: list[str]
    ) -> None:
        """Delete per-component manifest files this run did not (re)generate.

        ``create_application_manifests`` only writes/overwrites files for the CURRENT
        components, and the kustomization is rebuilt from the on-disk glob, so any
        leftover per-component file would otherwise be re-adopted and keep its dead
        workload running in ArgoCD. This prunes those obsolete files, covering both a
        removed component (none of its files regenerated) and a renamed/dropped file of
        a surviving component (e.g. an ingress whose path changed). Shared files and
        ``*.marked-for-deletion.yaml`` PVCs are preserved.

        Args:
            deployment: Current deployment configuration.
            target_path: Absolute deployment manifest directory in the cloned repo.
            generated_files: Basenames generated this run (the desired-state set).
        """
        deployment_name = deployment["name"]

        def _component_refs(dep: dict[str, Any]) -> set[str]:
            return {
                ref for comp in dep.get("components", []) if isinstance(comp, dict) and (ref := comp.get("reference"))
            }

        # Components that own files here: current ones (stale-file pruning) plus any
        # from the previous version (removed-component pruning).
        component_names = _component_refs(deployment) | _component_refs(
            self._host.get_previous_deployment(deployment_name) or {}
        )
        if not component_names:
            return

        obsolete = select_obsolete_component_manifests(
            target_path, component_names, set(generated_files), deployment_name
        )
        if not obsolete:
            return

        logger.info(
            f"Pruning {len(obsolete)} obsolete component manifest(s) from {deployment_name}: {', '.join(obsolete)}"
        )
        for basename in obsolete:
            os.remove(os.path.join(target_path, basename))
