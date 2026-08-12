# Multi-Path Ingress

## What it is

Allows a component to expose multiple URL paths, with each path generating its own Kubernetes Ingress resource. This is useful when a single service needs to be accessible via different URL paths, such as `/api` and `/v1/api`.

## How to use it

### Simple path (default behavior)

```yaml
components:
  - name: frontend
    path: "/"              # Single path as string
    publish-on-web: true
```

### Multiple paths

```yaml
components:
  - name: api
    publish-on-web: true
    path:
      - match: "/api"       # First path
      - match: "/v1/api"    # Second path
```

This generates two separate Ingress resources:
- `deployment-api` for path `/api`
- `deployment-api-v1api` for path `/v1/api`

## Configuration

| Field | Required | Description |
|-------|----------|-------------|
| `path` | No | URL path(s) for ingress routing. Defaults to `/` |

### Path formats

**String format** (single path):
```yaml
path: "/api"
```

**List format** (multiple paths):
```yaml
path:
  - match: "/api"
  - match: "/v1/api"
  - match: "/health"
```

## Examples

### API with versioned endpoints

```yaml
components:
  - name: backend
    publish-on-web: true
    path:
      - match: "/api"
      - match: "/api/v1"
      - match: "/api/v2"
```

### Service with separate health endpoint

```yaml
components:
  - name: app
    publish-on-web: true
    path:
      - match: "/"
      - match: "/health"
```

## Generated resources

For a component named `api` in deployment `main` with paths `/api` and `/v1`:

| Path | Ingress Name | Manifest Filename |
|------|--------------|-------------------|
| `/` | `main-api` | `api-ingress.yaml` |
| `/api` | `main-api-api` | `api-ingress-api.yaml` |
| `/v1` | `main-api-v1` | `api-ingress-v1.yaml` |
| `/v1/users` | `main-api-v1users` | `api-ingress-v1users.yaml` |

## Path rewrite

A path entry may carry a `rewrite` next to its `match`. The ingress then rewrites the
external path before the request reaches the container:

```yaml
path:
  - match: "/api"
    rewrite: "/"    # https://<host>/api/status arrives as /status
```

Without `rewrite` the path is passed on unchanged, which is what a component that serves
its own prefix needs. There is no default, so existing components keep their behaviour.
The field can also be set through the API - see `component-path-rewrite-api.md`.

## Dependencies

- Component must have `publish-on-web: true` for ingress generation
- All paths share the same hostname (determined by domain mode)
- Each path gets its own Ingress resource to avoid nginx annotation conflicts

## Troubleshooting

### Ingress not created
- Verify `publish-on-web: true` is set on the component
- Check that `path` syntax is correct (list of objects with `match` key)

### Path conflicts
- Each path must be unique within the component
- Different components can have overlapping paths if they use different domain modes
