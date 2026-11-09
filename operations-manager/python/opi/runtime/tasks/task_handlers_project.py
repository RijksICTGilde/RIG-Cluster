"""Task handlers for project creation and upsert deployment.

These handlers are called by TaskWorker. Each receives a payload dict and a
PersistentTaskProgressManager instance, and returns a result dict.

They have NO dependency on FastAPI Request objects.
"""

import logging
import time
from typing import Any

from opi.core.cluster_config import is_reserved_project_name
from opi.services.oom_watcher import reset_oom_tune_attempts
from opi.services.tasks import progress_steps as steps
from opi.services.tasks.task_errors import CLIENT_ERROR_TYPES, platform_failure_fields
from opi.services.tasks.task_rollout import note_rollout_skipped, rollout_requested, skipped_processing
from opi.utils.yaml_util import load_yaml_from_string

logger = logging.getLogger(__name__)


async def handle_create_project(payload: dict, progress: Any) -> dict:
    """Handle async project creation task.

    Supports two paths:

    1. Full generation -- payload contains SelfServiceProjectRequest fields
       (project_name, display_name, components, etc.) and the handler
       generates YAML via generate_self_service_project_yaml().
    2. Pre-built YAML -- payload contains ``yaml_content`` directly (produced
       by the wizard with editables and generators).  YAML generation is
       skipped.
    """
    from opi.manager.project_manager import ProjectManager
    from opi.runtime.tasks.simple_background import _monitor_argocd_and_deployment
    from opi.services.project_store import ConflictError, get_project_store
    from opi.services.project_utils import RESERVED_PROJECT_NAME_MESSAGE, generate_self_service_project_yaml
    from opi.utils.project_names import validate_project_name

    start_time = time.time()
    project_name: str = payload["project_name"]
    pre_built_yaml: str | None = payload.get("yaml_content")
    # Scope processing to specific deployment(s) when the caller specified them:
    # ``deployment_name`` for a single deployment (e.g. a modal webadres edit) or
    # ``deployment_names`` for an explicit set (e.g. domain approval affecting one
    # or more deployments). When both are absent (full project create/update) the
    # filter stays None and all deployments are processed as before.
    deployment_name: str | None = payload.get("deployment_name")
    deployment_names: list[str] | None = payload.get("deployment_names")

    validate_task = progress.add_task(steps.PROJECT_NAME_CHECK)

    if not validate_project_name(project_name):
        error_msg = f"Invalid project name format: {project_name}"
        progress.fail_task(validate_task, error_msg)
        progress.fail_project(error_msg)
        return {
            "project_name": project_name,
            "status": "failed",
            "error": error_msg,
            "error_type": "invalid_project_name",
        }

    if payload.get("is_new_project", False) and is_reserved_project_name(project_name):
        error_msg = RESERVED_PROJECT_NAME_MESSAGE.format(name=project_name)
        progress.fail_task(validate_task, error_msg)
        progress.fail_project(error_msg)
        return {
            "project_name": project_name,
            "status": "failed",
            "error": error_msg,
            "error_type": "invalid_project_name",
        }

    progress.complete_task(validate_task)

    if pre_built_yaml is not None:
        # Path 2: wizard-provided YAML. No step: nothing is checked here, and the
        # validation happens in "Projectbestand valideren" during processing.
        logger.debug(
            "Using pre-built YAML content for %s (%d chars)",
            project_name,
            len(pre_built_yaml),
        )
        yaml_content = pre_built_yaml
    else:
        # Path 1: generate YAML from the request payload.
        yaml_task = progress.add_task(steps.PROJECT_FILE_BUILD)
        try:
            # generate_self_service_project_yaml expects an object with attributes.
            # Build a lightweight namespace from the payload so it can use attribute
            # access (the generator uses getattr / dot-notation).
            from types import SimpleNamespace

            project_data = SimpleNamespace(**payload)
            yaml_content = await generate_self_service_project_yaml(project_data)
            logger.debug(
                "Generated YAML content for %s (%d chars)",
                project_name,
                len(yaml_content),
            )
            progress.complete_task(yaml_task)
        except Exception as exc:
            error_msg = f"Failed to generate YAML: {exc}"
            progress.fail_task(yaml_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "project_name": project_name,
                "status": "failed",
                "error": error_msg,
                "error_type": "internal_error",
            }

    git_task = progress.add_task(steps.PROJECT_FILE_SAVE)

    try:
        project_file_path = f"projects/{project_name}.yaml"
        commit_message = (
            f"Create project {project_name}"
            if payload.get("is_new_project", False)
            else f"Update project {project_name}"
        )

        # Project-create flow: refuse if the project file already exists.
        # Without this a tenant could pick another tenant's name and take
        # over their project on the next reload. Edit/update flows that
        # reuse this task type (modal-edit, component delete) MUST set
        # is_new_project=False so legitimate overwrites still work.
        # Asked of the store, not of a private clone: the store owns the projects
        # repo, and reconcile() first costs one ls-remote (~60ms) when nothing
        # changed, so a project created by another cluster is still seen.
        store = get_project_store()
        is_new_project = payload.get("is_new_project", False)
        if is_new_project:
            await store.reconcile()
        if is_new_project and await store.read_path(project_file_path) is not None:
            error_msg = (
                f"Project '{project_name}' bestaat al. "
                f"Kies een andere projectnaam; een bestaand project kan niet "
                f"via aanmaken worden overschreven."
            )
            progress.fail_task(git_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "project_name": project_name,
                "status": "failed",
                "error": error_msg,
                "error_type": "already_exists",
            }

        project_data_dict = load_yaml_from_string(yaml_content)
        if not project_data_dict:
            error_msg = f"Kon de projectconfiguratie voor '{project_name}' niet inlezen"
            progress.fail_task(git_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "project_name": project_name,
                "status": "failed",
                "error": error_msg,
                "error_type": "validation_error",
            }

        # Do not inject a connector: ProjectManager would then skip the store's warm working
        # copy, and the processing below would read a clone from before this write.
        project_manager = ProjectManager(project_file_relative_path=project_file_path)

        if payload.get("project_file_already_saved", False):
            # The caller saved through the store; writing again can only do harm. See
            # tests/test_project_store_gelijktijdig_ui_en_api.py.
            project_data_dict = await project_manager.get_contents()
            logger.info("Project file at %s was already saved by the caller; not rewriting it", project_file_path)
        else:
            # Publish the complete project file: schema validation, canonical dumper,
            # commit + push in one shot. base_version lets the store merge concurrent changes.
            base: dict[str, Any] | None = None
            if not is_new_project:
                base_version: str | None = payload.get("base_version")
                if base_version:
                    base = await store.read_version(base_version)
                    if base is None:
                        logger.warning(
                            "Version %s of %s is no longer readable; saving without a concurrency check",
                            base_version,
                            project_file_path,
                        )
                else:
                    logger.warning(
                        "No base_version in the create_project payload for %s; that caller is not wired up yet, "
                        "so a concurrent change to this project would be overwritten",
                        project_name,
                    )

            await project_manager.save_and_commit_project(project_data_dict, commit_message, base=base)
            logger.info("Project file created and pushed at %s", project_file_path)

        progress.complete_task(git_task)
    except ConflictError as exc:
        logger.warning("Concurrent change blocked the save of %s: %s", project_name, exc)
        error_msg = (
            "Dit project is ondertussen door iemand anders gewijzigd en de wijzigingen konden niet "
            "automatisch worden samengevoegd. Herlaad de pagina en probeer het opnieuw."
        )
        progress.fail_task(git_task, error_msg)
        progress.fail_project(error_msg)
        return {"project_name": project_name, "status": "failed", "error": error_msg, "error_type": "conflict"}
    except Exception as exc:
        error_msg = f"Failed Git operations: {exc}"
        progress.fail_task(git_task, error_msg)
        progress.fail_project(error_msg)
        return {"project_name": project_name, "status": "failed", "error": error_msg, "error_type": "internal_error"}

    # A caller that does not ask for the rollout stops here: process_project reports "no
    # deployments for this cluster" as an error and would fail a project that was created
    # correctly.
    if not rollout_requested(payload):
        await project_manager.close()
        note_rollout_skipped(progress)
        progress.update_current_step(steps.PROJECT_SAVED)
        progress.complete_project()
        elapsed_time = time.time() - start_time
        return {
            "project_name": project_name,
            "project_description": payload.get("project_description", "No description"),
            "components_count": len(payload.get("components", [])),
            "elapsed_time": f"{elapsed_time:.2f}",
            "file_path": project_file_path,
            "status": "success",
            "processing": skipped_processing(),
        }

    # Clear OOM state for each deployment at start, before the rollout (not after).
    # Outside the OOM_WATCHER_ENABLED guard: the inline health-check callback charges the
    # budget without reading that setting, so its counter must be cleared here regardless.
    if isinstance(project_data_dict, dict):
        for dep in project_data_dict.get("deployments", []):
            if dep_name := dep.get("name", ""):
                reset_oom_tune_attempts(project_name, dep_name)

    # The notice says why a new project takes longer, and what a timeout means. No
    # reassurance that a timeout leaves the creation standing: the wait at project level is
    # fail-closed, so a timeout here means the deployment applications never reached git.
    # The wait that reassurance does hold for is `_monitor_argocd_and_deployment`
    # (simple_background.py), and it writes it there itself, with the app status next to it.
    if payload.get("is_new_project", False):
        progress.complete_task(progress.add_task(steps.NEW_PROJECT_NOTICE))

    try:
        try:
            processing_result = await project_manager.process_project_from_git(
                project_file_path, progress, deployment_name=deployment_name, deployment_names=deployment_names
            )
            logger.info("Project processing completed, result: %s", processing_result)
        finally:
            await project_manager.close()

        if processing_result:
            # ArgoCD monitoring, under the rollout phase: a line of its own would say the
            # same as "uitgerold en gezond" right above it.
            monitor_task = progress.current_phase or progress.add_task(steps.ROLLOUT_CHECK)
            await _monitor_argocd_and_deployment(
                _task_id="",  # not used by the monitor helper
                project_name=project_name,
                task_progress_manager=progress,
                monitor_task=monitor_task,
                # Scope the wait to what this task touched. Without it another
                # deployment's broken app blocks this one's wait indefinitely.
                deployment_names=deployment_names or ([deployment_name] if deployment_name else None),
            )

            # Schedule fire-and-forget OOM watcher for each deployment
            from opi.core.config import settings as app_settings
            from opi.services.oom_watcher import schedule_oom_check

            if app_settings.OOM_WATCHER_ENABLED and isinstance(project_data_dict, dict):
                for dep in project_data_dict.get("deployments", []):
                    if dep_name := dep.get("name", ""):
                        schedule_oom_check(project_name, dep_name)

            progress.update_current_step(steps.PROJECT_DEPLOYED)
            progress.complete_project()

            elapsed_time = time.time() - start_time
            result: dict[str, Any] = {
                "project_name": project_name,
                "project_description": payload.get("project_description", "No description"),
                "components_count": len(payload.get("components", [])),
                "elapsed_time": f"{elapsed_time:.2f}",
                "file_path": project_file_path,
                "status": "success",
                # On the successful branch as well: "rolled out, but not healthy" is formally
                # a success, and this is the channel that carries the per-component story.
                **(
                    {"processing": {"status": "completed", "component_failures": component_failures}}
                    if (component_failures := project_manager.get_component_failures())
                    else {}
                ),
            }
            logger.info(
                "Project creation completed successfully: %s (took %.2fs)",
                project_name,
                elapsed_time,
            )
            return result
        else:
            error_msg = project_manager.get_processing_error() or "Project processing failed"
            progress.fail_project(error_msg)
            component_failures = project_manager.get_component_failures()
            return {
                "project_name": project_name,
                "status": "failed",
                "error": error_msg,
                # The rollout itself failed. Deliberately not an input error and
                # deliberately not "ours": a component that does not become healthy can be
                # the user's image and can be the cluster, and which of the two is in
                # component_failures. This type therefore lands at the client as "not
                # attributable", which is the honest outcome here.
                "error_type": "processing_failed",
                **platform_failure_fields(project_manager),
                "processing": {
                    "status": "failed",
                    "error": error_msg,
                    **({"component_failures": component_failures} if component_failures else {}),
                },
            }

    except Exception as exc:
        error_msg = f"Failed deployment: {exc}"
        progress.fail_project(error_msg)
        return {"project_name": project_name, "status": "failed", "error": error_msg, "error_type": "internal_error"}


