# Operations Manager Deployment Guide

## Publishing to GitHub Container Registry

The operations-manager image is published to GitHub Container Registry (GHCR) at:
`ghcr.io/minbzk/base-images/operations-manager:latest`

### Build and Push to GHCR

**Important: Run this command from the repository root directory!**

```bash
docker buildx build --platform linux/amd64,linux/arm64 -f operations-manager/Dockerfile -t ghcr.io/minbzk/base-images/operations-manager:latest --push .
```

### Prerequisites for Publishing

1. **Docker Buildx**: Ensure Docker buildx is available for multi-platform builds
2. **Registry Authentication**: Login to GitHub Container Registry:
   ```bash
   echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin
   ```
3. **Repository Access**: Ensure you have push permissions to the `minbzk/base-images` repository

## Local Development

For local development and testing, use the `sandbox:update-operations-manager` task:

```bash
task sandbox:update-operations-manager
```

This task:
1. Builds the image and pushes it to the registry next to the kind cluster
2. Points the sandbox overlay at that commit's tag
3. Applies the overlay and waits for the rollout

## Image Usage Patterns

### Local Development
- **Image**: `localhost:5001/operations-manager:<commit>` (local build, see `docs/sandbox-kind-registry.md`)
- **Used in**: Sandbox kind cluster deployments
- **Configuration**: `bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/patches/deployment.yaml`

### Production (ODCN)
- **Image**: `ghcr.io/minbzk/base-images/operations-manager:latest`
- **Used in**: Production ODC-Noord deployments
- **Configuration**: `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml`

## Deployment Workflow

1. **Development**: Make changes to operations-manager code
2. **Local Testing**: Run `task sandbox:update-operations-manager` to test locally
3. **Publishing**: When ready for production, run the buildx command to publish to GHCR
4. **Production Deployment**: ArgoCD automatically picks up the latest image for production overlays
