# Manifest Inventory Cleanup

## Problem

When deployment configuration changes (e.g. `publish-on-web` removed from a component, domain/path changed, component removed from a deployment), OPI generates new manifests but never cleans up stale files from previous runs. This leaves orphaned manifests (ingresses, services, network policies, secrets) in the git repo, causing ArgoCD sync failures.

Current cleanup only handles:
- Root ingress when `root-component` changes
- Bare domain ingress when `expose-component-on-bare-domain` changes
- Server-side resources (DB, MinIO, Redis) via `handle_service_removal()`

Missing cleanup for:
- Component ingresses when `publish-on-web` is removed
- Ingress files when domain/subdomain/path changes (filename changes)
- All component manifests when a component is removed from a deployment
- Issuer and ACME network policy when issuer config changes
- PVC manifests when persistent-storage is removed
- Secret manifests when services are removed

## Solution: Manifest Inventory

Store a `.zad-manifest-inventory.json` in each deployment directory that lists all files ZAD created. On the next run, compare old inventory to new `created_files` and delete the difference.

### Inventory File

Location: `{deployment_path}/.zad-manifest-inventory.json`

```json
{
  "version": 1,
  "files": [
    "documentatie-deployment.yaml",
    "documentatie-service.yaml",
    "documentatie-ingress.yaml",
    "documentatie-allow-all-network-policy.yaml",
    "documentatie-platform-secret.yaml",
    "typesense-deployment.yaml",
    "typesense-service.yaml",
    "typesense-allow-all-network-policy.yaml",
    "typesense-platform-secret.yaml",
    "typesense-user-secret.sops.yaml",
    "typesense-data-pvc.yaml",
    "acme-http-prodregs-network-policy.yaml",
    "issuer-letsencrypt-rijks-app-prodregs.yaml"
  ]
}
```

### Why Not Delete All Unknown Files

Files may be added outside of ZAD (manual additions, other tooling). The inventory only tracks what ZAD created, so only ZAD-created files that are no longer needed get deleted.

### Why Not Predict Filenames

The manifest naming logic is complex (ingress maps, path suffixes, per-deployment secrets, PVC generations, sidecars). Duplicating this in a prediction function creates drift risk. Using the actual `created_files` output from `create_application_manifests()` is authoritative.

## Implementation

### Location

`_process_deployment_manifests()` in `opi/manager/project_manager.py` (around line 2550).

### Flow

```
1. Read old inventory from {target_path}/.zad-manifest-inventory.json
2. create_application_manifests() -> capture created_files
3. Normalize created_files: .to-sops.yaml -> .sops.yaml (final encrypted form)
4. Add always-generated files: kustomization.yaml, decrypt-sops.yaml
5. Compute stale_files = old_inventory - new_created_files
6. Delete stale files from disk, log each removal
7. Write new inventory (new_created_files)
8. Continue with: extensions, kustomization generation, SOPS encryption
```

### Edge Cases

- **First run** (no inventory file): no cleanup, just write the inventory after generation
- **Files added outside ZAD**: not in inventory, never touched
- **Component renamed**: old-name files are in inventory, new-name files are in `created_files`, diff correctly removes old
- **Deployment deleted entirely**: handled by existing `delete_project_manager`, not this feature
- **Inventory file in git**: committed alongside manifests, not a `.yaml` so kustomize ignores it, ArgoCD ignores it (not in kustomization resources)

### Kustomize/ArgoCD Compatibility

- `.json` files are not picked up by kustomize (only processes `.yaml`/`.yml`)
- The file is not listed in `kustomization.yaml` resources
- ArgoCD only deploys what kustomize outputs

## Also Fixed in This Session

- **ArgoCD freshness check** (`argo_manager.py`): changed `<=` to `<` on lines 857 and 1008 so that equal `reconciledAt` timestamps are treated as fresh (prevents infinite polling)
- **Alias removal bug** (`router_detail_edit.py` line 258): `dict.update()` in `_apply_list_item_merge` doesn't delete keys absent from form submission — needs fix to remove keys that the form processor intentionally deleted (fields with `remove_when_none=True`)
