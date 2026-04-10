# Internal IDs for Components and Deployments

## Problem

Component and deployment names are currently used for:
1. **Display** - shown in the UI
2. **Identity** - referenced by other parts of the YAML (e.g., `deployments[*]/components[*]/reference`)
3. **Manifest naming** - Kubernetes resource names, PVC names, secret names (e.g., `frontend-deployment.yaml`, `frontend-data-pvc`)
4. **Kubernetes resource names** - the actual names of Deployments, Services, PVCs in the cluster

This coupling means renaming a component is destructive:
- PVCs cannot be renamed in Kubernetes - a rename creates a new (empty) PVC and orphans the old one with its data
- Manifest files using the old name remain on disk, causing duplicate resources
- ArgoCD sees both old and new resources
- All cross-references in the YAML must be updated atomically

The same problem applies to deployment names, which are used in namespace generation, ArgoCD application names, and secret naming.

## Proposed Solution

Introduce an **internal ID** for both components and deployments. This ID:
- Is auto-generated at creation time
- Is human-readable (e.g., `comp-frontend-a1b2`, `dep-production-x3y4`)
- Never changes after creation
- Is used for all naming: manifests, Kubernetes resources, cross-references
- Is NOT shown in the UI (or shown only in advanced/debug views)

The user-facing `name` field becomes a pure display name that can be freely renamed.

## YAML Schema Change

### Current
```yaml
components:
  - name: frontend        # used for identity + display + naming
    ...

deployments:
  - name: production      # used for identity + display + naming
    components:
      - reference: frontend   # references component by name
        image: ...
```

### Proposed
```yaml
components:
  - id: comp-frontend-a1b2    # immutable, used for naming + references
    name: Frontend App         # display name, freely editable
    ...

deployments:
  - id: dep-prod-x3y4         # immutable, used for naming + references
    name: Production           # display name, freely editable
    components:
      - reference: comp-frontend-a1b2   # references component by ID
        image: ...
```

## ID Format Options

| Format | Example | Pros | Cons |
|--------|---------|------|------|
| `{type}-{slug}-{short-hash}` | `comp-frontend-a1b2` | Human-readable, type-prefixed | Longer |
| `{slug}-{short-hash}` | `frontend-a1b2` | Shorter, still readable | No type info |
| `{short-uuid}` | `a1b2c3d4` | Short, guaranteed unique | Not human-readable |

Recommendation: `{slug}-{short-hash}` where slug is derived from the initial name at creation time. The slug in the ID does NOT change when the display name changes - it's just a hint for humans reading raw YAML or kubectl output.

## Impact Areas

### Manifest Generation (`project_manager.py`)
- `generate_unique_name(deployment_id, component_id)` instead of names
- `generate_manifest_name(component_id, type)` instead of names
- All generated filenames use IDs
- Kubernetes resource names use IDs

### Cross-References
- `deployments[*]/components[*]/reference` uses component ID
- `components[*]/uses-components` uses component IDs
- Domain/subdomain config references deployment ID

### UI / Editables
- Component name field becomes freely editable (no `readonly_on_edit`)
- Dropdown selectors show display names but store IDs
- `ComponentReferenceOptionsProvider` maps IDs to display names

### Schema Migration
- Existing projects need migration: auto-generate IDs from current names
- For existing projects, the ID can simply equal the current name (backward compatible)
- New projects get proper `{slug}-{hash}` IDs

### ArgoCD / Kubernetes
- ArgoCD application names use deployment IDs
- Namespace naming uses deployment IDs
- No impact on running resources when display names change

## Migration Strategy

1. **Phase 1**: Add optional `id` field to schema. If absent, fall back to `name` (full backward compatibility).
2. **Phase 2**: Auto-generate IDs for new components/deployments. Existing ones get `id = name`.
3. **Phase 3**: Update all internal code to prefer `id` over `name` for naming/references.
4. **Phase 4**: Make `name` purely a display field. Allow free renaming.

## Benefits

- Component and deployment names become freely editable
- No manifest cleanup needed on rename - IDs don't change
- No PVC data loss on rename
- No cross-reference update needed on rename
- Kubernetes resources maintain stable identities
- Simpler change detection - only display metadata changed, not infrastructure

## Related

- Current workaround: component name is `readonly_on_edit` to prevent rename issues
- See also: manifest cleanup gap analysis (no per-component cleanup exists today)
- The dry-run manifest prediction approach (needed if we ever allow structural changes) should use the existing generation code path, not a separate prediction function
