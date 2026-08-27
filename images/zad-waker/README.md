# zad-waker

The tiny pod that stands in for a **sleeping** ZAD deployment (sleep-mode). While a
deployment is asleep its application runs at `replicas: 0`, so a separate, always-on pod
must catch incoming requests, show an "application is starting" page, and ask ZAD to
wake the deployment. This is that pod.

It shares the application component's Service and Ingress: its Deployment carries the
same `app: <unique_name>` label and `component: application` (the Service selector),
plus `zad-role: waker` so the two Deployments are distinguishable. While the app has
zero pods the waker is the only endpoint; once the app is back, the waker's readiness
probe flips to `503` and Kubernetes removes it from the EndpointSlice — the app takes
over with no gap.

## Base image

`gcr.io/distroless/static-debian12:nonroot`. Static `CGO_ENABLED=0` binary, runs as
UID 1001, no writable paths — compatible with OpenShift's `restricted-v2` SCC and a
read-only root filesystem. `GOMAXPROCS`/`GOMEMLIMIT` are set so the Go runtime sizes to
the container (100m CPU / 64Mi), not the node.

## Environment variables

Supplied by the waker ConfigMap (`<unique_name>-waker-config`) and Secret
(`<unique_name>-waker-token`).

| Variable | Description |
| --- | --- |
| `ZAD_API_URL` | In-cluster base URL of the OPI API (e.g. `http://operations-manager.rig-system.svc.cluster.local:8000`) |
| `ZAD_PROJECT` | Project name |
| `ZAD_DEPLOYMENT` | Deployment name |
| `ZAD_APP_TITLE` | Title shown on the page (defaults to the deployment name) |
| `ZAD_APP_DESCRIPTION` | Optional description shown on the page |
| `ZAD_WAKE_MODE` | `auto` \| `confirm` \| `manual` |
| `ZAD_POLL_INTERVAL_SEC` | Seconds between status polls to ZAD (default 3) |
| `ZAD_WAKE_TOKEN` | Per-deployment wake token, sent as `X-Wake-Token` (from the Secret) |
| `ZAD_PORT` | Port to listen on (default 8080). Must equal the port the application's Service targets: the waker has no Service of its own, it joins the application's by carrying the same `app` label. |

## Routes

| Route | Behaviour |
| --- | --- |
| `GET /__zad/healthz` | Always `200` (liveness/startup) |
| `GET /__zad/ready` | `200` while the app is not back, `503` once it is (so the waker leaves the EndpointSlice) |
| `GET /__zad/status` | JSON `{state, title, description, mode, elapsed}` |
| `POST /__zad/wake` | Start the wake (idempotent, single-flight). `403` in `manual` mode |
| `GET /robots.txt` | `User-agent: * / Disallow: /` |
| everything else | The `200` "starting" page; in `auto` a browser GET also triggers the wake |

## Wake modes

- **auto** — the first browser GET triggers the wake; the page shows a spinner and reloads when ready.
- **confirm** — the page shows a button; nothing happens until the visitor clicks it. Crawlers and link previews cannot wake the app.
- **manual** — the page only informs the visitor that an admin must start the app; `POST /__zad/wake` is `403`. The waker still polls and steps out of traffic when an admin wakes the app via the UI/API.

Single-flight: a hundred simultaneous visitors cause exactly one wake call to ZAD, so one commit. On failure the wake is retried up to three times with backoff, then the page shows an error.

## Design

The page reuses the visual language of the authorization sign-in card
(`operations-manager/python/manifests/sidecar-authorization-wall.yaml.jinja`) so the
two pages read as one system.

## Registry

Published to `ghcr.io/minbzk/base-images/zad-waker`. OPI's ghcr→RCR rewrite handles the
pull on ODCN.

## Building and publishing

```bash
# Local build + smoke test
docker build -t zad-waker:test images/zad-waker/
docker run --rm -e ZAD_WAKE_MODE=confirm -e ZAD_APP_TITLE="My app" -p 8080:8080 zad-waker:test

# Build and push (existing repo task)
task docker-build-and-push \
  BUILD_CONTEXT=images/zad-waker \
  IMAGE_NAME=zad-waker \
  REGISTRY_IMAGE=ghcr.io/minbzk/base-images/zad-waker
```
