# e2e-allservices test workload image

A purpose-built, ultra-minimal test workload that, on startup, does a **real
write-and-read-back round-trip against every platform service it is bound to**
and reports the outcome over HTTP. It is the sandbox E2E all-services fixture and
doubles as a human smoke-test workload.

- **Source:** `images/e2e-allservices/` (Go binary + Dockerfile)
- **Spec generator:** `scripts/generate_probe_spec.py`
- **Image:** `ghcr.io/minbzk/base-images/e2e-allservices:latest`
- **Design doc / rationale:** `features/futures/minimal-e2e-test-image.md` (the
  approved plan this implements)

## What it is / why

The E2E suite used to deploy a trivial hello-world image as the test workload. It
only proved a pod could start and pass a TCP probe on `:8080` - it never touched
the database, Redis, MinIO or Keycloak the project bound, so a create-with-all-
services test could go green while a service was misprovisioned (wrong secret
keys, unreachable host, missing schema, broken OIDC URL).

This image closes that gap: when it boots green, the whole binding actually works
end to end with the exact credentials the platform injected. For every bound
resource it performs an actual authenticated operation (write *and* read back,
verifying the bytes) - never a "credentials are present" check - because the
point is to catch a mis-provisioned secret, an unreachable host, a missing grant,
a wrong bucket, or an unmounted volume.

## What it checks

Each bound service maps to one reusable *probe kind*:

| Kind | Services | Round-trip |
|---|---|---|
| `sql` | postgresql-database, namespace-postgresql-database | connect via `DATABASE_SERVER_FULL` (or discrete vars), `SELECT 1`; for the default `DATABASE_SCHEMA` and every extra `DATABASE_SCHEMA_{POSTFIX}`: create a real table, insert, read back, drop; the read-only role (`DATABASE_SERVER_USER_RO`) must `SELECT` and be **refused** writes |
| `redis` | redis, namespace-redis | `PING`, then `SET`/`GET`/`DEL` on `{REDIS_PREFIX}:e2e-healthcheck` (the ACL only grants the prefixed keyspace) |
| `s3` | minio-storage | `PutObject`/`GetObject`/compare/`RemoveObject` in `OBJECT_STORE_BUCKET_NAME` |
| `oidc` | keycloak | GET `OIDC_DISCOVERY_URL` (assert `issuer` + `token_endpoint`), then a **client-credentials token grab** with `OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` (falls back to discovery-only if the grant is unavailable, clearly marked - never a silent pass) |
| `path` | persistent-storage, temp-storage | for each of `DATA_PATH` / `TEMP_PATH`: write a file, `fsync`, read back, compare, delete |
| `metadata` | publish-on-web, metrics-scraper, platform, send-email | assert presence and echo (`DEPLOYMENT_NAME`, `PUBLIC_HOST`, ...); secret-looking values are redacted |

The mail probe stays `metadata` on purpose: a real send counts against the
project's daily budget on the relay, so the check round never sends. Instead the
status page carries a manual **"Stuur testmail"** form (`POST /send-testmail`)
when send-email is bound: STARTTLS + AUTH as the injected account, one message
to an address you choose, and the subject line in the response so you can find
it at the receiving end (sandbox: Mailpit on the sink, port 8025).

Beyond the round-trip, for every **bound** service it asserts that **all** env
vars the platform injects for it are actually injected (the key exists) - a
dropped or renamed variable is a provisioning-drift bug and fails the check. An
injected-but-empty value is left to the handler, which surfaces it with a precise
error (e.g. an auth failure) instead of aborting before the round-trip runs. A
service whose connection vars are absent is reported `skipped`, not failed, so the
same image works for a project that binds only some services.

The Postgres **read-only role** is enforced when its credentials are injected
(must `SELECT`, must be refused writes); when the platform has not provisioned RO
credentials it is reported `read_only: skipped (no RO credentials provisioned)` -
visible in `/status`, but it does not fail the database check, whose core is the
read/write binding and every schema round-trip.

## Scan-driven coverage (stays in sync with the platform)

What it tests is **not hardcoded**. `scripts/generate_probe_spec.py` imports OPI's
own service registry (`opi/services/services.py`) and `*Variables` enums and emits
`images/e2e-allservices/probe_spec.json`: per resource, the probe kind plus every
env var the platform injects - canonical names, `APP_` aliases, and the computed
secret keys (`DATABASE_SERVER_FULL`, `OBJECT_STORE_ENDPOINT_URL`, `REDIS_URL`),
discovered by instantiating each secret class and calling `to_k8s_secret_data()`,
the same method the platform uses. The spec is embedded in the Go binary via
`go:embed`.

Consequences:

- Adding a variable to an existing service, or a second service of an existing
  kind, is picked up automatically on regenerate - no code change.