async def handle_upsert_deployment(payload: dict, progress: Any) -> dict:
    """Handle async upsert deployment task."""
    from opi.manager.project_manager import ProjectManager
    from opi.services.project_utils import normalize_container_image
    from opi.utils.naming import sanitize_kubernetes_name
    from opi.utils.project_names import validate_project_name

    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    components: list[dict[str, str]] = payload["components"]
    clone_from: str | None = payload.get("clone_from") or payload.get("cloneFrom")
    force_clone: bool = payload.get("force_clone") or payload.get("forceClone", False)
    domain_format: str | None = payload.get("domain_format")
    subdomain: str | None = payload.get("subdomain")
    base_domain: str | None = payload.get("base_domain")
    environment: str | None = payload.get("environment")
    actor: str | None = payload.get("actor")

    # Clear OOM state at start, and outside the OOM_WATCHER_ENABLED guard: see
    # handle_create_project.
    reset_oom_tune_attempts(project_name, deployment_name)

    project_manager: ProjectManager | None = None

    try:
        validate_task = progress.add_task(steps.NAMES_CHECK)

        if not validate_project_name(project_name):
            error_msg = (
                "Invalid project name format. Must start with lowercase letter, "
                "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
            )
            progress.fail_task(validate_task, error_msg, client_error=True)
            progress.fail_project(error_msg)
            return {
                "deployment_name": deployment_name,
                "status": "failed",
                "error": error_msg,
                "error_type": "validation_error",
            }

        sanitized_name = sanitize_kubernetes_name(deployment_name)
        if sanitized_name != deployment_name.lower():
            error_msg = (
                f"Invalid deployment name. Use lowercase letters, numbers, and "
                f"hyphens only. Suggested: {sanitized_name}"
            )
            progress.fail_task(validate_task, error_msg, client_error=True)
            progress.fail_project(error_msg)
            return {
                "deployment_name": deployment_name,
                "status": "failed",
                "error": error_msg,
                "error_type": "validation_error",
            }

        progress.complete_task(validate_task)

        upsert_task = progress.add_task(steps.DEPLOYMENT_SAVE, subject=deployment_name)

        project_file_relative_path = f"projects/{project_name}.yaml"
        project_manager = ProjectManager(
            project_file_relative_path=project_file_relative_path,
        )

        # Build component dicts in the shape expected by project_manager
        from types import SimpleNamespace

        component_objects = [SimpleNamespace(reference=c["reference"], image=c["image"]) for c in components]

        result = await project_manager.upsert_deployment(
            deployment_name=deployment_name,
            components=component_objects,
            clone_from=clone_from,
            force_clone=force_clone,
            domain_format=domain_format,
            subdomain=subdomain,
            base_domain=base_domain,
            environment=environment,
            actor=actor,
        )

        if not result["success"]:
            error_msg = result.get("error", "Unknown upsert error")
            error_type = result.get("error_type", "unknown")
            progress.fail_task(upsert_task, error_msg, client_error=error_type in CLIENT_ERROR_TYPES)
            progress.fail_project(error_msg)
            return {
                "deployment_name": deployment_name,
                "status": "failed",
                "error": error_msg,
                "error_type": error_type,
            }

        progress.complete_task(upsert_task)

        # ArgoCD Application/AppProject resources only change when a new
        # deployment is created.  Image updates don't touch ArgoCD resources.
        is_new_deployment = result.get("created", False)
        action = "created" if is_new_deployment else "updated"

        urls: dict[str, dict[str, Any]] = {}

        if not rollout_requested(payload):
            note_rollout_skipped(progress)
            succeeded = True
            processing: dict[str, Any] = skipped_processing()
        else:
            processing_result = await project_manager.process_project_from_git(
                project_file_relative_path,
                task_progress_manager=progress,
                deployment_name=deployment_name,
                force_clone=force_clone,
                argocd_resources_changed=is_new_deployment,
            )

            # Collect URLs from deployment results
            deployment_results = project_manager.get_deployment_results(deployment_name)
            for dep_name, dep_result in deployment_results.items():
                urls[dep_name] = {
                    "cluster": dep_result.cluster,
                    "urls": dep_result.urls,
                }
                # Report web addresses for each component URL
                for url_name, url_value in (dep_result.urls or {}).items():
                    progress.update_component_web_address(url_name, url_value)

            if processing_result:
                # Schedule fire-and-forget OOM watcher
                from opi.core.config import settings
                from opi.services.oom_watcher import schedule_oom_check

                if settings.OOM_WATCHER_ENABLED:
                    oom_attempt = payload.get("oom_watch_attempt", 1)
                    schedule_oom_check(
                        project_name,
                        deployment_name,
                        attempt=oom_attempt,
                    )
            else:
                processing_error = project_manager.get_processing_error() or "Deployment processing failed"
                progress.fail_project(processing_error)

            succeeded = bool(processing_result)
            processing = {
                "status": "completed" if succeeded else "failed",
                **(
                    {"error": project_manager.get_processing_error()}
                    if not succeeded and project_manager.get_processing_error()
                    else {}
                ),
                **(
                    {"component_failures": component_failures}
                    if (component_failures := project_manager.get_component_failures())
                    else {}
                ),
            }

        response: dict[str, Any] = {
            "status": "success" if succeeded else "failed",
            "message": (
                f"Deployment '{deployment_name}' {action} successfully"
                if succeeded
                else (project_manager.get_processing_error() or f"Deployment '{deployment_name}' processing failed")
            ),
            "deployment": {
                "name": deployment_name,
                "project": project_name,
                "components": [
                    {
                        "reference": c["reference"],
                        "image": normalize_container_image(c["image"])[0],
                    }
                    for c in components
                ],
                "forceClone": force_clone,
                "created": result.get("created", False),
            },
            "urls": urls,
            # A deployment with a domain that is not approved does run, but on the default
            # cluster address. So that is in "urls" too, and without this field there is
            # nothing that says why a different address than the one asked for is there.
            "approvals": result.get("approvals", []),
            "processing": processing,
        }
        if result.get("warnings"):
            response["warnings"] = result["warnings"]
        return response

    except Exception as exc:
        error_msg = f"Error upserting deployment: {exc}"
        logger.error(error_msg)
        progress.fail_project(error_msg)
        return {
            "deployment_name": deployment_name,
            "status": "failed",
            "error": error_msg,
            "error_type": "internal_error",
        }
    finally:
        if project_manager:
            await project_manager.close()


