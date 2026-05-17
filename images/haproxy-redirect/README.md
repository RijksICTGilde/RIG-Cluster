# haproxy-redirect

Minimal HAProxy image that issues an HTTP `302` redirect for every incoming request, with the target URL supplied via a single environment variable.

Intended for use as a redirect deployment when a project's URL has changed and the old hostname must point to a new one. One redirect per pod (one `TARGET_URL` per deployment).

## Base image

`haproxy:3.2.19-alpine` (HAProxy 3.2 LTS, latest patch). Runs as `USER 10001`, no root required, no writable paths needed — compatible with OpenShift's `restricted-v2` SCC and read-only root filesystems.

## Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `TARGET_URL` | yes | Absolute URL the request is redirected to. The original request path and query string are appended. Example: `https://new-host.example.com` |

## Port

The container listens on TCP `8080` (HTTP only). TLS termination is expected to happen upstream at the cluster Ingress.

## Behaviour

- Returns HTTP `302 Found` for every request
- `Location` header is `${TARGET_URL}` with the original request URI appended (path + query string preserved)
- No host-matching, no path rewriting, no health endpoint — a TCP probe on port 8080 is sufficient for liveness/readiness

Example: with `TARGET_URL=https://new-host.example.com`, a request to `http://<pod>:8080/foo/bar?x=1` returns `Location: https://new-host.example.com/foo/bar?x=1`.

## Registry

Published to `ghcr.io/minbzk/base-images/haproxy-redirect`.

## Building and publishing

```bash
# Local build
docker build -t haproxy-redirect:test images/haproxy-redirect/

# Local smoke test
docker run --rm -e TARGET_URL=https://example.com -p 8080:8080 haproxy-redirect:test
curl -I http://localhost:8080/foo?bar=1
# Expect: HTTP/1.1 302 Found
#         Location: https://example.com/foo?bar=1

# Publish (multi-arch, GHCR)
task publish-haproxy-redirect
```