- A genuinely new *kind* of resource (say a message queue) needs one new handler
  in the Go binary plus one line in `KIND_MAP`. This is the honest boundary of
  "dynamic": you cannot derive an AMQP publish/consume from a var name alone, but
  you pay it once per resource class, not per service or per variable.
- `tests/test_probe_spec_drift.py` regenerates the spec and fails if the committed
  file drifts, so a new service or injected variable that isn't reflected breaks
  the build instead of silently going untested.

Regenerate after touching services/variables:

```bash
uv run python scripts/generate_probe_spec.py     # or: task generate-probe-spec
```

## HTTP surface

- `GET /` - a plain human page: a `Hello, world` banner + a live table of
  service -> OK/FAIL/skipped + latency. Eyeball a deployment in a browser via the
  project's public ingress.
- `GET /healthz` - `200 OK` once the process is listening (liveness only; never
  reflects a downstream service).
- `GET /status` - JSON, the payload the E2E suite asserts on:

```json
{
  "deployment": "alls123-xy-main",
  "component": "web",
  "ready": true,
  "all_ok": true,
  "services": {
    "postgres": { "id": "postgres", "kind": "sql", "bound": true, "ok": true,
                  "latency_ms": 42,
                  "detail": { "schemas": { "public": "ok", "reporting": "ok" },
                              "read_only": "read ok, write refused (permission denied)" } },
    "redis":    { "id": "redis", "kind": "redis", "bound": true, "ok": true },
    "minio":    { "id": "minio", "kind": "s3", "bound": true, "ok": true },
    "oidc":     { "id": "oidc", "kind": "oidc", "bound": true, "ok": true,
                  "detail": { "issuer": "https://keycloak.../realms/xyz" } },
    "storage-data": { "bound": false, "ok": null }
  }
}
```

`GET /status?strict=1` returns `503` when not all bound services verify (for
probe-style use). The server binds `:8080` immediately, before the first check
round, so the platform's `tcpSocket:8080` probe passes at once.

## Platform constraints it satisfies

The platform forces a hard securityContext on every component pod
(`runAsUser: 1001`, `runAsNonRoot`, `fsGroup: 1001`, drop ALL capabilities). The
image is a single static Go binary on `distroless-static` (`nonroot`): it holds
no writable state and runs cleanly as an **arbitrary UID**, listens on TCP 8080,
and cold-starts to "listening" instantly.

## How the E2E suite uses it

`operations-manager/python/tests/e2e/helpers/lifecycle.py` sets
`RUNNABLE_IMAGE` to this image; every created project deploys it.
`tests/e2e/test_sandbox_all_services.py::test_all_services_status_reports_every_binding_ok`
reads `/status` from the workload pod over a `kubectl port-forward` (bypassing
the ingress and the auth-wall sidecar; the image is distroless so `exec` + curl
is not possible) and asserts `all_ok` plus, for every bound resource service,
`bound: true, ok: true` - including the postgres schema round-trip and RO-role
behaviour. This turns "pod Healthy" into "every bound service is actually
reachable with the injected credentials".

## Build / publish

```bash
task publish-e2e-allservices    # regenerate spec, then multi-arch build + push
```

or directly:

```bash
uv run python scripts/generate_probe_spec.py
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/minbzk/base-images/e2e-allservices:latest --push images/e2e-allservices
```

On a network-restricted host where the build container cannot reach
`proxy.golang.org`, use a single-arch `docker build --network=host` (module
download then uses the host network).

## Dependencies

- Runtime: Go stdlib + `jackc/pgx/v5` (Postgres), `redis/go-redis/v9`,
  `minio/minio-go/v7` (S3). Built `CGO_ENABLED=0`, `-trimpath -ldflags="-s -w"`.
- Build-time spec generator: Python, imports OPI's `opi.services` /
  `opi.utils.secrets`. Never runs in the pod.

## Validation

Validated end to end on the live sandbox: created an all-services project that
deployed this image (kind-loaded as `local/e2e-allservices:latest`), and the
workload reported **`all_ok: true` (9/9 bound services)** against the real
platform resources with the injected credentials - the full write/read loop for
Postgres (schema round-trip), Redis (prefixed ACL key), MinIO (put/get/remove)
and the PVCs, plus a real client-credentials token grab against the sandbox
Keycloak (issuer decoded from the returned JWT). It also runs cleanly as UID 1001
and the `metrics` auth token is redacted in `/status`. The RO role showed
`skipped (no RO credentials provisioned)` on that project - the image surfacing a
real platform state, not a false green.

## Notes / follow-ups

- The final image is ~14-15 MB (three service clients in one static binary); the
  compressed layer pushed to the registry is a few MB. This is above the
  "< 10 MB" design target but well within budget for a fast-booting fixture.
- `rig-world` can be retired from the E2E path once this fixture is published to
  ghcr and the suite runs green in CI (see the plan's next-steps).
