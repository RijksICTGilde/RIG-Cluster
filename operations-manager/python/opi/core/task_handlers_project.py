"""Task handlers for project creation and upsert deployment.

These handlers are called by TaskWorker. Each receives a payload dict and a
PersistentTaskProgressManager instance, and returns a result dict.

They have NO dependency on FastAPI Request objects.
"""

import logging
import time
from typing import Any

from opi.core.task_rollout import note_rollout_skipped, rollout_requested, skipped_processing

logger = logging.getLogger(__name__)


async def handle_create_project(payload: dict, progress: Any) -> dict:
    """Handle async project creation task.

    Extracted from simple_background.process_project_background() and
    process_project_yaml_background().  Supports two paths:

    1. Full generation -- payload contains SelfServiceProjectRequest fields
       (project_name, display_name, components, etc.) and the handler
       generates YAML via generate_self_service_project_yaml().
    2. Pre-built YAML -- payload contains ``yaml_content`` directly (produced
       by the wizard with editables and generators).  YAML generation is
       skipped.
    """
    from opi.core.simple_background import _monitor_argocd_and_deployment
    from opi.manager.project_manager import ProjectManager
    from opi.services.project_store import ConflictError, get_project_store
    from opi.utils.project_utils import generate_self_service_project_yaml, validate_project_name

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

    # ------------------------------------------------------------------
    # Step 1: Validation
    # ------------------------------------------------------------------
    validate_task = progress.add_task("Project validatie")
    progress.update_current_step("Validating project name")

    if not validate_project_name(project_name):
        error_msg = f"Invalid project name format: {project_name}"
        progress.fail_task(validate_task, error_msg)
        progress.fail_project(error_msg)
        return {"project_name": project_name, "status": "failed", "error": error_msg}

    progress.complete_task(validate_task)

    # ------------------------------------------------------------------
    # Step 2: YAML content
    # ------------------------------------------------------------------
    if pre_built_yaml is not None:
        # Path 2: wizard-provided YAML -- just verify we have content.
        yaml_task = progress.add_task("YAML configuratie controleren")
        logger.debug(
            "Using pre-built YAML content for %s (%d chars)",
            project_name,
            len(pre_built_yaml),
        )
        yaml_content = pre_built_yaml
        progress.complete_task(yaml_task)
    else:
        # Path 1: generate YAML from the request payload.
        yaml_task = progress.add_task("YAML configuratie genereren")
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
            return {"project_name": project_name, "status": "failed", "error": error_msg}

    # ------------------------------------------------------------------
    # Step 3: Git operations
    # ------------------------------------------------------------------
    git_task = progress.add_task("Git repository operaties")
    progress.update_current_step("Pushing project file to Git")

    try:
        from opi.utils.yaml_util import load_yaml_from_string

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
            return {"project_name": project_name, "status": "failed", "error": error_msg}

        project_data_dict = load_yaml_from_string(yaml_content)
        if not project_data_dict:
            error_msg = f"Kon de projectconfiguratie voor '{project_name}' niet inlezen"
            progress.fail_task(git_task, error_msg)
            progress.fail_project(error_msg)
            return {"project_name": project_name, "status": "failed", "error": error_msg}

        # Persist through the single validated save path: schema + structural
        # integrity validation, canonical dumper, commit + push, and cache
        # refresh in one shot. Replaces the previous unvalidated direct commit.
        # No connector injected on purpose. Injecting one makes ProjectManager skip
        # the store's warm copy (see get_git_connector_for_project_files), so the
        # processing step below would read a clone taken before this very write.
        # What arrives here is a COMPLETE project file, built from what the user saw
        # when the form was rendered. Publishing it as-is overwrites anything that
        # landed in between -- another portal user, another cluster, a direct push --
        # without anyone noticing. ``base_version`` names the version the form started
        # from, so the store can treat this as a change relative to that version and
        # merge it with the newer state instead. A caller that does not send one keeps
        # the old last-writer-wins behaviour; it is logged so the gap stays visible.
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

        project_manager = ProjectManager(project_file_relative_path=project_file_path)
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
        return {"project_name": project_name, "status": "failed", "error": error_msg}
    except Exception as exc:
        error_msg = f"Failed Git operations: {exc}"
        progress.fail_task(git_task, error_msg)
        progress.fail_project(error_msg)
        return {"project_name": project_name, "status": "failed", "error": error_msg}

    # ------------------------------------------------------------------
    # Step 4: Project deployment
    # ------------------------------------------------------------------
    # The project file is written and committed at this point. A caller that asked
    # not to roll out stops here: nothing is generated and nothing reaches the
    # cluster until the project is processed. That is what a project without
    # deployments needs -- process_project reports "no deployments for this
    # cluster" as a failure, which would mark a perfectly created project failed.
    if not rollout_requested(payload):
        await project_manager.close()
        note_rollout_skipped(progress)
        progress.update_current_step(f"Project {project_name} aangemaakt")
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

    deploy_task = progress.add_task("Project deployment")
    progress.update_current_step("Deploying project")

    # Known ArgoCD cache-invalidation bug: creating a new project invalidates
    # ArgoCD's cache, so its apps can take a few minutes to sync and the sync-wait
    # may run into its timeout. Warn the user up front that a timeout here does NOT
    # mean creation failed.
    if payload.get("is_new_project", False):
        notice = progress.add_subtask(
            deploy_task,
            "Let op: door een bekende bug in ArgoCD kan het aanmaken van een nieuw project een paar minuten duren, "
            "excuus daarvoor. Een eventuele time-out-melding betekent niet dat het aanmaken is mislukt, alleen dat de "
            "wachttijd is verstreken; het project wordt vrijwel zeker gewoon aangemaakt.",
        )
        progress.complete_task(notice)

    try:
        try:
            processing_result = await project_manager.process_project_from_git(
                project_file_path, progress, deployment_name=deployment_name, deployment_names=deployment_names
            )
            logger.info("Project processing completed, result: %s", processing_result)
        finally:
            await project_manager.close()

        if processing_result:
            # ArgoCD monitoring
            monitor_task = progress.add_subtask(deploy_task, "ArgoCD & deployment monitoring")
            await _monitor_argocd_and_deployment(
                _task_id="",  # not used by the monitor helper
                project_name=project_name,
                task_progress_manager=progress,
                monitor_task=monitor_task,
                # Scope the wait to what this task touched. Without it another
                # deployment's broken app blocks this one's wait indefinitely.
                deployment_names=deployment_names or ([deployment_name] if deployment_name else None),
            )

            progress.complete_task(deploy_task)

            # Schedule fire-and-forget OOM watcher for each deployment
            from opi.core.config import settings as app_settings
            from opi.services.oom_watcher import schedule_oom_check

            if app_settings.OOM_WATCHER_ENABLED and isinstance(project_data_dict, dict):
                for dep in project_data_dict.get("deployments", []):
                    dep_name = dep.get("name", "")
                    if dep_name:
                        schedule_oom_check(project_name, dep_name)

            progress.update_current_step(f"Project {project_name} succesvol geimplementeerd")
            progress.complete_project()

            elapsed_time = time.time() - start_time
            result: dict[str, Any] = {
                "project_name": project_name,
                "project_description": payload.get("project_description", "No description"),
                "components_count": len(payload.get("components", [])),
                "elapsed_time": f"{elapsed_time:.2f}",
                "file_path": project_file_path,
                "status": "success",
            }
            logger.info(
                "Project creation completed successfully: %s (took %.2fs)",
                project_name,
                elapsed_time,
            )
            return result
        else:
            error_msg = project_manager.get_processing_error() or "Project processing failed"
            progress.fail_task(deploy_task, error_msg)
            progress.fail_project(error_msg)
            component_failures = project_manager.get_component_failures()
            return {
                "project_name": project_name,
                "status": "failed",
                "error": error_msg,
                "processing": {
                    "status": "failed",
                    "error": error_msg,
                    **({"component_failures": component_failures} if component_failures else {}),
                },
            }

    except Exception as exc:
        error_msg = f"Failed deployment: {exc}"
        progress.fail_task(deploy_task, error_msg)
        progress.fail_project(error_msg)
        return {"project_name": project_name, "status": "failed", "error": error_msg}


