# Configurable Health & Readiness Probes

**Status**: Partially implemented (commit 8b91f13c, 2026-07-02)
**Priority**: Medium
**Created**: 2026-06-24

> **Let op**: wat er gebouwd is, is een afgeslankte versie van onderstaand
> ontwerp: een `probe:` blok met `scheme` (`none`/`tcp`/`http`/`https`),
> `liveness-path` en `readiness-path`, op componentniveau. Niet gebouwd zijn:
> de veldnaam `health`, de override per deployment-component, en de `port` per
> probe. Zie [health-check-service.md](health-check-service.md) voor het plan om
> het `probe:` blok te vervangen door een `health-check` service, mét poort.

## Overview

This feature lets an application declare its own liveness and readiness (and
optionally startup) health checks through the project file. Today ZAD emits a
fixed set of **TCP-socket** probes that only check whether the application port
accepts a connection. An app that needs an HTTP health endpoint (e.g.
`/healthz`, `/readyz`) cannot express that.

## Current Behaviour

Probes are **hardcoded** in the deployment template and are **not driven by any
project-file value**. The only input is `application_port`.

`operations-manager/python/manifests/deployment.yaml.jinja:149-166`:

```yaml
startupProbe:
  tcpSocket:
    port: {{ application_port }}
  initialDelaySeconds: 5
  periodSeconds: 5
  failureThreshold: 36   # 5 + (36 × 5) = 185s max startup
livenessProbe:
  tcpSocket:
    port: {{ application_port }}
  initialDelaySeconds: 5
  periodSeconds: 30
  failureThreshold: 3
readinessProbe:
  tcpSocket:
    port: {{ application_port }}
  initialDelaySeconds: 0
  periodSeconds: 5
  failureThreshold: 3
```

Consequence: a pod counts as "ready/healthy" the moment it accepts a TCP
connection on its port. There is no HTTP path, and an unhealthy-but-listening
app (e.g. one that is up but failing dependency checks) is still routed traffic.

The probe template context comes from `project_manager.py` (the `variables`
dict, ~`project_manager.py:4823`), which passes `application_port` but no
probe-related fields.

## What It Is

An optional `health` block per component that selects the probe type and, for
HTTP probes, the path. Default (block omitted) keeps **exactly today's
behaviour** — TCP socket on the application port — so the change is fully
backwards compatible.

Following the same pattern as
[configurable-deployment-resources.md](configurable-deployment-resources.md):
configure at component level, optionally override at deployment-component level.

## Configuration

### Component-level

```yaml
components:
  - name: api-server
    image: my-registry/api:latest
    health:
      type: http            # http | tcp   (default: tcp = current behaviour)
      liveness:
        path: /healthz
      readiness:
        path: /readyz
```

### Deployment-component override

```yaml
deployments:
  - name: production
    cluster: odcn-production
    components:
      - reference: api-server
        health:
          type: http
          readiness:
            path: /readyz
            port: 8081       # optional; defaults to application_port
```

### Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `health` | No | — | Health-check configuration block |
| `health.type` | No | `tcp` | `tcp` (socket on port) or `http` (HTTP GET) |
| `health.liveness.path` | No (req. for http) | — | HTTP path for the liveness probe |
| `health.readiness.path` | No (req. for http) | — | HTTP path for the readiness probe |
| `health.<probe>.port` | No | `application_port` | Port to probe |

**Deliberately out of scope (KISS):** probe timings
(`initialDelaySeconds`, `periodSeconds`, `failureThreshold`) stay on the proven
defaults. Exposing them invites mis-tuning (flapping liveness, premature kills)
for marginal benefit. Add them only if a concrete need arises.

`startupProbe` follows the liveness configuration automatically (same type/path)
so apps don't configure it separately.

## Behaviour

| `health` block | Liveness/Readiness emitted |
|----------------|----------------------------|
| omitted | `tcpSocket` on `application_port` (today's defaults) |
| `type: tcp` | `tcpSocket`, optionally on a custom `port` |
| `type: http` | `httpGet` with `path` (+ optional `port`) |

When `type: http` but a probe has no `path`, validation rejects the project
(an HTTP probe without a path is meaningless).

## Implementation Sketch

1. **Schema** — add a `health` `$def` and reference it from `component`
   (`project_v2.json:340-409`) and `deployment-component`
   (`project_v2.json:458-498`), mirroring how `resources` is referenced.
2. **Validation** — require a `path` for each configured probe when
   `type: http`; reject otherwise. Live next to the resource validation.
3. **Context** — resolve the effective `health` (deployment-component override
   over component) in `project_manager.py` and pass a `health_config` dict into
   the template `variables` (alongside `metrics_config`).
4. **Template** — in `deployment.yaml.jinja:149-166`, branch on
   `health_config`: emit `httpGet`/custom `tcpSocket` when present, else the
   current hardcoded TCP block verbatim.
5. **Docs/UI** — once stable, move this doc out of `futures/` and consider a
   wizard/detail-edit field (optional follow-up; not required for the core).

## Verification

- Project without `health` → generated Deployment is byte-for-byte unchanged
  (regression test on the rendered manifest).
- `type: http` with paths → `httpGet` probes with the given paths/ports.
- `type: http` without a path → schema/validation error.
- Deployment-component `health` overrides the component-level one.

## Related Features

- [configurable-deployment-resources.md](configurable-deployment-resources.md) — same component/override pattern this feature follows.
