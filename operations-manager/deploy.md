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

For local development and testing, use the `update-operations-manager` task:

```bash
task update-operations-manager
```

This task:
1. Generates SOPS-encrypted secrets from `.env.secrets`
2. Builds the image locally as `operations-manager:latest`
3. Loads the image into the local kind cluster
4. Deploys the updated image

## Image Usage Patterns

### Local Development
- **Image**: `operations-manager:latest` (local build)
- **Used in**: Local kind cluster deployments
- **Configuration**: `bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml`

### Production (ODCN)
- **Image**: `ghcr.io/minbzk/base-images/operations-manager:latest`
- **Used in**: Production ODC-Noord deployments
- **Configuration**: `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml`

## Deployment Workflow

1. **Development**: Make changes to operations-manager code
2. **Local Testing**: Run `task update-operations-manager` to test locally
3. **Publishing**: When ready for production, run the buildx command to publish to GHCR
4. **Production Deployment**: ArgoCD automatically picks up the latest image for production overlays