async def handle_upsert_deployment(payload: dict, progress: Any) -> dict:
    """Handle async upsert deployment task.

    Extracted from router.upsert_deployment().
    """
    from opi.manager.project_manager import ProjectManager
    from opi.utils.naming import sanitize_kubernetes_name
    from opi.utils.project_utils import normalize_container_image, validate_project_name

    project_name: str = payload["project_name"]
    deployment_name: str = payload["deployment_name"]
    components: list[dict[str, str]] = payload["components"]
    clone_from: str | None = payload.get("clone_from") or payload.get("cloneFrom")
    force_clone: bool = payload.get("force_clone") or payload.get("forceClone", False)
    domain_format: str | None = payload.get("domain_format")
    subdomain: str | None = payload.get("subdomain")
    base_domain: str | None = payload.get("base_domain")

    project_manager: ProjectManager | None = None

    try:
        # ------------------------------------------------------------------
        # Step 1: Validation
        # ------------------------------------------------------------------
        validate_task = progress.add_task("Deployment validatie")
        progress.update_current_step("Validating project and deployment names")

        if not validate_project_name(project_name):
            error_msg = (
                "Invalid project name format. Must start with lowercase letter, "
                "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "deployment_name": deployment_name,
                "status": "failed",
                "error": error_msg,
            }

        sanitized_name = sanitize_kubernetes_name(deployment_name)
        if sanitized_name != deployment_name.lower():
            error_msg = (
                f"Invalid deployment name. Use lowercase letters, numbers, and "
                f"hyphens only. Suggested: {sanitized_name}"
            )
            progress.fail_task(validate_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "deployment_name": deployment_name,
                "status": "failed",
                "error": error_msg,
            }

        progress.complete_task(validate_task)

        # ------------------------------------------------------------------
        # Step 2: Upsert deployment in project YAML
        # ------------------------------------------------------------------
        upsert_task = progress.add_task("Deployment upsert")
        progress.update_current_step(f"Upserting deployment '{deployment_name}'")

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
        )

        if not result["success"]:
            error_msg = result.get("error", "Unknown upsert error")
            progress.fail_task(upsert_task, error_msg)
            progress.fail_project(error_msg)
            return {
                "deployment_name": deployment_name,
                "status": "failed",
                "error": error_msg,
                "error_type": result.get("error_type", "unknown"),
            }

        progress.complete_task(upsert_task)

        # ------------------------------------------------------------------
        # Step 3: Process deployment
        # ------------------------------------------------------------------
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
            deploy_task = progress.add_task("Deployment processing")
            progress.update_current_step(f"Processing deployment '{deployment_name}'")

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
                progress.complete_task(deploy_task)

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
                progress.fail_task(deploy_task, processing_error)
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

        # ------------------------------------------------------------------
        # Build response
        # ------------------------------------------------------------------
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
        }
    finally:
        if project_manager:
            await project_manager.close()


async def handle_delete_project(payload: dict, progress: Any) -> dict:
    """Handle async project deletion task.

    Extracted from the web delete endpoint, which ran the whole teardown -- deployments,
    ArgoCD, namespace, databases, buckets, the project file -- inside the request while
    the browser sat on an open POST. As a task the dialog can follow it, and the answer
    comes back through the same progress fragment as every other action.

    Expected payload keys:
        project_name: Name of the project to delete
    """
    from opi.manager.project_manager import create_project_manager

    project_name: str = payload["project_name"]

    logger.info(f"Task: deleting project {project_name}")

    delete_task = progress.add_task(f"Project '{project_name}' verwijderen")
    project_manager = create_project_manager()
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
