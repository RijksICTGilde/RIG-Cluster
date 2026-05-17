# Auto-Wire imagePullSecret for Platform-Registry Images

**Status**: Proposed
**Trigger incident**: 2026-05-08 — `dp-bn7/productie` rollout to `desa-portfolio-11` stuck in `ImagePullBackOff` for ~65 minutes.

## Context

Customers can push private images to the platform mirror at `rcr.rijksapps.nl/rig/...` via the existing image-upload-proxy endpoint (`POST /api/v1/projects/{name}/images/push`). They then reference those images from their project file (typically as a deployment-level component override).

Pulling such images requires the `rig-robot-pull-secret` (a pre-existing K8s secret rolled out by the rig-system bootstrap to every user namespace). For the pull to succeed, that secret must end up in `Deployment.spec.template.spec.imagePullSecrets` of the rendered manifest.

## Failure Mode (dp-bn7)

1. dp-bn7 declared `image: rcr.rijksapps.nl/rig/zad:desa-portfolio-11` directly on the deployment-level component override.
2. The project file had **no** `registries:` section and **no** `registry:` ref on the component.
3. Two `imagePullSecrets` mechanisms exist; neither emitted `rig-robot-pull-secret`:
   - **Template** (`operations-manager/python/manifests/deployment.yaml.jinja:36-38`) emits one secret based on the lookup `imagePullSecretsMap[imageURL]`. The map (`opi/manager/project_manager.py:4143-4177`) is built from components that declare `registry:` referencing an entry in the project's top-level `registries:` list. dp-bn7 declared neither → no entry → no secret.
   - **`RegistryRewriteExtension`** (`opi/extensions/registry_rewrite.py`) rewrites image URLs and attaches the secret declared on the matched mapping. Mappings live in `operations-manager/python/extensions/odcn-registry-rewrite.yaml`. **There is no `from:` prefix for `rcr.rijksapps.nl/rig`**, and the rewrite extension only attaches a secret when the rewrite actually changes the URL (`new_image != image`, line 33). So images already on the destination registry don't get a secret from this path.
4. Net result: the new pod ended up with only `quay-rig-robot-pull-secret` (added by registry-rewrite for the oauth2-proxy sidecar at `quay.io/oauth2-proxy/...` → `rcr.rijksapps.nl/quay-rig/...`). Auth fails for `/rig/...`, kubelet reports `invalid username/password: authentication required`.

The issue stayed dormant for `desa-portfolio-10` because that pod's ReplicaSet was created when the older deployment template emitted *all* configured registry secrets unconditionally. The new ReplicaSet got the post-simplification template output and broke.

**Why no other project hit it**: every other project's images come from external registries (`ghcr.io/...`, etc.) that get rewritten by `RegistryRewriteExtension`, which adds the matching secret as a side effect. dp-bn7 is currently the only project pushing to `rcr.rijksapps.nl/rig/...` and referencing it directly.

## Workaround Applied

Added to `dp-bn7.yaml`:
```yaml
registries:
  - name: rig-platform
    url: rcr.rijksapps.nl/rig
    secretName: rig-robot-pull-secret
```
And `registry: rig-platform` on the deployment-level component override. Confirmed working: new ReplicaSet rolled out with both `rig-robot-pull-secret` and `quay-rig-robot-pull-secret`, pod went `2/2 Running`.

## Proposed Fix (Two Parts)

The fixes are independent and complementary — implement either, both is best.

### Part A: Auto-Wire on Image Push and Deployment Image Update

Make the platform handle the registry plumbing automatically when the customer interacts with the platform's own registry through the API. The customer should never need to hand-author `registries:` blocks for the platform mirror.

**On `POST /api/v1/projects/{name}/images/push`** (`opi/api/image_router.py:31`):
- After a successful skopeo push to `{REGISTRY_URL}/{REGISTRY_ORG}/{name}/...`, idempotently ensure a project-level registry entry exists pointing at `{REGISTRY_URL}/{REGISTRY_ORG}` with `secretName: rig-robot-pull-secret` (or, more correctly, the secret rolled out per-namespace by rig-system bootstrap — derive the name from cluster config).
- Use the existing `ProjectManager.upsert_registry_by_secret` (`opi/manager/project_manager.py`, called from `opi/api/router.py:3121`). It's already idempotent (upsert) and already triggers `process_project_from_git` to reconcile.
- A canonical name like `rig-platform` keeps multiple pushes deduplicated.

