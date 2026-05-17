# HAProxy Redirect Image

## Overview

`haproxy-redirect` is a minimal custom image that serves a single HTTP `302 Found` redirect from every incoming request to a configurable target URL. It is intended for projects whose hostname has changed and whose old hostname must continue to point users to the new location.

One redirect per pod: one `TARGET_URL` per deployment. For two old hostnames, deploy two stacks.

## When to use it

- A project URL has changed (e.g. moved to a new subdomain, renamed, or migrated between clusters) and the old hostname must still resolve.
- You want the redirect to live alongside the project's other Kubernetes resources, going through the same domain request/approval flow as any other ingress.

Not a fit for: many-to-many redirect tables, regex/path-rewriting redirects, redirect rules that change frequently. Those should use a shared edge solution (e.g. HAProxy at the cluster boundary).

## Image

- Source: [`images/haproxy-redirect/`](../images/haproxy-redirect/)
- Base: `haproxy:3.2.19-alpine` (HAProxy 3.2 LTS, pinned to a specific patch for reproducibility)
- Published to: `ghcr.io/minbzk/base-images/haproxy-redirect`
- Build / publish: `task publish-haproxy-redirect` (multi-arch `linux/amd64,linux/arm64`)

### Footprint

| Metric | Value |
| --- | --- |
| Image size | ~39 MB |
| Idle memory (cgroup) | ~14 MB |
| CPU | negligible at low traffic |
| Listening port | TCP `8080` (HTTP) |
| Runs as | `USER 10001` (OpenShift `restricted-v2` compatible, no writable paths) |

Memory is bounded by `maxconn 256` + `nbthread 1` in the baked-in config. More than sufficient for redirect workloads (which are stateless and complete in milliseconds).

## Configuration

A single environment variable:

| Variable | Required | Description |
| --- | --- | --- |
| `TARGET_URL` | yes | Absolute URL the request is redirected to. The original request path and query string are appended to it. Example: `https://new-host.example.com` |

The redirect status code is `302 Found` (temporary), baked into the image. Change requires a new image build.

## Behaviour

Given `TARGET_URL=https://new-host.example.com`, a request:

```
GET /foo/bar?x=1 HTTP/1.1
Host: old-host.example.com
```

produces:

```
HTTP/1.1 302 Found
Location: https://new-host.example.com/foo/bar?x=1
```

No host-matching is performed — every request that reaches the pod is redirected. The Ingress is responsible for routing only the intended old hostname(s) to this Service.

## Wiring it into a project

The image is consumed like any other container image. A redirect deployment needs:

- A **Deployment** running `ghcr.io/minbzk/base-images/haproxy-redirect:<tag>` with `TARGET_URL` set
- A **Service** exposing port `8080`
- An **Ingress** for the old hostname pointing at that Service (with the cluster's normal TLS termination)

No ConfigMaps, no init containers, no secrets. TLS is terminated upstream at the Ingress — the pod itself only speaks HTTP on `:8080`.

## Dependencies

- Kubernetes Ingress with TLS (handled by the cluster's existing ingress + cert-manager flow)
- Pull access to `ghcr.io/minbzk/base-images` from the target cluster

## Out of scope (revisit later)

- **ZAD service-type integration** — exposing this as a first-class `redirect` service in the project YAML schema, so ZAD would generate the Deployment/Service/Ingress automatically. Deferred until usage justifies the schema/generator work.
- **Multi-redirect-per-pod** — one HAProxy pod handling several `Host: → target` mappings. Requires ACLs on Host headers and either multiple env-var pairs or a map file. Not needed for current 1–2 redirect cases.
- **Configurable redirect code** — currently `302` is baked in. If permanent (`301`) is needed for a project, either build a variant image or revisit to make the code env-driven.