async def handle_delete_project(payload: dict, progress: Any) -> dict:
    """Handle async project deletion task.

    The whole teardown: deployments, ArgoCD, namespace, databases, buckets and the project
    file. A task and not a request, so the dialog can follow it and the answer comes back
    through the same progress fragment as every other action.

    Expected payload keys:
        project_name: Name of the project to delete
    """
    from opi.manager.project_manager import create_project_manager

    project_name: str = payload["project_name"]

    logger.info(f"Task: deleting project {project_name}")

    delete_task = progress.add_task(steps.PROJECT_DELETE, subject=project_name)
    project_manager = create_project_manager()
    # Attach the progress manager so the delete names its own steps (infrastructure,
    # Keycloak, ArgoCD, namespaces, remeasurement) instead of one long-running task.
    project_manager.set_progress_manager(progress)
    try:
        deletion_results = await project_manager.delete_project(project_name)
    except Exception as exc:
        error_msg = f"Failed to delete project: {exc}"
        progress.fail_task(delete_task, error_msg)
        progress.fail_project(error_msg)
        raise
    finally:
        await project_manager.close()

    if not deletion_results.get("success"):
        # Deployments on another cluster are the one refusal that is not an error: this
        # instance only manages its own cluster, so it cannot finish the job here.
        remaining = deletion_results.get("remaining_deployments") or []
        if remaining:
            clusters = sorted({str(dep.get("cluster")) for dep in remaining if isinstance(dep, dict)})
            error_msg = (
                f"Project '{project_name}' kan niet verwijderd worden: er zijn nog deployments "
                f"op andere clusters ({', '.join(clusters)})"
            )
        else:
            errors = deletion_results.get("errors", []) or []
            error_msg = f"Project '{project_name}' niet volledig verwijderd: " + (
                "; ".join(str(e) for e in errors) or "onbekende fout"
            )
        progress.fail_task(delete_task, error_msg)
        progress.fail_project(error_msg)
        raise RuntimeError(error_msg)

    progress.complete_task(delete_task)
    logger.info(f"Task: project deletion completed successfully for {project_name}")
    return {
        "status": "completed",
        "message": f"Project '{project_name}' deleted successfully",
        "project": project_name,
        "deletion_results": deletion_results,
        "warning": "This deletion is permanent and cannot be undone",
    }
