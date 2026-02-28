# Sidecar and Non-App Container Resource Tuning

## Overview

The current resource tuning endpoint (`/api/resources/{project_name}/tune`) only queries Prometheus for the `app` container. Sidecar containers (e.g. `authorization-wall` / oauth2-proxy) and any future sidecars are invisible to the tuning logic, meaning their resource usage is never analyzed or adjusted.

## Problem

- Prometheus queries filter on `container="app"`, missing sidecar memory/CPU usage entirely
- Sidecar resource limits are hardcoded in the sidecar Jinja templates (e.g. 64Mi for oauth2-proxy)
- Pod-level memory pressure may be caused by sidecars, but the tuning endpoint won't detect it
- OOM kill detection also only looks at the `app` container

## What Needs to Change

1. **Query all containers in the pod** — the tune endpoint should query Prometheus per container (not just `app`) and report usage for each
2. **Per-container recommendations** — resource recommendations should be generated per container, not just for the main app
3. **Sidecar resource overrides in project YAML** — allow projects to override default sidecar resource limits if needed (e.g. a heavy-traffic site may need more memory for oauth2-proxy)
4. **Template-driven defaults** — sidecar templates should define sensible defaults, but the project manager should be able to override them from project YAML values

## Considerations

- The sidecar resource limits live in Jinja templates, not in the project YAML — need a mechanism to pass overrides from project config to template variables
- Some sidecars (like oauth2-proxy) have predictable, low resource usage; others may vary significantly
- The sanitize endpoint has the same blind spot (only checks `app` container health)
