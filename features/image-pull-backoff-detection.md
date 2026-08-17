# ImagePullBackOff Detection and Auto-Disable

## What it is

Automatic detection of ImagePullBackOff errors on deployments, with auto-disable of the affected component to prevent continuous registry slamming. When a new image is pushed for the component, the disabled state is automatically reset.

This extends the existing sanitize endpoint (which already handles crash loops) with image pull error detection. Sanitize does not disable for OOM kills: those belong to the resource tuner, see `auto-resource-tuning.md`.

## How it works

```
Deployment running
       |
  Image pull fails (wrong tag, deleted image, auth error)
       |
  Pod enters ImagePullBackOff
       |
  Sanitize endpoint detects Warning events (ErrImagePull, ImagePullBackOff, InvalidImageName)
       |
  Component disabled in project YAML (disabled: true, disabled-reason: "ImagePullBackOff: ...")
       |
  Reprocessing generates replicas: 0 --> pod stops retrying
       |
  User pushes new image via API
       |
  The rollout event (ActionEvent.REDEPLOY) fires; deployment-health clears the disable
       |
  Component re-enabled (disabled: false) --> replicas: 1 --> new image pulled
```

### Why disable instead of letting Kubernetes retry?

Kubernetes retries image pulls with exponential backoff (up to 5 minutes between attempts), but it never stops. For images that will never be found (wrong tag, deleted from registry), this causes:

- Continuous load on the container registry
- Noise in monitoring and events
- Wasted node resources on scheduling attempts

Disabling the component (replicas: 0) stops the retry loop entirely until the user takes action.

### Why a broken registry is the exception

Disabling is only correct when the registry *answered* and the image is not there. A `500`,
`502`, `503`, `504` or a `429` rate limit says nothing about whether the tag exists, so
disabling on one is a guess -- and an expensive guess: `replicas: 0` removes the very pod
that would have retried, so the component can never recover on its own, not even once the
registry is healthy again. That turns a hiccup of a few seconds into an outage that lasts
until someone pushes a new tag.

So a pull error whose message names a registry-side failure leaves the component **enabled**
and is only reported. Kubelet keeps retrying the pull with its own backoff and the component
comes back by itself. `is_transient_registry_error()` in `opi/handlers/project_file_handler.py`
draws the line; it matches literal phrases (`internal server error`, `http status: 503`, ...)
and never a bare number, so an image tag like `pr-500-abc1234` is not read as a status code.

This came out of the incident of 2026-08-12, where the ODCN pull-through mirror
`rcr.rijksapps.nl/ghcr-rig` returned 500 on manifests that ghcr served fine. Two components
sharing one image tag would pull at the same moment, one would catch the 500, and that half
of the pair was disabled permanently while its twin ran happily on the identical tag.

## Detection

The sanitize endpoint (`POST /api/resources/{project_name}/sanitize`) checks Kubernetes namespace events for Warning events with these reasons:

- `ErrImagePull` -- initial pull failure
- `ImagePullBackOff` -- backoff after repeated failures
- `InvalidImageName` -- malformed image reference

Events are filtered by:
- **Component name**: only events for the specific component's pods (matched by `unique_name` prefix)
- **Age**: only events from the last hour (`max_age_hours=1`)

## Auto-reset on a rollout

A rollout -- an image push or an upsert of an existing deployment -- re-enables the
component before reprocessing, so the new image gets a chance to pull without manual
intervention.

Since RC-37 this is no longer a reason check in `update_image_and_regenerate()`, and it is
no longer limited to image-pull disables. The rollout paths fire `ActionEvent.REDEPLOY` and
the deployment-health service clears the disable **whatever the reason said** -- OOMKilled
and crash loops included -- because every automatic disable is a judgement about content
that was just replaced. See `features/redeploy-clears-recorded-state.md` for the reasoning
and for the one case that is left alone (a `disabled` flag on the component definition,
which is a project-wide decision by a person).

## Configuration

No additional configuration needed. The detection uses the existing sanitize infrastructure:

| Setting | Default | Description |
|---------|---------|-------------|
| `SANITIZE_RESTART_THRESHOLD` | `10` | Restart count threshold (existing, unrelated to image pull) |

## Key files

| File | Purpose |
|------|---------|
| `opi/api/resource_router.py` | Sanitize endpoint with ImagePullBackOff event check |
| `opi/handlers/project_file_handler.py` | `is_transient_registry_error()`: registry failure vs missing image |
| `opi/manager/project_manager.py` | Inline deploy path: splits the two and only disables the missing-image half |
| `opi/services/oom_watcher.py` | Delayed watcher: same split before `disable_components_for_image_pull()` |
| `opi/services/redeploy.py` | The rollout scan that lets the services clear their state |
| `opi/services/catalog/deployment_health/` | Clears the disable on a rollout |
| `opi/connectors/kubectl.py` | `get_namespace_events()` for event retrieval |

## Related features

- [oom-kill-watcher.md](oom-kill-watcher.md) -- Similar pattern for OOM detection and auto-tuning
- [auto-resource-tuning.md](auto-resource-tuning.md) -- Resource tuning via the same sanitize endpoint
- [redeploy-clears-recorded-state.md](redeploy-clears-recorded-state.md) -- The hook that re-enables the component