**On `PUT /api/projects/{name}/deployments/{deployment}/image`** (`opi/api/router.py:1649`):
- The endpoint already accepts an optional `registry:` field. Make it auto-fill that field when `newImageUrl` starts with the platform mirror prefix and the project has a `rig-platform` (or equivalent) registry declared.
- This closes the loop so customers can issue a single `PUT .../image` after pushing without thinking about pull secrets at all.

**Tests**:
- Push an image with no prior `registries:` block in project file → registry entry appears, deployment regenerates with `imagePullSecrets`.
- Repeated push doesn't duplicate the registry entry.
- Image update to `rcr.rijksapps.nl/rig/...` without explicit `registry:` field auto-wires the ref.

### Part B: Secret-Only Rules in RegistryRewriteExtension

Cover the case where a project file directly references a platform-mirror image without going through the push endpoint (e.g., copy-pasted image URLs, manual edits, future projects that import images via a different path).

Today `RegistryRewriteExtension._rewrite_image` only attaches a secret when the rewrite actually changes the URL (`registry_rewrite.py:33`). Extend the YAML schema and the extension to support rules of the form:

```yaml
# operations-manager/python/extensions/odcn-registry-rewrite.yaml
mappings:
  - prefix: rcr.rijksapps.nl/rig
    imagePullSecret: rig-robot-pull-secret
  - from: ghcr.io
    to: rcr.rijksapps.nl/ghcr-rig
    imagePullSecret: ghcr-rig-robot-pull-secret
  ...
```

Where `prefix:` (no `from:`/`to:`) means: don't rewrite, just add the secret if the image starts with this prefix. Implement in `_rewrite_image` (or a sibling method walked alongside) so the secret is appended to `secrets_to_add` even when the URL is unchanged.

This is the cleanest long-term fix because it keeps "registry → secret" knowledge in one place (the rewrite config) and stops requiring per-project declarations for the platform's own mirror.

**Tests**:
- Image already at `rcr.rijksapps.nl/rig/...` → `rig-robot-pull-secret` appears in pod's `imagePullSecrets`, image URL unchanged.
- Image at `ghcr.io/...` → URL rewritten and `ghcr-rig-robot-pull-secret` added (existing behavior preserved).
- Image at unrelated URL → no rewrite, no secret added.

## Adjacent Bug Found While Investigating

`oom_watcher` / `resource_tuning_service` auto-disable on `ImagePullBackOff` looks up the wrong component identifier. In the dp-bn7 incident:

```
WARNING - Component 'productie-website' not found in deployment 'productie' for disabled state update
```

The component reference is `website` (per `components[].name` and `deployments[].components[].reference`). The auto-disable lookup uses the rendered Deployment resource name (`productie-website`). The disable becomes a silent no-op, but the surrounding flow still commits an "auto-disable: image pull errors..." change attempt and triggers a `refresh_deployment` task, churning the project for nothing.

Fix: in the auto-disable lookup, derive the component reference name from the deployment-resource name (or pass it through the call site) before attempting the project-file mutation. Should be a small, contained change in `opi/services/oom_watcher.py` (or `resource_tuning_service.py` — confirm which owns the call site).

## Files Likely To Touch

- `operations-manager/python/opi/api/image_router.py` — auto-wire registry on push
- `operations-manager/python/opi/api/router.py` — auto-fill `registry:` on deployment image update
- `operations-manager/python/opi/manager/project_manager.py` — small helpers if needed (existing `upsert_registry_by_secret` already covers the heavy lifting)
- `operations-manager/python/opi/extensions/registry_rewrite.py` — secret-only rule support
- `operations-manager/python/extensions/odcn-registry-rewrite.yaml` — `rcr.rijksapps.nl/rig` entry
- `operations-manager/python/opi/services/oom_watcher.py` — component-name lookup fix
- Tests for all of the above.

## Out of Scope

- Reworking the deployment template to emit *all* configured registry secrets again. Today's narrower behavior is correct in principle (one secret per image). The fix is in the inputs (rewrite config + auto-wired project-file entries), not in widening the output.
- Cluster-level `imagePullSecrets` injection via mutating webhook. Heavier than warranted given there's only one platform mirror in play.
