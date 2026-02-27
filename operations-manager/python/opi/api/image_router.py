"""
Image upload API router for pushing container images to a remote registry.

Customers upload Docker image tarballs (from `docker save`) via HTTP, and the
Operations Manager pushes them to the configured registry using skopeo.
"""

import logging
import os
import tempfile

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from opi.api.endpoint_util import validate_api_token
from opi.connectors.skopeo import SkopeoConnectionError, SkopeoConnector, SkopeoExecutionError, SkopeoValidationError
from opi.core.config import settings
from starlette.requests import Request

logger = logging.getLogger(__name__)

image_router = APIRouter(prefix="/api/v1/projects", tags=["images"])

CHUNK_SIZE = 64 * 1024  # 64 KB


@image_router.post("/{project_name}/images/push")
@validate_api_token
async def push_image(
    project_name: str,
    request: Request,
    file: UploadFile,
    image_name: str = Query(..., description="Name of the container image"),
    tag: str = Query(..., description="Image tag"),
) -> JSONResponse:
    """
    Upload a Docker image tarball and push it to the configured container registry.

    The tarball should be created with `docker save`. It is streamed to disk in chunks
    to avoid holding the full image in memory, then pushed via skopeo.
    """
    max_bytes = settings.IMAGE_UPLOAD_MAX_SIZE_MB * 1024 * 1024
    upload_dir = settings.TEMP_DIR

    connector = SkopeoConnector()

    # Validate early before accepting the upload
    try:
        connector._validate_image_name(image_name)
        connector._validate_tag(tag)
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

        return JSONResponse(
            {
                "status": "success",
                "message": f"Successfully pushed to {image_ref}",
                "image": image_ref,
            }
        )

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
