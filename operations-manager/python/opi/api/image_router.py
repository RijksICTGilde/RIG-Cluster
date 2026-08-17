"""
Image upload API router for pushing container images to a remote registry.

Customers upload Docker image tarballs (from `docker save`) via HTTP, and the
Operations Manager pushes them to the configured registry using skopeo.

Optionally updates a deployment's component image reference after a successful push.
"""

import logging
import os
import tempfile

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.api.params import ProjectNamePath
from opi.connectors.skopeo import SkopeoConnectionError, SkopeoConnector, SkopeoExecutionError, SkopeoValidationError
from opi.core.config import settings
from opi.manager.project_manager import ProjectManager
from starlette.requests import Request

logger = logging.getLogger(__name__)

image_router = APIRouter(prefix="/api/v1/projects", tags=["images"])

CHUNK_SIZE = 64 * 1024  # 64 KB


@image_router.post("/{project_name}/images/push")
@validate_api_token
async def push_image(
    project_name: ProjectNamePath,
    request: Request,
    file: UploadFile,
    image_name: str = Query(..., description="Name of the container image"),
    tag: str = Query(..., description="Image tag"),
    deployment: str | None = Query(None, description="Deployment to update with the pushed image"),
    component: str | None = Query(None, description="Component within the deployment to update"),
) -> JSONResponse:
    """
    Upload a Docker image tarball and push it to the configured container registry.

    The tarball should be created with `docker save`. It is streamed to disk in chunks
    to avoid holding the full image in memory, then pushed via skopeo.

    The image lands on a tag that carries the project as its owner
    (`{project_name}_{image_name}-{tag}`), so two projects pushing the same
    `image_name` and `tag` get two different images and neither can overwrite the
    other's. Use the returned `image` reference, not a hand-built one.

    Optionally, provide both `deployment` and `component` to update
    the deployment's image reference and trigger a redeployment after a successful push.
    """
    if (deployment is None) != (component is None):
        raise HTTPException(
            status_code=400,
            detail="Both deployment and component must be provided together",
        )

    max_bytes = settings.IMAGE_UPLOAD_MAX_SIZE_MB * 1024 * 1024
    upload_dir = settings.TEMP_DIR

    connector = SkopeoConnector()

    # Validate early before accepting the upload. The project name is part of the
    # target, so it is validated here too: the destination tag is owner-pinned.
    try:
        connector.validate_push_target(project_name, image_name, tag)
    except SkopeoValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not settings.REGISTRY_URL:
        raise HTTPException(status_code=501, detail="Container registry is not configured")

    # Stream the upload to a temp file
    fd, tarball_path = tempfile.mkstemp(suffix=".tar", dir=upload_dir)
    try:
        bytes_written = 0
        with os.fdopen(fd, "wb") as tmp:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds maximum size of {settings.IMAGE_UPLOAD_MAX_SIZE_MB} MB",
                    )
                tmp.write(chunk)

        if bytes_written == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        logger.info(
            f"Received image tarball for {project_name}/{image_name}:{tag} ({bytes_written / (1024 * 1024):.1f} MB)"
        )

        image_ref = await connector.push_image(tarball_path, project_name, image_name, tag)

        response_data: dict[str, object] = {
            "status": "success",
            "message": f"Successfully pushed to {image_ref}",
            "image": image_ref,
        }

        if deployment and component:
            project_manager = ProjectManager(project_file_relative_path=f"projects/{project_name}.yaml")
            try:
                await project_manager.update_image_and_regenerate(deployment, component, image_ref)
            finally:
                await project_manager.close()
            response_data["deployment_updated"] = True
            response_data["deployment"] = deployment
            response_data["component"] = component

        return JSONResponse(response_data)

    except HTTPException:
        raise
    except SkopeoConnectionError as e:
        logger.error(f"Skopeo not available: {e}")
        raise HTTPException(status_code=503, detail="Image push service is unavailable")
    except SkopeoValidationError as e:
        logger.error(f"Validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except SkopeoExecutionError as e:
        logger.error(f"Push failed for {project_name}/{image_name}:{tag}: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to push image: {e}")
    finally:
        # Always clean up the tarball
        if os.path.exists(tarball_path):
            os.unlink(tarball_path)
            logger.debug(f"Cleaned up tarball: {tarball_path}")
