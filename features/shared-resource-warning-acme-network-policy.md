# Shared Resource Warning: ACME HTTP Network Policy

## Status: Resolved

## What It Was

ArgoCD reported a `SharedResourceWarning` when the same Kubernetes resource was managed by multiple ArgoCD Applications. This occurred with the `acme-http-network-policy` NetworkPolicy and `issuer-letsencrypt-*` Issuer when a project had multiple deployments sharing the same namespace.

Example warning:
```
SharedResourceWarning
NetworkPolicy/acme-http-network-policy is part of applications rig-prd-operations/wies-pr-164 and wies-staging2
```

## Root Cause

The naming functions `generate_network_policy_name` and `generate_issuer_name` produced fixed names based only on purpose/domain, not the deployment. When multiple deployments in the same project shared a namespace and both used Let's Encrypt, they generated identical resources with the same name, causing ArgoCD to see two Applications claiming the same resource.

## Solution: Deployment-Scoped Resource Names

Resource names now include the deployment name to ensure uniqueness per deployment:

```
# Before (shared, caused warnings)
acme-http-network-policy
issuer-letsencrypt-rijksapp-nl

# After (scoped per deployment)
acme-http-staging2-network-policy
acme-http-pr-164-network-policy
issuer-letsencrypt-rijksapp-nl-staging2
issuer-letsencrypt-rijksapp-nl-pr-164
```

Each ArgoCD Application now owns its own resources. Deleting a deployment cleanly removes only its resources without affecting other deployments in the same namespace.

### Changed Files

| File | Change |
|------|--------|
| `opi/utils/naming.py` | Added optional `deployment_name` parameter to `generate_network_policy_name`, `generate_network_policy_manifest_name`, `generate_issuer_name`, `generate_issuer_secret_name`, `generate_issuer_manifest_name` |
| `opi/manager/project_manager.py` | Pass `deployment_name` at all three code paths (helm-charts, helmfile/kustomize, components) for both resource generation and issuer references |

### Impact

- **No functional change** to TLS certificate issuance or ACME challenge handling
- **Eliminates SharedResourceWarning** in ArgoCD for shared-namespace projects
- **Cleaner deletion**: removing a deployment no longer risks removing resources needed by sibling deployments
- **Backward compatibility**: existing deployments will get new resource names on next sync; old resources will be pruned by ArgoCD
