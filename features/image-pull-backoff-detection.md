# ImagePullBackOff Detection and Auto-Disable

## What it is

Automatic detection of ImagePullBackOff errors on deployments, with auto-disable of the affected component to prevent continuous registry slamming. When a new image is pushed for the component, the disabled state is automatically reset.

This extends the existing sanitize endpoint (which already handles OOM kills and crash loops) with image pull error detection.

## How it works

```
Deployment running
       |
  Image pull fails (wrong tag, registry down, auth error)
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
  update_image_and_regenerate() detects disabled-reason contains "ImagePullBackOff"
       |
  Component re-enabled (disabled: false) --> replicas: 1 --> new image pulled
```

### Why disable instead of letting Kubernetes retry?

Kubernetes retries image pulls with exponential backoff (up to 5 minutes between attempts), but it never stops. For images that will never be found (wrong tag, deleted from registry), this causes:

- Continuous load on the container registry
- Noise in monitoring and events
- Wasted node resources on scheduling attempts

Disabling the component (replicas: 0) stops the retry loop entirely until the user takes action.

## Detection

The sanitize endpoint (`POST /api/resources/{project_name}/sanitize`) checks Kubernetes namespace events for Warning events with these reasons:

- `ErrImagePull` -- initial pull failure
- `ImagePullBackOff` -- backoff after repeated failures
- `InvalidImageName` -- malformed image reference

Events are filtered by:
- **Component name**: only events for the specific component's pods (matched by `unique_name` prefix)
- **Age**: only events from the last hour (`max_age_hours=1`)

## Auto-reset on image change

When `update_image_and_regenerate()` is called (via the image push API or deployment update), it checks:

1. Is the component currently disabled?
2. Does the `disabled-reason` contain "ImagePullBackOff"?

If both are true, the component is automatically re-enabled before reprocessing. This ensures the new image gets a chance to pull without manual intervention.

Components disabled for other reasons (e.g. OOMKilled, crash loops) are not affected by this reset.

## Configuration

No additional configuration needed. The detection uses the existing sanitize infrastructure:

| Setting | Default | Description |
|---------|---------|-------------|
| `SANITIZE_RESTART_THRESHOLD` | `10` | Restart count threshold (existing, unrelated to image pull) |

## Key files

| File | Purpose |
|------|---------|
| `opi/api/resource_router.py` | Sanitize endpoint with ImagePullBackOff event check |
| `opi/manager/project_manager.py` | Auto-reset in `update_image_and_regenerate()` |
| `opi/connectors/kubectl.py` | `get_namespace_events()` for event retrieval |

## Related features

- [oom-kill-watcher.md](oom-kill-watcher.md) -- Similar pattern for OOM detection and auto-tuning
- [auto-resource-tuning.md](auto-resource-tuning.md) -- Resource tuning via the same sanitize endpoint
