# Health Check Service

## Overview

The **Health check** service lets a component control how Kubernetes probes its
health: the probe scheme (`tcp`, `http`, `https` or `none`), the port that is
probed, and the liveness/readiness paths.

Unlike every other platform service, absence of this service is **not** neutral.
A component is always health-checked -- without this service it gets a plain
**TCP** probe on its first inbound port (the platform default). This service
exists to point that probe at an HTTP(S) endpoint, at a *separate* port, or to
switch probing off entirely.

The motivating case: FSC components serve their health endpoints on a separate
monitoring port (e.g. `8080`) without mTLS, while their functional port (`8443`)
enforces mTLS. A default TCP probe against `8443` opens and drops a bare
connection every few seconds, which an mTLS server logs as a handshake failure.
Pointing an HTTP probe at the monitoring port makes the log quiet and the check
meaningful.

This is a component-level, behaviour-only service: it provisions nothing and owns
no secret. It overrides the deployment template's probe variables via its
manifest contribution.

## How to Use

### Point an HTTP probe at a separate monitoring port

```yaml
components:
  - name: dirmgr
    ports:
      inbound:
        - 8443            # functional (mTLS) port; NOT the probe target
    services:
      - publish-on-web:
          config:
            tls: passthrough
      - health-check:
          config:
            scheme: http
            port: 8080     # the monitoring port (need not be an inbound port)
            liveness-path: /health/live
            readiness-path: /health/ready
```

### Only paths, probe the inbound port

Leave `port` out to probe the component's first inbound port:

```yaml
components:
  - name: dirui
    ports:
      inbound:
        - 8080
    services:
      - health-check:
          config:
            scheme: http
            liveness-path: /health/live
            readiness-path: /health/ready
```

### Disable all probes

Disabling probes is done by *adding* the service with `scheme: none`. This is
explicit and visible in the project file. Note: a component with no probes is not
restarted when it hangs, and a not-ready pod still receives traffic.

```yaml
services:
  - health-check:
      config:
        scheme: none
```

## Configuration Options

| Option | Required | Default | Description |
|---|---|---|---|
| `scheme` | No | `tcp` | `none`, `tcp`, `http` or `https` |
| `port` | No | first inbound port | Port to probe |
| `liveness-path` | No | `/` | Path for the liveness and startup probes; ignored when scheme is `tcp`/`none` |
| `readiness-path` | No | `/` | Path for the readiness probe; ignored when scheme is `tcp`/`none` |

Both paths must be absolute and built from URL-safe characters only
(`^/[A-Za-z0-9/_.\-]*$`); `port` must be in `1-65535`. These values are
interpolated into the generated pod spec, so the config is rejected on save if a
path carries YAML control characters or a port is out of range.

The config uses the `config:` wrapper (like `publish-on-web` and `attachments`).
`port` is an integer, not a named port: ZAD generates container-port names itself
(`http` for the first inbound port, `p<number>` for the rest), so a number is
unambiguous. The probe port does **not** need to appear in `ports.inbound` -- the
kubelet probes the pod IP directly, and adding the monitoring port to `inbound`
would create an extra Service port.

## Behaviour Notes

- **A component without an inbound port is never probed**, even with this service
  selected. The kubelet cannot reach it, so generic code forces `scheme: none`
  and the service contributes nothing.
- **Only the keys you set are overridden.** A half-filled config never renders a
  broken probe: unset `scheme` falls back to the platform default, unset `port`
  to the application port, unset paths to `/`.
- **Probe timings are fixed** (startup 5s/5s with 36 attempts, liveness 5s/30s,
  readiness 0s/5s). They are not configurable.
- **One port for all three probes.** Startup, liveness and readiness share the
  probe port and (for liveness/startup) the liveness path.
- **Component-level only.** A health endpoint is a property of the image, not the
  environment, so there is no per-deployment override.

## What Gets Rendered

With `scheme: http`, `port: 8080`, the deployment renders httpGet probes:

```yaml
startupProbe:
  httpGet:
    port: 8080
    path: /health/live
    scheme: HTTP
  initialDelaySeconds: 5
  periodSeconds: 5
  failureThreshold: 36
livenessProbe:
  httpGet:
    port: 8080
    path: /health/live
    scheme: HTTP
  ...
readinessProbe:
  httpGet:
    port: 8080
    path: /health/ready
    scheme: HTTP
  ...
```

An `httpGet` probe counts HTTP 200-399 as healthy; there is no response-body check.

## Implementation

- Service package: `operations-manager/python/opi/services/catalog/health_check/`
  (`__init__.py`, `config_model.py`, `editables.py`, `visualizers.py`,
  `health-check.v1.0.json`).
- Manifest override: `contribute_manifest_context` sets `probe_scheme`,
  `probe_port`, `probe_liveness_path` and `probe_readiness_path` in `template_vars`.
- Template: `manifests/deployment.yaml.jinja` -- the probe port lines use
  `probe_port | default(application_port, true)`; the base probe scheme lives in
  generic code (`project_manager.py`).

This service replaces the earlier component-level `probe:` block (never used by any
project). See `features/futures/configurable-health-probes.md` for the original
design and `instructions/services.md` for the service system.

A probe pointed at a port the component does not serve does not fail loudly: the
kubelet restarts the container, which reaches `CrashLoopBackOff` just like a real
crash. What the portal reports in that case -- and how it tells the two apart -- is
in `features/probe-kill-is-geen-crash.md`.

## Dependencies

None. Behaviour-only; no external system, no secret, no other service required.
