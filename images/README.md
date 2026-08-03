We created the following images because there was no default solution:

e2e-allservices: minimal, fast-booting test workload that round-trips every platform service it is
bound to (PostgreSQL incl. extra schemas + RO role, Redis, MinIO/S3, Keycloak/OIDC, PVCs) and reports
over HTTP (/, /healthz, /status). Used as the sandbox E2E all-services fixture; what it tests is
scan-driven from OPI's service registry. See images/e2e-allservices/README.md and
features/e2e-allservices-image.md. Build+publish: `task publish-e2e-allservices`.

cmp-kustomize-sops: used as sidecar in ArgoCD, so we can apply kustomize with sops secrets, where the secret is
stored in the namespace, similar to how Flux would deploy.

To build and use locally:
docker build --no-cache --progress=plain -t rig-cmp-argo-kustomize-sops:latest .

TODO: push docker to external registry
docker push your-registry.com/rig-cmp-argo-kustomize-sops:latest

docker buildx build --platform linux/amd64,linux/arm64 -t ghcr.io/minbzk/base-images/rig-cmp-argo-kustomize-sops:latest --push .

LOCAL Kind, NOTE: use the correct clustername
kind load docker-image rig-cmp-argo-kustomize-sops:latest --name gitops-fluxcd

To run locally with shell access:
docker run --rm -it --entrypoint /bin/bash rig-cmp-argo-kustomize-sops:latest

task bootstrap-argo-system
kubectl rollout restart deployment argocd-repo-server -n rig-system
