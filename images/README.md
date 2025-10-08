We created the following images because there was no default solution:

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