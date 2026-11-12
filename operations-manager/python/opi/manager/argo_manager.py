"""ArgoCD manager for handling ArgoCD resources and repository secrets."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from opi.connectors.argo import ArgoTransportError
from opi.core.cluster_config import get_argo_namespace, get_prefixed_namespace
from opi.core.config import settings
from opi.core.metrics import PHASE_ARGO_WAIT_HEALTHY, PHASE_ARGO_WAIT_SYNCED, observe_rollout_phase
from opi.services.event_interpreter import condense_render_error
from opi.services.project_utils import project_level_deployment
from opi.services.tasks.task_supersede import raise_if_superseded
from opi.utils.age import decrypt_password_smart
from opi.utils.git_signing import get_git_signing_identity
from opi.utils.naming import (
    generate_argocd_application_name,
    generate_argocd_appproject_prefix,
    generate_argocd_project_application_name,
    generate_argocd_project_folder_path,
    generate_argocd_repository_secret_name,
    generate_deployment_manifest_path,
    generate_infrastructure_application_name,
    generate_infrastructure_argocd_application_filename,
    generate_infrastructure_argocd_appproject_filename,
    generate_infrastructure_argocd_folder_path,
    generate_infrastructure_manifest_path,
    generate_project_level_manifest_path,
    get_output_filename_from_template,
    make_argocd_repository_url_unique,
)
from opi.utils.sops import encrypt_to_sops_files_or_fail

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: ArgoCD condition types that mean the application can never reach a healthy synced state
#: on its own: the desired manifests could not be generated or compared (where a broken
#: kustomization or CMP render surfaces), or the spec or sync is invalid. ArgoCD then sets
#: sync.status=Unknown and starts NO sync operation, so there is no operationState.phase to
#: key off and the message lives only in status.conditions.
_TERMINAL_CONDITION_TYPES = frozenset({"ComparisonError", "InvalidSpecError", "SyncError", "UnknownError"})


def terminal_condition_message(status_data: dict[str, Any]) -> str | None:
    """Return the message of the first terminal ArgoCD condition, or ``None``.

    Reads ``status.conditions[]`` and looks for a type in :data:`_TERMINAL_CONDITION_TYPES`
    (ComparisonError etc.). The message carries the underlying cause - for a broken
    kustomization it includes the plugin stderr, e.g. "Failed to load target state: failed
    to generate manifests in '<path>': ... exit status 1: <kustomize error>".
    """
    conditions = status_data.get("status", {}).get("conditions", []) or []
    for condition in conditions:
        if condition.get("type") in _TERMINAL_CONDITION_TYPES:
            message = condition.get("message") or condition.get("type", "")
            return f"{condition.get('type')}: {message}"
    return None


#: How often the wait loops ask ArgoCD. Two seconds, because this is about how fast the
#: USER sees it: the step is already done and only we do not know yet. Lower buys nothing,
#: because the progress in the browser does not refresh faster either, while it does cost
#: ArgoCD requests.
POLL_INTERVAL_SECONDS = 2

#: The watcher that keeps poking `user-applications` runs on the ANSWER of the refresh and
#: not on the clock: that call hangs until a reconcile has finished (about ten seconds on
#: production), and exactly then it shows whether that reconcile carried our revision. This
#: lower bound only exists for the case where the call does come straight back, so the loop
#: cannot turn hot; a person is waiting on it.
UMBRELLA_REFRESH_MIN_INTERVAL_SECONDS = 1

#: How many times that is tried at most. A refresh on the umbrella is expensive, because the
#: application lacks the `manifest-generate-paths` annotation (issue #130) and so redraws
#: some 90 child apps. If the revision is still wrong after this many rounds, something other
#: than a lost wake-up is going on and poking again does not help.
UMBRELLA_REFRESH_MAX_ATTEMPTS = 6

#: How long the create waits for the PROJECT LEVEL to be synced. That application manages
#: namespace-wide resources with no rollout of its own, so this is the time for the umbrella
#: plus a sync, not for a pod that starts. A deployment that misses its ServiceAccount does
#: not make its own sync timeout either.
PROJECT_LEVEL_SYNC_TIMEOUT_SECONDS = 240

#: Budget for login/refresh retries during ArgoCD recovery (seconds).
ARGOCD_RECOVERY_BUDGET_SECONDS = 600

#: Minimum interval between retries (seconds).
ARGOCD_RECOVERY_POLL_SECONDS = 5

#: How long a wait for the EXISTENCE of a child Application may take. That wait is not about
#: the application itself but about the umbrella that has to create it, and a refresh of that
#: umbrella redraws some 90 child apps (issue #130), so under load it costs minutes. The same
#: bound for the infrastructure, project and deployment applications: all three wait on that
#: same umbrella.
APPLICATION_CREATION_TIMEOUT_SECONDS = 360

#: How often the wait asks for a refresh itself while the status stays ``Progressing``.
#: ArgoCD re-evaluates an application only on a watch event of a managed resource, and
#: otherwise not until ``timeout.reconciliation`` (15 minutes on production). If such an
#: event is lost, the health freezes on the last observation and a missed event would turn
#: into a user-visible error. Asking for a refresh pulls that one application loose, without
#: lowering the cluster-wide knob.
REFRESH_EVERY_SECONDS = 60


def _elapsed_since(start: float) -> float:
    """Elapsed wall-clock time since ``start``, rounded to 0.1 s.

    Not ``elapsed_time += poll_interval``: on a slow loop, or a poll that runs into its own
    timeout, a round takes much longer than that, and the counter would then lie about the
    real wait and run the loop past its budget.
    """
    return round(asyncio.get_running_loop().time() - start, 1)


class ApplicationGoneError(RuntimeError):
    """The Application existed during this wait and is gone now.

    Not a failure of the app itself: something else removed it, usually a delete_deployment
    for the same deployment. Waiting on is pointless, because nothing is coming back.
    """


class ArgoManager:
    def __init__(self, project_manager: ProjectManager) -> None:
        self.project_manager = project_manager
        #: The commit `_mutate_and_push_argo` pushed last, which the waiters use as the only
        #: hard proof that the umbrella has seen our change. None while nothing was pushed.
        self.last_pushed_argo_commit: str | None = None
        #: Last read from `user-applications`, purely for the timeout diagnosis below.
        self.last_umbrella_revision: str | None = None
        self.last_umbrella_reconciled_at: str | None = None

    @staticmethod
    def _write_manifest_file(target_dir: str, filename: str, content: str) -> str:
        """Write a rendered manifest into the checked-out repo and return its path.

        For anything that still has to come out of a template, ``ManifestGenerator`` is the
        way. Does not commit: ``create_argocd_resources`` decides when a push happens, and it
        does that per phase (project level, then the deployment applications).
        """
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, filename)
        with open(path, "w") as f:
            f.write(content)
        return path

    async def create_argocd_resources(
        self, deployment_name: str | None = None, deployment_names: list[str] | None = None
    ) -> None:
        """
        Create ArgoCD resources for the in-scope deployments of this project.

        In a Distributed Operations Manager architecture, this creates ArgoCD resources
        only for deployments in the CLUSTER_MANAGER cluster.

        Args:
            deployment_name: Optional single deployment to create resources for.
            deployment_names: Optional explicit set of deployments (takes precedence
                over ``deployment_name``). Scoping this prevents a single-deployment
                op from regenerating every deployment's ArgoCD manifests (which would
                rewrite unrelated deployments and collide with a concurrent delete).

        This goes in TWO commits: the project level first, then the deployment applications.
        The gate in between waits until the project level's ServiceAccount is there; in one
        commit there would be nothing left to wait for.

        Both exceptions below travel OUT of here, and that is deliberate: ``process_project``
        turns them into ``return False`` and rolls out nothing at all. A Deployment that
        misses its ServiceAccount does not make its own sync timeout either, so carrying on
        degraded would be a failure presenting itself as a success, and the damage stays with
        this one project.

        Raises:
            TimeoutError: the project level did not sync within
                ``PROJECT_LEVEL_SYNC_TIMEOUT_SECONDS``.
            RuntimeError: ArgoCD is unreachable, or the refresh yields nothing.
        """
        project_name = await self.project_manager.get_name()
        logger.info(f"Creating ArgoCD resources for {project_name} on cluster {settings.CLUSTER_MANAGER}")

        project_data = await self.project_manager.get_contents()

        async def _project_level() -> str | None:
            await self.create_repository_secrets(project_data, deployment_name, deployment_names)
            await self.create_app_projects(project_data, deployment_name, deployment_names)
            # Project-wide, so outside the deployment scope above. Idempotent.
            app_name = await self.create_project_application(project_data)
            # The kustomization is a single shared per-project file that must enumerate
            # every manifest, so it intentionally stays project-wide.
            await self.create_kustomization_files(project_data, deployment_name)
            return app_name

        project_app_name = await self._mutate_and_push_argo(
            f"Added ArgoCD project-level resources for project {project_name} on cluster {settings.CLUSTER_MANAGER}",
            _project_level,
        )

        # The ordering lives here and not in the sync wave: while the CR of a deployment
        # application does not exist, ArgoCD cannot sync it either, so the commit that carries
        # it only goes out after this gate (RC-229, and
        # `docs/post-mortems/de-serviceaccount-komt-na-de-deployment.md`).
        if project_app_name:
            await self.project_manager.wait_for_project_level_application(project_app_name)

        async def _deployment_applications() -> None:
            await self.create_applications(project_data, deployment_name, deployment_names)
            await self.create_kustomization_files(project_data, deployment_name)

        await self._mutate_and_push_argo(
            f"Added ArgoCD resources for project {project_name} on cluster {settings.CLUSTER_MANAGER}",
            _deployment_applications,
        )

    async def _mutate_and_push_argo[T](self, message: str, mutate: Callable[[], Awaitable[T]]) -> T:
        """Write to argo-applications repo and remember the pushed commit."""
        workspace = await self.project_manager.get_argo_workspace()
        outcome = await workspace.mutate_and_push(message, mutate)
        # The state the waiter has to see back in the umbrella. If nothing landed because that
        # state was already there, it is the tip we built on, and that it was compared can
        # still be proven.
        write = workspace.last_write
        self.last_pushed_argo_commit = write.ref if write else None
        return outcome

    async def create_repository_secrets(
        self,
        project_data: dict[str, Any],
        deployment_name: str | None = None,
        deployment_names: list[str] | None = None,
    ) -> None:
        """
        Create ArgoCD repository secrets with unique URLs for each project.

        This method creates ArgoCD repository manifest files for each repository defined
        in the project. Each repository URL is made unique by embedding the project name
        as a username, preventing ArgoCD credential conflicts when multiple projects
        use the same repository URL.

        Repository files are created with SOPS encryption using .to-sops.yaml naming convention.

        In a Distributed Operations Manager architecture, this method only creates secrets
        for deployments in the CLUSTER_MANAGER cluster.

        Args:
            project_data: The project configuration data containing repositories and deployments
            deployment_name: Optional deployment name to filter by specific deployment
        """
        project_name = await self.project_manager.get_name()

        deployments = await self.project_manager.get_deployments(
            cluster_filter=True, deployment_name=deployment_name, deployment_names=deployment_names
        )

        if not deployments:
            logger.warning(f"No deployments for cluster {settings.CLUSTER_MANAGER}")
            return

        argo_workspace = await self.project_manager.get_argo_workspace()
        working_dir = await argo_workspace.get_working_dir()

        logger.info(
            f"Creating ArgoCD repository secrets for project: {project_name} on cluster {settings.CLUSTER_MANAGER}"
        )

        repositories = await self.project_manager.get_repositories()
        if not repositories:
            logger.error("No repositories defined in project data")
            return

        cluster_name = settings.CLUSTER_MANAGER
        project_dir = os.path.join(
            str(working_dir), generate_argocd_project_folder_path(str(cluster_name), project_name)
        )
        logger.info(f"Creating cluster/project directory: {project_dir}")
        os.makedirs(project_dir, exist_ok=True)

        manifest_configs = []
        for repository in repositories:
            repo_name = repository["name"]
            repo_url = repository["url"]

            logger.info(f"  Processing repository: {repo_name} ({repo_url})")

            # ArgoCD properly reloads secrets when they change, so versioning is not needed
            unique_repo_name = generate_argocd_repository_secret_name(project_name, repo_name)
            logger.info(f"  Generated secret name: {unique_repo_name}")

            variables = await self.prepare_repository_variables(
                name=unique_repo_name,
                namespace=get_argo_namespace(cluster_name),
                repository=repository,
                repo_type="git",
                project_name=project_name,
            )

            is_https = repo_url.startswith("https://")
            template_filename = "argo-repository-https.yaml.jinja" if is_https else "argo-repository.yaml.jinja"
            template_path = os.path.join(settings.MANIFESTS_PATH, template_filename)
            output_filename = get_output_filename_from_template(template_filename, unique_repo_name)

            logger.info(f"  Output filename: {output_filename}")

            manifest_config = {
                "template_path": template_path,
                "values": variables,
                "output_filename": output_filename,
                "use_sops": True,
            }

            manifest_configs.append(manifest_config)
            logger.info(f"  Added repository manifest config for {unique_repo_name} (SOPS: True)")

        logger.info(f"Creating {len(manifest_configs)} ArgoCD repository manifests with SOPS encryption")
        created_files = []

        for config in manifest_configs:
            manifest_path = self.project_manager._manifest_generator.create_manifest_file(
                template_path=config["template_path"],
                values=config["values"],
                output_dir=project_dir,
                output_filename=config["output_filename"],
                use_sops=config["use_sops"],
            )
            created_files.append(manifest_path)
            logger.info(f"Successfully created repository manifest: {os.path.basename(manifest_path)}")

        await asyncio.to_thread(
            encrypt_to_sops_files_or_fail,
            project_dir,
            cast("str", settings.SOPS_AGE_PUBLIC_KEY),
            f"ArgoCD repository-secret (git SSH key/HTTPS-wachtwoord) voor project '{project_name}'",
            private_key=settings.SOPS_AGE_PRIVATE_KEY,
        )

    async def prepare_repository_variables(
        self, name: str, namespace: str, repository: dict[str, Any], repo_type: str, project_name: str
    ) -> dict[str, Any]:
        """
        Prepare variables for ArgoCD repository manifest templates.

        This method generates the template variables needed to create an ArgoCD repository
        secret. It makes the repository URL unique by embedding the project name as a username,
        preventing ArgoCD credential conflicts when multiple projects use the same repository.

        Args:
            name: Repository secret name
            namespace: ArgoCD namespace
            repository: Repository configuration dictionary containing url, username, password
            repo_type: Repository type (e.g., "git", "helm")
            project_name: Project name to make URL unique

        Returns:
            Dictionary containing variables for template substitution with unique URL

        Raises:
            ValueError: If the decrypted password holds a line break or a NUL byte
        """
        repository_url = repository.get("url", "")
        username = repository.get("username")
        password = repository.get("password")

        # The project name goes into the URL as a username, which keeps ArgoCD from warning
        # "Found multiple credentials for repoURL".
        unique_url = make_argocd_repository_url_unique(repository_url, project_name)

        logger.debug(f"Original URL: {repository_url}")
        logger.debug(f"Unique URL for ArgoCD: {unique_url}")

        is_https = unique_url.startswith("https://")

        decrypted_password = None
        if password:
            decrypted_password = await decrypt_password_smart(password, settings.SOPS_AGE_PRIVATE_KEY)
            # The templates already encode this as a YAML scalar; this check makes a wrongly
            # set value show up during processing instead of only in ArgoCD.
            if decrypted_password and any(character in decrypted_password for character in "\r\n\x00"):
                raise ValueError(
                    f"Het wachtwoord van repository '{repository.get('name') or name}' bevat een regeleinde"
                    " of een NUL-byte"
                )

        # SSH deploy keys (per-repo, AGE-encrypted in project file) are not
        # injected here. The rendered secret carries an empty sshPrivateKey;
        # callers that need SSH auth supply the key via the project flow.
        ssh_private_key = ""

        return {
            "name": name,
            "namespace": namespace,
            "type": repo_type,
            "repository_url": unique_url,  # UNIQUE URL for ArgoCD
            "is_https": is_https,
            "username": username or "",
            "password": decrypted_password or "",
            "ssh_private_key": ssh_private_key,
        }

    async def create_app_projects(
        self,
        project_data: dict[str, Any],
        deployment_name: str | None = None,
        deployment_names: list[str] | None = None,
    ) -> None:
        """
        Create ArgoCD AppProject resources for the project.

        In a Distributed Operations Manager architecture, this method only creates AppProjects
        for deployments in the CLUSTER_MANAGER cluster.

        Args:
            project_data: The project configuration data
            deployment_name: Optional deployment name to filter by specific deployment
        """
        project_name = await self.project_manager.get_name()
        logger.debug(f"Creating ArgoCD AppProject for project: {project_name} on cluster {settings.CLUSTER_MANAGER}")

        deployments = await self.project_manager.get_deployments(
            cluster_filter=True, deployment_name=deployment_name, deployment_names=deployment_names
        )

        if not deployments:
            logger.warning(f"No deployments for cluster {settings.CLUSTER_MANAGER}")
            return

        base_namespaces: set[str] = set()
        for deployment in deployments:
            base_namespace = deployment.get("namespace")
            base_namespaces.add(base_namespace)

        # The source repos come from EVERY deployment of this cluster, not from the filtered
        # selection above: an AppProject is overwritten, and with only one deployment's repo in
        # it the Application of another deployment in the same namespace can no longer sync.
        source_repos_per_namespace = self._source_repos_per_namespace(
            await self.project_manager.get_deployments(cluster_filter=True),
            project_data.get("repositories", []),
            project_name,
        )

        argo_workspace = await self.project_manager.get_argo_workspace()
        working_dir = await argo_workspace.get_working_dir()

        cluster_name = settings.CLUSTER_MANAGER

        for base_destination_namespace in base_namespaces:
            appproject_name = generate_argocd_appproject_prefix(project_name, base_destination_namespace)

            appproject_content = self.generate_appproject_manifest(
                name=appproject_name,
                namespace=get_argo_namespace(cluster_name),
                destination_namespace=get_prefixed_namespace(cluster_name, base_destination_namespace),
                project_label=project_name,
                source_repos=source_repos_per_namespace.get(base_destination_namespace, []),
            )

            template_filename = "argocd-appproject.yaml.jinja"
            output_filename = get_output_filename_from_template(template_filename, appproject_name)

            project_dir = os.path.join(working_dir, generate_argocd_project_folder_path(cluster_name, project_name))
            appproject_file_path = self._write_manifest_file(project_dir, output_filename, appproject_content)

            logger.info(
                f"Successfully created ArgoCD AppProject file for cluster {cluster_name}, "
                f"namespace {base_destination_namespace}: {appproject_file_path}"
            )

    @staticmethod
    def _source_repos_per_namespace(
        deployments: list[dict[str, Any]], repositories: list[dict[str, Any]], project_name: str
    ) -> dict[str, list[str]]:
        """The source repos per deployment namespace, in the form the Application uses."""
        found: dict[str, set[str]] = {}
        for deployment in deployments:
            repo_name = deployment.get("repository")
            repo_info = next((r for r in repositories if r.get("name") == repo_name), None)
            if not repo_info:
                logger.error(f"Repository not found for AppProject sourceRepos: {repo_name}")
                continue
            namespace = str(deployment.get("namespace"))
            unique_url = make_argocd_repository_url_unique(repo_info["url"], project_name)
            found.setdefault(namespace, set()).add(unique_url)
        return {namespace: sorted(urls) for namespace, urls in found.items()}

    def generate_appproject_manifest(
        self, name: str, namespace: str, destination_namespace: str, project_label: str, source_repos: list[str]
    ) -> str:
        """Render the ArgoCD AppProject manifest from its template.

        Args:
            destination_namespace: Target namespace pattern, e.g. "project-*".
            source_repos: Repository URLs the project may deploy from.
        """
        manifest_path = os.path.join(settings.MANIFESTS_PATH, "argocd-appproject.yaml.jinja")

        # Without a signing key the template renders no signatureKeys, so a cluster where OPI
        # does not sign keeps working.
        signing = get_git_signing_identity()

        variables = {
            "name": name,
            "namespace": namespace,
            "destination": {"namespace": destination_namespace},
            "labels": {"project": project_label},
            "source_repos": source_repos,
            "signature_key_id": signing.key_id if signing else None,
        }

        try:
            with open(manifest_path) as f:
                manifest_template = f.read()

            return self.project_manager._manifest_generator.template_manifest(manifest_template, variables)
        except Exception:
            logger.exception("Error generating ArgoCD AppProject manifest")
            raise

    def generate_application_manifest(
        self,
        name: str,
        namespace: str,
        argo_project: str,
        repo_url: str,
        target_revision: str,
        repo_path: str,
        destination_namespace: str,
        project_label: str,
        sync_wave: int = 1,
    ) -> str:
        """Render the ArgoCD Application manifest from its template.

        Args:
            target_revision: Git branch or tag.
            sync_wave: Deployments run on 1; the project level runs on 0, so what is
                namespace-wide is there before the pods that need it.
        """
        manifest_path = os.path.join(settings.MANIFESTS_PATH, "argocd-application.yaml.jinja")

        variables = {
            "name": name,
            "namespace": namespace,
            "argo_project": argo_project,
            "repoURL": repo_url,
            "targetRevision": target_revision,
            "repoPath": repo_path,
            "labels": {"project": project_label},
            "destination": {"namespace": destination_namespace},
            "sync_wave": sync_wave,
        }

        try:
            with open(manifest_path) as f:
                manifest_template = f.read()

            return self.project_manager._manifest_generator.template_manifest(manifest_template, variables)
        except Exception:
            logger.exception("Error generating ArgoCD application manifest")
            raise

    async def create_applications(
        self,
        project_data: dict[str, Any],
        deployment_name: str | None = None,
        deployment_names: list[str] | None = None,
    ) -> bool:
        """Create the ArgoCD Application resources, for the CLUSTER_MANAGER cluster only.

        Args:
            deployment_name: Limits the work to one deployment.
        """
        project_name = await self.project_manager.get_name()
        logger.debug(f"Creating ArgoCD application for project: {project_name}")

        argo_workspace = await self.project_manager.get_argo_workspace()
        working_dir = await argo_workspace.get_working_dir()

        try:
            deployments = await self.project_manager.get_deployments(
                cluster_filter=True, deployment_name=deployment_name, deployment_names=deployment_names
            )
            if deployment_name:
                logger.info(f"Creating ArgoCD application only for deployment: {deployment_name}")

            if not deployments:
                logger.error("No deployments defined in project data")
                return False

            all_succeeded = True

            for deployment in deployments:
                cluster_name = deployment.get("cluster")
                base_namespace = deployment.get("namespace")
                namespace = get_prefixed_namespace(cluster_name, base_namespace)

                repo_name = deployment.get("repository")
                repositories = project_data.get("repositories", [])
                repo_info = next((r for r in repositories if r.get("name") == repo_name), None)

                if not repo_info:
                    logger.error(f"Repository not found: {repo_name}")
                    all_succeeded = False
                    continue

                app_name = generate_argocd_application_name(project_name, deployment["name"])

                cluster_name = deployment.get("cluster", "local")
                deployment_path = generate_deployment_manifest_path(
                    cluster_name, project_name, deployment["name"], repo_info.get("path", "")
                )

                # Make repository URL unique for ArgoCD (must match the repository secret URL)
                repo_url = make_argocd_repository_url_unique(repo_info.get("url"), project_name)
                logger.debug(f"Using unique repository URL for ArgoCD application: {repo_url}")

                argocd_app_content = self.generate_application_manifest(
                    name=app_name,
                    namespace=get_argo_namespace(cluster_name),
                    argo_project=generate_argocd_appproject_prefix(project_name, base_namespace),
                    repo_url=repo_url,
                    target_revision=repo_info.get("branch", "main"),
                    repo_path=deployment_path,
                    destination_namespace=namespace,
                    project_label=project_name,
                )

                template_filename = "argocd-application.yaml.jinja"
                output_filename = get_output_filename_from_template(template_filename, app_name)

                cluster_name = deployment.get("cluster")
                project_dir = os.path.join(
                    str(working_dir), generate_argocd_project_folder_path(str(cluster_name), str(project_name))
                )
                app_file_path = self._write_manifest_file(project_dir, output_filename, argocd_app_content)

                logger.info(f"Successfully created ArgoCD application file: {app_file_path}")

            return all_succeeded
        except Exception as e:
            logger.exception(f"Error creating ArgoCD application: {e}")
            return False

    async def create_project_application(self, project_data: dict[str, Any]) -> str | None:
        """Create the ArgoCD application for the PROJECT LEVEL of the deployments repo.

        Next to the AppProject in the same project directory, on sync-wave 0 while the
        deployment applications are on 1.

        Returns the name of the application, or None when nothing was written: no project
        level for this project on this cluster, or the repository was not found. The caller
        hangs its ordering gate on that name.
        """
        project_name = await self.project_manager.get_name()
        deployments = await self.project_manager.get_deployments(cluster_filter=True)
        if not deployments:
            logger.debug(f"Geen deployments op dit cluster voor '{project_name}'; geen projectapplicatie")
            return None

        # Repository and path come from the same functions the writer of the directory uses
        # (``_process_project_manifests``), so the application cannot point next to it.
        deployment = project_level_deployment(deployments)
        if deployment is None:
            return None
        cluster_name = str(deployment.get("cluster"))
        base_namespace = str(deployment.get("namespace"))
        namespace = get_prefixed_namespace(cluster_name, base_namespace)

        repositories = project_data.get("repositories", [])
        repo_info = next((r for r in repositories if r.get("name") == deployment.get("repository")), None)
        if not repo_info:
            logger.error(f"Repository not found for project application: {deployment.get('repository')}")
            return None

        project_path = generate_project_level_manifest_path(cluster_name, project_name, repo_info.get("path", ""))

        app_name = generate_argocd_project_application_name(project_name)
        content = self.generate_application_manifest(
            name=app_name,
            namespace=get_argo_namespace(cluster_name),
            argo_project=generate_argocd_appproject_prefix(project_name, base_namespace),
            repo_url=make_argocd_repository_url_unique(repo_info.get("url"), project_name),
            target_revision=repo_info.get("branch", "main"),
            repo_path=project_path,
            destination_namespace=namespace,
            project_label=project_name,
            sync_wave=0,
        )

        argo_workspace = await self.project_manager.get_argo_workspace()
        working_dir = await argo_workspace.get_working_dir()
        project_dir = os.path.join(str(working_dir), generate_argocd_project_folder_path(cluster_name, project_name))
        output_filename = get_output_filename_from_template("argocd-application.yaml.jinja", app_name)
        self._write_manifest_file(project_dir, output_filename, content)

        logger.info(f"Successfully created ArgoCD project application file: {output_filename}")
        return app_name

    async def create_kustomization_files(
        self, project_data: dict[str, Any], deployment_name: str | None = None
    ) -> None:
        """Write the kustomization.yaml of an ArgoCD project folder, for CLUSTER_MANAGER only.

        It lists every YAML file in the folder, so applications, repositories and appprojects
        all end up in it.

        Args:
            deployment_name: Accepted but unused.
        """
        argo_workspace = await self.project_manager.get_argo_workspace()
        working_dir = await argo_workspace.get_working_dir()

        project_name = await self.project_manager.get_name()

        cluster_name = settings.CLUSTER_MANAGER
        project_dir = os.path.join(
            str(working_dir), generate_argocd_project_folder_path(str(cluster_name), str(project_name))
        )

        self.project_manager._manifest_generator.create_kustomization_files(
            output_dir=project_dir,
            namespace=get_argo_namespace(cluster_name),  # Use ArgoCD namespace for the cluster
        )

        logger.info(
            f"Successfully created ArgoCD kustomization.yaml for project {project_name} for cluster {cluster_name}"
        )

    async def create_infrastructure_application(
        self, project_data: dict[str, Any], database_config: dict[str, Any], cluster_name: str
    ) -> bool:
        """Create the ArgoCD application and AppProject for a namespace-specific database.

        A separate application on sync-wave 0 that manages the infrastructure resources (the
        CNPG cluster and its superuser credentials) in the infrastructure namespace. Written
        in the ArgoCD applications repo under {cluster}/{project_name}-infrastructure/: the
        Application, the AppProject, the SOPS-encrypted repository secret, and the
        kustomization.

        Args:
            database_config: Image, storage and instances from the parsed services.
        """
        from opi.core.cluster_config import get_infrastructure_namespace

        project_name = await self.project_manager.get_name()
        logger.info(f"Creating ArgoCD infrastructure application for project: {project_name}")

        argo_workspace = await self.project_manager.get_argo_workspace()
        working_dir = await argo_workspace.get_working_dir()

        try:
            repositories = project_data.get("repositories", [])
            if not repositories:
                raise ValueError("No repositories defined in project data")
            repo_info = repositories[0]  # Use first repository

            infrastructure_namespace = get_infrastructure_namespace(cluster_name, project_name)

            app_name = f"{project_name}-infrastructure"

            infrastructure_path = generate_infrastructure_manifest_path(
                cluster_name, project_name, repo_info.get("path", "")
            )

            repo_url = make_argocd_repository_url_unique(repo_info.get("url"), project_name)

            async def _infrastructure() -> None:
                infra_folder_relative = generate_infrastructure_argocd_folder_path(cluster_name, project_name)
                infra_argo_dir = os.path.join(str(working_dir), infra_folder_relative)
                os.makedirs(infra_argo_dir, exist_ok=True)

                logger.info(f"Creating ArgoCD infrastructure resources in: {infra_argo_dir}")

                appproject_name = generate_infrastructure_application_name(project_name)
                appproject_content = self.generate_appproject_manifest(
                    name=appproject_name,
                    namespace=get_argo_namespace(cluster_name),
                    destination_namespace=infrastructure_namespace,
                    project_label=project_name,
                    source_repos=[repo_url],
                )

                appproject_filename = generate_infrastructure_argocd_appproject_filename(project_name)
                appproject_file_path = self._write_manifest_file(
                    infra_argo_dir, appproject_filename, appproject_content
                )

                logger.info(f"Created infrastructure AppProject: {appproject_file_path}")

                # Scope the secret name to the infrastructure app so it does not collide with the
                # parent project's repository secret (same repo URL) when both subfolders are
                # rendered into the single user-applications ArgoCD app (RepeatedResourceWarning).
                repo_name = repo_info.get("name", "main-repo")
                unique_repo_name = generate_argocd_repository_secret_name(
                    generate_infrastructure_application_name(project_name), repo_name
                )

                variables = await self.prepare_repository_variables(
                    name=unique_repo_name,
                    namespace=get_argo_namespace(cluster_name),
                    repository=repo_info,
                    repo_type="git",
                    project_name=project_name,
                )

                is_https = repo_info.get("url", "").startswith("https://")
                template_filename = "argo-repository-https.yaml.jinja" if is_https else "argo-repository.yaml.jinja"
                template_path = os.path.join(settings.MANIFESTS_PATH, template_filename)
                output_filename = get_output_filename_from_template(template_filename, unique_repo_name)

                repo_secret_path = self.project_manager._manifest_generator.create_manifest_file(
                    template_path=template_path,
                    values=variables,
                    output_dir=infra_argo_dir,
                    output_filename=output_filename,
                    use_sops=True,
                )
                logger.info(f"Created repository secret: {os.path.basename(repo_secret_path)}")

                await asyncio.to_thread(
                    encrypt_to_sops_files_or_fail,
                    infra_argo_dir,
                    cast("str", settings.SOPS_AGE_PUBLIC_KEY),
                    f"ArgoCD infrastructuur repository-secret (git SSH key/HTTPS-wachtwoord) voor project '{project_name}'",
                    private_key=settings.SOPS_AGE_PRIVATE_KEY,
                )

                argocd_app_content = self.generate_application_manifest(
                    name=app_name,
                    namespace=get_argo_namespace(cluster_name),
                    argo_project=appproject_name,
                    repo_url=repo_url,
                    target_revision=repo_info.get("branch", "main"),
                    repo_path=infrastructure_path,
                    destination_namespace=infrastructure_namespace,
                    project_label=project_name,
                )

                argocd_app_content = argocd_app_content.replace(
                    'argocd.argoproj.io/sync-wave: "1"', 'argocd.argoproj.io/sync-wave: "0"'
                )

                app_filename = generate_infrastructure_argocd_application_filename(project_name)
                app_file_path = self._write_manifest_file(infra_argo_dir, app_filename, argocd_app_content)

                logger.info(f"Created infrastructure Application: {app_file_path}")

                kustomization_success = self.project_manager._manifest_generator.create_kustomization_files(
                    output_dir=infra_argo_dir,
                    namespace=None,  # No namespace override for ArgoCD resources
                )
                if not kustomization_success:
                    raise RuntimeError("Failed to create kustomization files for infrastructure ArgoCD resources")

                logger.info("Created kustomization.yaml for infrastructure ArgoCD resources")

            await self._mutate_and_push_argo(
                f"Add ArgoCD infrastructure application for {project_name} in {cluster_name} cluster",
                _infrastructure,
            )

            logger.info(f"Successfully committed ArgoCD infrastructure resources for project '{project_name}'")

            return True

        except Exception as e:
            logger.exception(f"Error creating infrastructure ArgoCD application: {e}")
            return False

    async def wait_for_application_created(
        self, app_name: str, timeout: int = 120, poll_interval: int = POLL_INTERVAL_SECONDS
    ) -> bool:
        """Poll the ArgoCD API until the application exists.

        Raises:
            TimeoutError: the application did not appear within ``timeout``.
        """
        from opi.connectors.argo import create_argo_connector

        logger.info(
            f"Waiting for ArgoCD application '{app_name}' to be created (timeout: {timeout}s, poll: {poll_interval}s)"
        )

        argo_connector = create_argo_connector()

        if not await argo_connector.login():
            raise RuntimeError("Failed to login to ArgoCD")

        elapsed_time = 0
        while elapsed_time < timeout:
            # A newer task whose scope covers this project will reprocess from the
            # committed state, so waiting out this timeout only delays it.
            await raise_if_superseded(f"waiting for ArgoCD application '{app_name}' to be created")
            try:
                if await argo_connector.application_exists(app_name):
                    logger.info(f"ArgoCD application '{app_name}' has been created!")
                    return True

                logger.debug(
                    f"Application '{app_name}' not found yet, waiting {poll_interval}s... (elapsed: {elapsed_time}s)"
                )
                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval

            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as e:
                logger.warning(f"Error checking application creation status: {e}, retrying...")
                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval

        # Name in the same line what the umbrella compared: if that revision is not our pushed
        # commit, the wait was on a reconcile that had not seen our change at all.
        error_msg = (
            f"Timeout waiting for ArgoCD application '{app_name}' to be created after {timeout}s "
            f"(gepushte commit: {self.last_pushed_argo_commit or 'onbekend'}, "
            f"user-applications revisie: {self.last_umbrella_revision or 'onbekend'}, "
            f"reconciledAt: {self.last_umbrella_reconciled_at or 'onbekend'})"
        )
        logger.error(error_msg)
        raise TimeoutError(error_msg)

    async def wait_for_infrastructure_ready(
        self,
        project_name: str,
        cluster_name: str,
        timeout: int = 300,
        poll_interval: int = POLL_INTERVAL_SECONDS,
        refreshed_after: str | None = None,
    ) -> bool:
        """Poll until the infrastructure application (the database cluster) is synced and healthy.

        Args:
            refreshed_after: ISO-8601 ``reconciledAt`` timestamp from
                ``refresh_application``. Given, terminal states are ignored until
                ``reconciledAt`` moves past this value.

        Raises:
            TimeoutError: the infrastructure did not become ready within ``timeout``.
            RuntimeError: the application failed to sync, or became unhealthy.
        """
        from opi.connectors.argo import create_argo_connector

        app_name = f"{project_name}-infrastructure"
        logger.info(
            f"Waiting for infrastructure application '{app_name}' to be ready "
            f"(timeout: {timeout}s, poll: {poll_interval}s, refreshed_after={refreshed_after})"
        )

        argo_connector = create_argo_connector()

        if not await argo_connector.login():
            raise RuntimeError(f"Failed to login to ArgoCD for cluster {cluster_name}")

        start = asyncio.get_running_loop().time()
        elapsed_time = 0.0
        while elapsed_time < timeout:
            try:
                status_data = await argo_connector.get_application_status(app_name)

                if not status_data:
                    logger.warning(f"Could not get status for application {app_name}, retrying...")
                    await asyncio.sleep(poll_interval)
                    elapsed_time = _elapsed_since(start)
                    continue

                sync_status = status_data.get("status", {}).get("sync", {}).get("status")
                health_status = status_data.get("status", {}).get("health", {}).get("status")

                logger.info(f"Infrastructure status: sync={sync_status}, health={health_status}")

                if sync_status == "Synced" and health_status == "Healthy":
                    logger.info(f"Infrastructure application '{app_name}' is ready!")
                    return True

                status_is_fresh = self.status_reflects_refresh(status_data, refreshed_after)
                if not status_is_fresh:
                    logger.debug(
                        f"Infrastructure '{app_name}': status is stale "
                        f"(reconciledAt={status_data.get('status', {}).get('reconciledAt', '')} "
                        f"<= refreshed_after={refreshed_after})"
                    )

                if status_is_fresh:
                    # A terminal ArgoCD condition (ComparisonError etc.) means the manifests
                    # could not be generated or compared - independent of health, which may
                    # still read Healthy from the last good reconciliation. Surface it now
                    # instead of waiting for the timeout.
                    condition_error = terminal_condition_message(status_data)
                    if condition_error:
                        logger.error(
                            "Infrastructure application '%s' terminal condition: %s", app_name, condition_error
                        )
                        raise RuntimeError(
                            f"Infrastructure application '{app_name}' cannot be rendered/compared: "
                            f"{condense_render_error(condition_error)}"
                        )

                    operation_state = status_data.get("status", {}).get("operationState", {})
                    operation_phase = operation_state.get("phase")
                    operation_message = operation_state.get("message", "")

                    if operation_phase in ("Failed", "Error"):
                        error_msg = f"Infrastructure application '{app_name}' sync failed: {operation_phase}"
                        logger.error(error_msg)
                        if operation_message:
                            logger.error(f"  Sync error: {operation_message}")
                        sync_result = operation_state.get("syncResult", {})
                        if sync_result:
                            resources = sync_result.get("resources", [])
                            for resource in resources:
                                if resource.get("status") == "SyncFailed":
                                    kind = resource.get("kind")
                                    name = resource.get("name")
                                    msg = resource.get("message")
                                    logger.error(f"  Resource failed: {kind}/{name} - {msg}")
                        raise RuntimeError(error_msg)

                    # Check for failure states - only Degraded is a true failure
                    # "Missing" means resources are still being created, which is expected during initial deployment
                    if health_status == "Degraded":
                        error_msg = f"Infrastructure application '{app_name}' is in failed state: {health_status}"
                        logger.error(error_msg)
                        conditions = status_data.get("status", {}).get("conditions", [])
                        for condition in conditions:
                            logger.error(f"  Condition: {condition.get('type')} - {condition.get('message')}")
                        raise RuntimeError(error_msg)

                if health_status == "Missing":
                    logger.info(
                        f"Infrastructure resources are being created, waiting {poll_interval}s... (elapsed: {elapsed_time}s)"
                    )
                elif health_status == "Progressing":
                    logger.info(
                        f"Infrastructure resources are progressing, waiting {poll_interval}s... (elapsed: {elapsed_time}s)"
                    )
                else:
                    logger.debug(
                        f"Infrastructure not ready yet (health={health_status}, fresh={status_is_fresh}), "
                        f"waiting {poll_interval}s... (elapsed: {elapsed_time}s)"
                    )
                await asyncio.sleep(poll_interval)
                elapsed_time = _elapsed_since(start)

            except ArgoTransportError as e:
                # A transport error, not an application status. Keep polling within the budget.
                logger.warning(f"Infrastructure status of '{app_name}' unreachable: {e}, retrying...")
                await asyncio.sleep(poll_interval)
                elapsed_time = _elapsed_since(start)
            except RuntimeError:
                raise
            except (OSError, ValueError, KeyError, TypeError) as e:
                logger.warning(f"Error checking infrastructure status: {e}, retrying...")
                await asyncio.sleep(poll_interval)
                elapsed_time = _elapsed_since(start)

        error_msg = f"Timeout waiting for infrastructure application '{app_name}' to be ready after {timeout}s"
        logger.error(error_msg)
        protocol = "https" if settings.ARGOCD_USE_TLS else "http"
        port_suffix = "" if settings.ARGOCD_PORT in (80, 443) else f":{settings.ARGOCD_PORT}"
        argocd_url = f"{protocol}://{settings.ARGOCD_HOST}{port_suffix}"
        logger.error(f"ArgoCD dashboard: {argocd_url}/applications/{app_name}")
        raise TimeoutError(error_msg)

    @staticmethod
    def status_reflects_refresh(status_data: dict[str, Any], refreshed_after: str | None) -> bool:
        """Whether this status was reconciled AFTER our own refresh.

        Without that a ``Synced`` proves nothing about the commit just pushed: the status may
        still be from the previous reconcile. Without ``refreshed_after`` there is nothing to
        compare and the status counts as fresh.
        """
        if not refreshed_after:
            return True
        return str(status_data.get("status", {}).get("reconciledAt", "")) > refreshed_after

    async def wait_for_application_synced(
        self,
        app_name: str,
        timeout: int = 300,
        poll_interval: int = POLL_INTERVAL_SECONDS,
        refreshed_after: str | None = None,
        on_progressing: Callable[[int], Awaitable[None]] | None = None,
        on_poll: Callable[[int, dict[str, Any], bool], Awaitable[None]] | None = None,
    ) -> bool:
        """Poll the ArgoCD API until the application is Synced and Healthy, or terminally failed.

        Args:
            refreshed_after: ISO-8601 ``reconciledAt`` timestamp from
                ``refresh_application``. Given, the loop does not treat ``Degraded`` or
                ``Failed``/``Error`` as terminal until ArgoCD has reconciled AFTER this
                timestamp, which keeps a status from a previous reconciliation out.
            on_progressing: Async callback per poll while health is ``Progressing`` and the
                status is fresh, given the elapsed time in seconds. An exception from it
                propagates to the caller.
            on_poll: Async callback per poll that read a status and is still waiting,
                whatever sync and health say, given the elapsed time, the application status
                and whether that status is fresh. Runs before ``on_progressing``.

        Raises:
            TimeoutError: the application did not become ready within ``timeout``.
            ApplicationGoneError: the application was visible during this wait and then
                disappeared, so waiting on is pointless.
            RuntimeError: the application entered a terminal failure state.
        """
        from opi.connectors.argo import create_argo_connector

        logger.info(
            f"Waiting for ArgoCD application '{app_name}' to be synced and healthy "
            f"(timeout: {timeout}s, poll: {poll_interval}s, refreshed_after={refreshed_after})"
        )

        argo_connector = create_argo_connector()
        if not await argo_connector.login():
            raise RuntimeError("Failed to login to ArgoCD")

        start = asyncio.get_running_loop().time()
        elapsed_time = 0.0
        # Wall-clock moment of the last refresh asked for here. Not
        # elapsed_time % REFRESH_EVERY_SECONDS: a wall clock does not hit that point exactly.
        last_refresh = 0.0
        # Has the application been read successfully in THIS wait? While it has not, an empty
        # answer means "it still has to appear" and the loop keeps polling. After that the same
        # empty answer means "it is gone", which is an ending and not a read error.
        seen = False
        # The moment the sync finished, so the time UNTIL Synced and the time from Synced to
        # Healthy stay separately measurable: the first is ArgoCD, the second is the pod.
        synced_at: float | None = None
        while elapsed_time < timeout:
            await raise_if_superseded(f"waiting for ArgoCD application '{app_name}' to sync")
            try:
                status_data = await argo_connector.get_application_status(app_name)

                if not status_data:
                    if seen:
                        raise ApplicationGoneError(
                            f"Application '{app_name}' verdween tijdens het wachten "
                            f"(na {elapsed_time}s); een andere taak heeft hem waarschijnlijk verwijderd"
                        )
                    logger.debug(f"Could not get status for '{app_name}', retrying...")
                    await asyncio.sleep(poll_interval)
                    elapsed_time = _elapsed_since(start)
                    continue
                seen = True

                sync_status = status_data.get("status", {}).get("sync", {}).get("status")
                health_status = status_data.get("status", {}).get("health", {}).get("status")

                if sync_status == "Synced" and synced_at is None:
                    synced_at = _elapsed_since(start)
                    observe_rollout_phase(PHASE_ARGO_WAIT_SYNCED, synced_at)

                if sync_status == "Synced" and health_status == "Healthy":
                    logger.info(f"Application '{app_name}' is synced and healthy")
                    observe_rollout_phase(PHASE_ARGO_WAIT_HEALTHY, _elapsed_since(start) - (synced_at or 0.0))
                    return True

                # Once the status has been Progressing for a minute, ask for a refresh: ArgoCD
                # re-evaluates on its own only after timeout.reconciliation, so a missed watch
                # event would leave the health frozen until our timeout. Only on Progressing,
                # because Degraded has its own handling below that must not be delayed.
                if elapsed_time - last_refresh >= REFRESH_EVERY_SECONDS and health_status == "Progressing":
                    last_refresh = elapsed_time
                    logger.info(
                        "Application '%s' staat %ds op Progressing; opnieuw een refresh gevraagd "
                        "(ArgoCD hertoetst zelf pas na timeout.reconciliation)",
                        app_name,
                        elapsed_time,
                    )
                    # Only overwrite when the refresh yields a timestamp: a failed refresh
                    # returns None, which would clear the freshness threshold and let an old
                    # status count as fresh.
                    reconciled_again = await argo_connector.refresh_application(app_name)
                    if reconciled_again:
                        refreshed_after = reconciled_again

                # Determine whether the status reflects our refresh.
                # If reconciledAt is still <= refreshed_after, ArgoCD
                # hasn't processed our changes yet - don't treat errors
                # as terminal.
                status_is_fresh = self.status_reflects_refresh(status_data, refreshed_after)
                if not status_is_fresh:
                    logger.debug(
                        f"Application '{app_name}': status is stale "
                        f"(reconciledAt={status_data.get('status', {}).get('reconciledAt', '')} "
                        f"<= refreshed_after={refreshed_after}), not evaluating terminal states yet"
                    )

                if status_is_fresh:
                    # Check for a terminal ArgoCD condition (ComparisonError etc.). This is
                    # where a broken kustomization / CMP render shows up: sync goes to
                    # Unknown with no sync operation, so without this the poll loop would run
                    # to the full timeout and report a generic time-out for a deployment that
                    # was never rendered. Raising here surfaces the real message instead.
                    condition_error = terminal_condition_message(status_data)
                    if condition_error:
                        # Log the full condition (helpful for debugging), but raise the
                        # condensed tail: ArgoCD's message can be kBs of echoed script source
                        # with the real error at the very end.
                        logger.error("Application '%s' terminal condition: %s", app_name, condition_error)
                        raise RuntimeError(
                            f"Application '{app_name}' cannot be rendered/compared: "
                            f"{condense_render_error(condition_error)}"
                        )

                    operation_state = status_data.get("status", {}).get("operationState", {})
                    operation_phase = operation_state.get("phase")
                    if operation_phase in ("Failed", "Error"):
                        operation_message = operation_state.get("message", "")
                        error_msg = f"Application '{app_name}' sync failed: {operation_phase}"
                        if operation_message:
                            error_msg += f" - {operation_message}"
                        logger.error(error_msg)
                        raise RuntimeError(error_msg)

                # Invoke the health check callback when the application is not
                # yet healthy.  The callback uses kubectl to check pod state
                # directly (OOM, CrashLoopBackOff, ImagePullBackOff), so it
                # does not depend on ArgoCD's reconciliation freshness.
                # This covers both "Progressing" (new deploy) and "Degraded"
                # (refresh of an already-failing app where ArgoCD skips the
                # Progressing phase).  Runs before the terminal Degraded
                # check so that per-component details are available.
                call_on_progressing = on_progressing is not None and health_status in ("Progressing", "Degraded")
                poll_status = (status_data, status_is_fresh)

                # Check for terminal health failure (only if no callback
                # will run — the callback provides richer per-component
                # details via DeploymentHealthError)
                if status_is_fresh and health_status == "Degraded" and not call_on_progressing:
                    error_msg = f"Application '{app_name}' is degraded"
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

                logger.debug(
                    f"Application '{app_name}': sync={sync_status}, health={health_status}, "
                    f"fresh={status_is_fresh}, waiting {poll_interval}s... (elapsed: {elapsed_time}s)"
                )

                await asyncio.sleep(poll_interval)
                elapsed_time = _elapsed_since(start)

            except ArgoTransportError as e:
                # A transport error, not an application status; a timeout here would break off
                # the whole wait.
                logger.warning(f"Status of '{app_name}' unreachable: {e}, retrying...")
                await asyncio.sleep(poll_interval)
                elapsed_time = _elapsed_since(start)
                continue
            except RuntimeError, TimeoutError:
                raise
            except PermissionError:
                # Application may not be accessible yet (AppProject not synced)
                logger.debug(
                    f"Permission denied for '{app_name}', waiting for AppProject sync... (elapsed: {elapsed_time}s)"
                )
                await asyncio.sleep(poll_interval)
                elapsed_time = _elapsed_since(start)
                continue
            except (OSError, ValueError, KeyError, TypeError) as e:
                logger.warning(f"Error checking status of '{app_name}': {e}, retrying...")
                await asyncio.sleep(poll_interval)
                elapsed_time = _elapsed_since(start)
                continue

            # Invoke the callbacks outside try/except so their
            # exceptions propagate directly to the caller.
            if on_poll is not None:
                await on_poll(int(elapsed_time), *poll_status)
            if call_on_progressing:
                await on_progressing(int(elapsed_time))  # type: ignore[misc]

        raise TimeoutError(f"Timeout waiting for application '{app_name}' to be synced and healthy after {timeout}s")
