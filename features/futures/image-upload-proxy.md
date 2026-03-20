# Image Upload Proxy - PoC (Level 1)

**Status**: Implemented

## Context

A customer needs to push private Docker images to Red Hat Quay (`rcr.rijksapps.nl`) which is behind a VPN. The Operations Manager pod in ODCN production **can** reach the registry (verified: auth token flow works). The customer cannot access Quay directly.

**Flow**: Customer does `docker save` locally, uploads tarball to OM via HTTP, OM uses `skopeo` to push to Quay, tarball is deleted.

## Prerequisites

- Skopeo is a **daemonless** CLI tool - no Docker daemon needed inside the pod. This is critical because running Docker-in-Docker in a Kubernetes pod would require privileged containers (security risk, likely blocked by ODCN).
- `skopeo copy docker-archive:/tmp/image.tar docker://registry/org/name:tag` pushes a tarball to a registry via the HTTP API.
- `python-multipart` is already a dependency in pyproject.toml (needed for FastAPI file uploads).

## Implementation Steps

### 1. Dockerfile - Install skopeo
**File**: `operations-manager/Dockerfile` (after Chisel install, ~line 62)

```dockerfile
# Install Skopeo - for pushing container images to remote registries
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends skopeo
```

Skopeo is in Debian repos (python:3.13-slim base). Simpler than downloading a static binary.

### 2. Config settings
**File**: `operations-manager/python/opi/core/config.py` - Add to `Settings` class:

- `REGISTRY_URL: str = ""` - Target registry (e.g., `rcr.rijksapps.nl`)
- `REGISTRY_ORG: str = ""` - Organization prefix (e.g., `rig`)
- `REGISTRY_USERNAME: str = ""` - Robot account name
- `REGISTRY_PASSWORD: str = ""` - Robot token (supports `age:`/`base64+age:`/`plain:` prefixes)
- `REGISTRY_VERIFY_TLS: bool = True`
- `IMAGE_UPLOAD_MAX_SIZE_MB: int = 5120` - Safety cap (5 GB)

### 3. Skopeo connector
**New file**: `operations-manager/python/opi/connectors/skopeo.py`

Following `minio_mc.py` singleton pattern:
- Sync `skopeo --version` check at init
- Async subprocess for `skopeo copy docker-archive:/path docker://destination`
- Credential masking in logs
- Password decrypted once at init via `decrypt_password_smart_auto_sync()` (same as git connector)
- `--dest-creds` for auth, `--dest-tls-verify=false` when configured
- Custom exceptions: `SkopeoConnectionError`, `SkopeoExecutionError`, `SkopeoValidationError`

### 4. Image upload API router
**New file**: `operations-manager/python/opi/api/image_router.py`

Single endpoint: `POST /api/v1/projects/{project_name}/images/push?image_name=X&tag=Y`
- `@validate_api_token` decorator (existing, project API key auth)
- `UploadFile` parameter for the tarball (`python-multipart` already a dependency)
- **Streaming write**: Read in 64KB chunks via `await file.read(CHUNK_SIZE)`, write directly to temp file via `tempfile.mkstemp(dir=upload_dir)` - never holds full image in memory
- Size enforcement during streaming (abort early if exceeds max)
- Destination: `{REGISTRY_URL}/{REGISTRY_ORG}/{project_name}/{image_name}:{tag}`
- Cleanup in `finally` block - tarball always deleted
- Response: `{"status": "success", "message": "...", "image": "rcr.rijksapps.nl/rig/project/name:tag"}`

### 5. Register router
**File**: `operations-manager/python/opi/server.py`

Import and include `image_router`.

### 6. ODCN production overlay - config & secrets
**File**: `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml` - Add:
```
REGISTRY_URL=rcr.rijksapps.nl
REGISTRY_ORG=rig
REGISTRY_USERNAME=rig+zad
IMAGE_UPLOAD_MAX_SIZE_MB=5120
```

**File**: `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/operations-manager-env-secrets.yaml` - Add `REGISTRY_PASSWORD` via `sops edit`.

### 7. ODCN production overlay - increase temp storage
**File**: `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml`

Current emptyDir on `/tmp` is 500Mi - too small for container images. Increase to **8Gi** via strategic merge patch on the `tmp` volume (uses node ephemeral storage, no PVC needed).

### 8. Ingress body size
The ODCN ingress uses HAProxy (OpenShift). HAProxy doesn't have a default body size limit, and the timeout is already 300s. No ingress changes needed for PoC. If uploads are slow over large files, may need `haproxy.router.openshift.io/timeout` increase.

## Customer Usage

```bash
# 1. Build and save image locally
docker save myapp:v1.2.3 -o myapp.tar

# 2. Upload to Operations Manager (one-liner)
curl -X POST \
  "https://zad.rijksapp.nl/api/v1/projects/my-project/images/push?image_name=myapp&tag=v1.2.3" \
  -H "X-API-Key: <project-api-key>" \
  -F "file=@myapp.tar"

# Response:
# {"status": "success", "message": "Successfully pushed to rcr.rijksapps.nl/rig/my-project/myapp:v1.2.3", "image": "rcr.rijksapps.nl/rig/my-project/myapp:v1.2.3"}
```

## Tests
- `tests/test_skopeo_connector.py` - Singleton, validation, command construction (mock subprocess)
- `tests/test_image_router.py` - Auth, streaming, size limit, cleanup on error (mock connector)

## Key Files to Reference
- `opi/connectors/minio_mc.py` - Connector pattern (singleton, async subprocess, sync init check)
- `opi/api/endpoint_util.py` - `@validate_api_token` decorator
- `opi/utils/age.py:385` - `decrypt_password_smart_auto_sync()`
- `opi/core/config.py` - Settings class

## Verification
1. Run tests: `cd operations-manager/python && uv run pytest tests/test_skopeo_connector.py tests/test_image_router.py -x -q --tb=short`
2. Run linting: `cd operations-manager/python && uv run ruff check . --fix && uv run ruff format . && uv run pyright`
3. Build Docker image locally to verify skopeo installs: `docker build -f operations-manager/Dockerfile .`
4. Deploy to sandbox/ODCN and test with a small image tarball via curl

## Future: Level 2 - Push and Deploy

After PoC validation, extend with a second endpoint that also updates the project's deployment to use the newly pushed image:

```bash
curl -X POST \
  "https://zad.rijksapp.nl/api/v1/projects/my-project/images/deploy?image_name=myapp&tag=v1.2.3&component=backend" \
  -H "X-API-Key: <project-api-key>" \
  -F "file=@myapp.tar"
```

This would push the image AND update the deployment/statefulset for the specified component with the new image reference.

## Verified Connectivity

Tested from Operations Manager pod (2025-02-25):
- `rcr.rijksapps.nl` is reachable from `rig-prd-operations` namespace
- Token-based auth flow works (Bearer realm at `https://rcr.rijksapps.nl/v2/auth`)
- Robot account `rig+zad` can obtain tokens successfully
