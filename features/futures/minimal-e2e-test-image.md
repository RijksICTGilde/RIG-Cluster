# Minimal all-services E2E test workload image

> **Status: PLANNED / not started.** Design complete, grounded against the live
> platform (securityContext, probe, injected env-var names verified today).
> Implementation not begun. Pick up from "Next steps" below.

## Context — why

The sandbox E2E suite deploys a trivial "hello world" web image as the test
workload (`RUNNABLE_IMAGE = "ghcr.io/minbzk/base-images/hello-world:latest"` in
`operations-manager/python/tests/e2e/helpers/lifecycle.py`). All it proves is
that **a pod can start and pass a TCP probe on :8080**. It does *not* touch any
of the platform services the project binds (database, Redis, MinIO, Keycloak),
so a create-with-all-services E2E test can go green while a service is
misprovisioned — wrong secret keys, an unreachable host, a missing extra schema,
a broken OIDC discovery URL. The test asserts "pod Healthy", not "the app can
actually use what the platform gave it".

There is an existing `rig-world` project/image that does exercise services, but
it is **too big and too slow to boot** to be a good E2E fixture — it drags out
every create/delete cycle and the suite already fights ~4-minute
`wait_for_project_apps_healthy` waits.

**Goal:** a purpose-built, ultra-minimal, fastest-booting test workload that, on
startup, does a **real read/write round-trip against every platform resource it
is bound to** — database (incl. every extra schema), Redis, MinIO/S3, Keycloak,
and the mounted PVCs — **logs each step and every failure clearly** to stdout,
reports the outcome over HTTP, and is fully up + verified within ~5 s. When this
image boots green we *know* the whole binding actually works, end to end, with
the exact credentials the platform injected. It also serves a plain human-facing
web page (a "hello world" string + a live status summary) because it can — handy
for eyeballing a deployment in a browser. This replaces `rig-world` as the E2E
fixture and doubles as a smoke-test workload a human can deploy to sanity-check a
cluster.

The requirement is deliberately strong: **no check is "optional" or "just report
the vars are present".** For every bound resource the image performs an actual
authenticated operation (write *and* read back, verifying the bytes), because the
whole point is to catch a mis-provisioned secret, an unreachable host, a missing
grant, a wrong bucket, or an unmounted volume — none of which a
"credentials-present" check would notice.

Equally important, **what it tests is derived from a scan of the platform's own
service definitions, not hardcoded.** The image stays in sync with the platform
automatically: it reads the service catalog / `*Variables` enums (via a
build-time generated spec) to learn which services exist, every env var each
injects, and how to exercise it — and asserts *every* injected variable is
actually present. See "Dynamic, scan-driven coverage" below.

## Hard platform constraints (verified live — the image MUST satisfy these)

These are not negotiable; a stock image that ignores them CrashLoopBackOffs.

1. **Arbitrary non-root UID.** The platform forces a strict securityContext on
   every component pod (OpenShift-style):
   ```yaml
   runAsUser: 1001
   runAsNonRoot: true
   runAsGroup: 1001
   fsGroup: 1001
   allowPrivilegeEscalation: false
   capabilities: { drop: ["ALL"] }
   seccompProfile: { type: RuntimeDefault }
   ```
   The image MUST run cleanly as an **arbitrary UID** (do not bake in a
   build-time user that owns files; write only to writable / group-owned dirs,
   or hold no writable state at all). A stock nginx pinned to UID 101
   (`nginxinc/nginx-unprivileged`) crashes here because it cannot write
   `/var/cache/nginx` when forced to 1001 — the current hello-world happens to
   survive only because it redirects nginx temp paths to `/tmp` (see
   `images/hello-world/nginx.conf`). A single static binary that listens and
   holds no writable state sidesteps the whole class of problem.

2. **Listen on TCP 8080.** The generated readiness/liveness probe is a
   `tcpSocket` on port **8080**. The HTTP server MUST bind `:8080` (all
   interfaces / `0.0.0.0`). This is also why boot must be near-instant: the probe
   starts firing immediately and a create test blocks on the app going Healthy.

3. **Tiny + instant boot.** Target image < 10 MB, cold-start to
   "listening on :8080" in well under a second, all service checks done within
   ~5 s total. No interpreter warm-up, no framework init.

## Injected service env vars (the real names — read from `opi/services/services.py`)

The platform injects each bound service's connection details into the component
as env vars, **plus `APP_`-prefixed aliases** for most of them (values are
identical; the alias exists for apps that expect the `APP_` convention). The
image should read the canonical name and fall back to the `APP_` alias. The
source of truth is the `*Variables` enums in
`operations-manager/python/opi/services/services.py`; do not invent names.

**PostgreSQL database** (`DatabaseVariables`, lines 224–289):

| Env var | Alias(es) | Meaning |
|---|---|---|
| `DATABASE_SERVER_HOST` | `APP_DATABASE_SERVER_HOST`, `APP_DATABASE_SERVER` | host |
| `DATABASE_SERVER_PORT` | `APP_DATABASE_PORT`, `APP_DATABASE_SERVER_PORT` | port |
| `DATABASE_SERVER_USER` | `APP_DATABASE_USER` | username |
| `DATABASE_PASSWORD` | `APP_DATABASE_PASSWORD` | password |
| `DATABASE_SERVER_USER_RO` | `APP_DATABASE_USER_RO` | read-only username |
| `DATABASE_PASSWORD_RO` | `APP_DATABASE_PASSWORD_RO` | read-only password |
| `DATABASE_DB` | `APP_DATABASE_DB` | database name |
| `DATABASE_SCHEMA` | `APP_DATABASE_SCHEMA` | default schema |
| `DATABASE_SERVER_FULL` | `APP_DATABASE_SERVER_FULL` | full connection string |

- **Extra schemas (RC-17):** each user-declared extra schema is exposed as
  `DATABASE_SCHEMA_{POSTFIX}` (+ `APP_DATABASE_SCHEMA_{POSTFIX}`), postfix
  uppercased — see `opi/utils/naming.py:560` (`DATABASE_SCHEMA_{POSTFIX}`) and
  `opi/utils/secrets.py:162` (`extra_schemas`). The names are dynamic, so the
  image must **discover them by scanning the environment** for keys matching
  `^DATABASE_SCHEMA_[A-Z0-9_]+$` (excluding the plain `DATABASE_SCHEMA`) rather
  than expecting a fixed list.

**Keycloak / OIDC** (`KeycloakVariables`, lines 306–350) — *no `APP_` aliases*:
`OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_PUBLIC_CLIENT_ID`,
`OIDC_DISCOVERY_URL`, `OIDC_URL`, `OIDC_REALM`, `OIDC_HOSTNAME`.

**MinIO / object storage** (`MinIOVariables`, lines 353–398, + computed keys in
`opi/utils/secrets.py:247`):
`OBJECT_STORE_HOST`, `OBJECT_STORE_PORT`, `OBJECT_STORE_USER` (access key),
`OBJECT_STORE_PASSWORD` (secret key), `OBJECT_STORE_BUCKET_NAME`,
`OBJECT_STORE_REGION`, plus computed `OBJECT_STORE_URL` and
`OBJECT_STORE_ENDPOINT_URL` (each with an `APP_` alias).

**Redis** (`RedisVariables`, lines 412–460):
`REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME`, `REDIS_PASSWORD`, `REDIS_PREFIX`,
`REDIS_URL` (each with an `APP_` alias). **Honour `REDIS_PREFIX`:** the ACL only
grants access to keys/channels beginning `{prefix}:`, so the round-trip key MUST
be `{REDIS_PREFIX}:e2e-healthcheck` — an unprefixed key gets NOPERM and would
make a *correct* Redis look broken.

**Storage (PVC mounts)** (`StorageVariables`): `DATA_PATH` (default `/data`),
`TEMP_PATH` (default `/tmp`) — direct paths, no secret. **Required check** (not
optional): for each path that is set, write a small file, `fsync`, read it back,
compare bytes, then delete it — this proves the PVC is actually mounted,
writable under the forced `fsGroup: 1001`, and durable. A read-only or
unmounted volume is a real, common provisioning bug this must catch.

**Web / publish** (`WebVariables`): `PUBLIC_HOST`, `PUBLIC_HOSTNAME` — informational
only (the app's own public URL); report but nothing to "connect" to.

**Platform (always present)** (`PlatformVariables`): `DEPLOYMENT_NAME`,
`COMPONENT_NAME` — echo these in `/status` for traceability.

## Design

### Language / runtime

**Recommendation: a single statically-linked Go binary** (`CGO_ENABLED=0`,
scratch or distroless-static base). Rationale:

- One self-contained binary, no libc, no interpreter → image a few MB, boot is
  instantaneous (no runtime warm-up before it can bind :8080).
- Trivially runs as any UID from `scratch` (no user DB, no writable state).
- First-class, well-maintained clients for exactly the services in scope:
  `github.com/jackc/pgx` (Postgres), `github.com/redis/go-redis`,
  `github.com/minio/minio-go` (S3), and OIDC discovery is a plain HTTPS GET of
  `.../.well-known/openid-configuration` (stdlib `net/http` + `encoding/json`).
- Easy concurrency: run all service checks in parallel goroutines with a short
  per-check timeout so total verification stays within the ~5 s budget.

**Alternatives considered (and why not):**

- *Rust* — equally tiny/fast, but the team's ecosystem here is Python/Go; Go's
  service clients are the more familiar, lower-friction choice. Fine as a
  fallback if a Go author isn't available.
- *Python (FastAPI/uvicorn or stdlib)* — matches OPI's stack, but interpreter +
  driver import cost eats into the boot budget and the image balloons well past
  10 MB. Rejected for the "fastest boot" requirement.
- *Static nginx + shell probes* — cannot do real authenticated round-trips to
  Postgres/Redis/S3/OIDC; this is precisely the gap we're closing. Rejected.
- *Distroless base vs scratch* — `gcr.io/distroless/static` adds CA certs
  (needed for the HTTPS OIDC discovery / TLS S3 endpoints) for a few hundred KB;
  from `scratch` you must `COPY` a `ca-certificates.crt` yourself. Either works;
  distroless-static is the lower-effort default.

### Dynamic, scan-driven coverage (keep it in sync with the platform — don't hardcode)

The env-var tables above describe **how the platform works today**; they must not
be baked into the binary as a frozen list, or the image silently rots the moment
a service adds a variable or a new service is introduced. The platform already
has a single source of truth for this: the `*Variables` enums and the service
catalog in `operations-manager/python/opi/services/`. The image's knowledge of
*what to test* should be **derived from that scan**, so coverage tracks the
platform automatically.

Concretely:

1. **Build-time scan → spec.** A small generator (a `scripts/` tool, run in the
   image's build / in CI) imports the service registry and every `*Variables`
   enum and emits a machine-readable **probe spec** (JSON) grouping, per service:
   its env-var names (canonical + `APP_` aliases + the dynamic
   `DATABASE_SCHEMA_{POSTFIX}` pattern), which vars are the connection parameters
   vs. metadata, which are secret (redact in logs), and the service's **probe
   kind** (see below). This spec is embedded in the image (Go `embed`). Because it
   is generated from the enums, adding a var to an existing service means a
   regenerate, no code change — and a CI check can fail if the committed spec
   drifts from the enums.
2. **Probe kinds, not per-service code.** The actual "how do I round-trip this"
   logic is a small, fixed set of **probe-kind handlers** — `sql`, `redis`, `s3`,
   `oidc-http`, `path`, `metadata` — one each, reused across services. The scan
   maps every service to one kind; the runtime dispatches on kind. This is the
   honest boundary of "dynamic":
   - **New env var on an existing service / a second instance of a kind →
     automatic** (the scan picks it up, the existing handler uses it).
   - **A genuinely new *kind* of resource** (say, a message queue) → one new
     handler (a few dozen lines) + tag the service with that kind. Everything else
     stays generic. There is no way around this: you cannot derive "issue an AMQP
     publish/consume" from an env-var name alone — but you only pay it once per
     resource *class*, not per service or per variable.
3. **Assert every injected var — coverage, not just connectivity.** For each bound
   service the runtime asserts **all** its spec'd env vars are actually present
   and non-empty (a dropped/renamed var is itself a provisioning bug), logs the
   set it saw, and reports any missing ones as a FAIL. Connection vars are then
   used in the real round-trip; metadata vars (`DEPLOYMENT_NAME`, `PUBLIC_HOST`,
   …) are asserted-present + echoed into `/status`. So "all env vars are tested"
   is satisfied in two tiers: **presence** for every var, **exercised** for every
   connection var. This is cheap and worth it.

The per-service check descriptions in "Startup behaviour" below are therefore the
**behaviour of each probe kind**, not bespoke code paths — the scan decides which
kind runs for which bound service.

### Startup behaviour

On start the binary:

1. Loads the embedded probe spec (from the scan) and reads the environment once.
   For each service in the spec it is "**bound**" iff its connection vars are
   present (e.g. `DATABASE_SERVER_HOST` set ⇒ run the `sql` probe). A service
   whose vars are **absent is skipped** (reported `skipped`, not `FAIL`) — the
   same image must work for a project that binds only some services. A service
   that is bound but **missing some of its spec'd vars is a FAIL**, not a skip.
2. Runs each bound resource's check **concurrently**, each with a short timeout
   (e.g. 2–3 s) so one slow/broken service can't blow the 5 s budget. Every check
   does a **real write-and-read-back**, not a ping:
   - **Postgres (write/read):** connect using `DATABASE_SERVER_FULL` (fall back to
     the discrete host/port/user/password/db vars), `SELECT 1`. Then for the
     default `DATABASE_SCHEMA` **and every discovered `DATABASE_SCHEMA_{POSTFIX}`**:
     `CREATE TEMP TABLE` (or a temp table in that schema), `INSERT` a marker row,
     `SELECT` it back, verify, drop it. This proves the role can actually write in
     each schema, not merely that the schema exists. Report each schema
     individually.
   - **Postgres read-only role (required):** connect as `DATABASE_SERVER_USER_RO`
     / `DATABASE_PASSWORD_RO`, assert it **can `SELECT`** and that a write is
     **refused** (expect a permission error). This catches grant/ownership bugs
     that a single-role check misses.
   - **Redis (write/read):** connect (honour `REDIS_URL` / discrete vars +
     `REDIS_USERNAME` ACL), `PING`, then `SET` a value, `GET` it back and compare,
     `DEL` it — all on key `{REDIS_PREFIX}:e2e-healthcheck` (the ACL only grants
     the prefixed keyspace; see the `REDIS_PREFIX` note above).
   - **MinIO / S3 (write/read):** client from `OBJECT_STORE_ENDPOINT_URL` (or
     host/port) + access/secret key + region; `PutObject` a tiny object with known
     bytes into `OBJECT_STORE_BUCKET_NAME`, `GetObject` it back, compare bytes,
     then `RemoveObject` it.
   - **Keycloak / OIDC (required):** HTTPS GET `OIDC_DISCOVERY_URL` (fall back to
     `OIDC_URL`/`OIDC_REALM` →
     `.../realms/{realm}/.well-known/openid-configuration`), assert 200 + a
     parseable doc containing `issuer` and `token_endpoint`, **then perform a
     client-credentials token request** against `token_endpoint` using
     `OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` and assert a bearer token comes back
     (and, cheaply, that it decodes as a JWT with the expected `iss`). This proves
     the injected client secret actually authenticates, not just that the realm is
     reachable. (If a given project's confidential client is configured without
     the client-credentials grant, fall back to discovery-only and say so in the
     status/log — but attempt the token grab by default.)
   - **Storage / PVC (required):** for each of `DATA_PATH` / `TEMP_PATH` that is
     set, write a small file, `fsync`, read it back, compare, delete — proving the
     volume is mounted and writable under `fsGroup: 1001`.
3. **Logs every step to stdout** as it runs (see "Logging" below), so
   `kubectl logs` alone tells the full story of what was attempted and what
   failed, with latencies and error detail.
4. Caches the per-resource results (status + latency + error message) and keeps
   re-running them lazily/periodically so `/status` reflects current reality, not
   just the boot snapshot.
5. Serves HTTP on `:8080` **immediately** (bind first, so the TCP probe passes
   even while the first check round is still in flight).

### Logging

The image is only as useful as its logs when something is red, so logging is a
first-class feature, not an afterthought:

- Emit a clear, timestamped line per check to **stdout** (the platform collects
  pod stdout): a `START <service> <target-host>` line, then either
  `OK <service> <op> (<latency>ms)` or `FAIL <service> <op>: <error>` with the
  underlying driver error verbatim (host, code, message) — never a swallowed
  generic. Redact secret values (log the host/user/bucket/key-name, never the
  password/secret).
- At the end of the first round print a one-line **summary banner**
  (`ALL OK (5/5)` or `FAILED: postgres(schema=reporting), minio`) so a human
  scanning `kubectl logs` sees the verdict instantly.
- Structured (JSON) log lines are fine and preferred if cheap, but a
  human-readable one-line-per-step format is the hard requirement — the goal is
  "open the logs, immediately understand what the app did and what broke".

### HTTP surface

- `GET /` → a plain **human-facing web page**: a big `Hello, world` string plus a
  small live summary rendered from the cached results (e.g. a table of
  service → OK/FAIL/skipped + latency, and the deployment/component name). Serving
  a real page is a requirement ("because it can") — it makes the workload
  eyeball-able in a browser via the project's public ingress, and doubles as a
  trivial publish-on-web smoke test. Keep it a single self-contained HTML string,
  no assets, no framework.
- `GET /healthz` → `200 OK` as soon as the process is up and listening (liveness;
  matches the platform's tcpSocket probe intent, and gives a cheap HTTP check
  too). Never fails on a downstream service — it reports process liveness only.
- `GET /status` → JSON, the real payload the E2E suite asserts on. Shape (draft):
  ```json
  {
    "deployment": "e2e97-llv-main",
    "component": "web",
    "all_ok": true,
    "services": {
      "postgres": { "bound": true, "ok": true, "latency_ms": 12,
                    "schemas": { "app": "ok", "reporting": "ok" } },
      "redis":    { "bound": true, "ok": true, "latency_ms": 4 },
      "minio":    { "bound": true, "ok": true, "latency_ms": 21 },
      "oidc":     { "bound": true, "ok": true, "latency_ms": 33,
                    "issuer": "https://keycloak.../realms/xyz" },
      "storage":  { "bound": false, "ok": null }
    }
  }
  ```
  Return HTTP 200 whenever the report is *produced* (so tests read the body and
  assert per-service), and put the overall verdict in `all_ok`. (Optionally a
  `?strict=1` mode that returns 503 when `all_ok` is false, for probe-style use.)

## How the E2E suite uses it

1. Publish the image (see next section) and point the fixture at it: replace
   `RUNNABLE_IMAGE` in
   `operations-manager/python/tests/e2e/helpers/lifecycle.py` with the new tag
   (e.g. `ghcr.io/minbzk/base-images/e2e-allservices:latest`). The long comment
   there documenting the securityContext/8080 constraints stays valid — this
   image is built to the same contract; update it to point at this doc rather
   than "use the platform's own base image".
2. The all-services flow (`walk_create_wizard_with_services` +
   `create_project_with_services`, driven from `test_sandbox_all_services.py`)
   already selects Postgres/Redis/MinIO/Keycloak/etc. After the project is
   Healthy, add an assertion step that fetches the deployed component's `/status`
   (via the project's public URL / ingress, or `kubectl exec`/port-forward) and
   asserts `all_ok == true` **and** every service the project bound shows
   `bound: true, ok: true` — including one asserted `DATABASE_SCHEMA_{POSTFIX}`
   entry when an extra schema was declared. This turns "pod Healthy" into "every
   bound service is actually reachable with the injected credentials".
3. Keep the plain hello-world path for the no-services lifecycle test if desired,
   or converge everything on the new image (it skips absent services, so it is a
   safe drop-in for the trivial case too).

## Where the source lives + how it's built/hosted

- **Source:** a new folder under the repo's existing image collection,
  `images/e2e-allservices/` (siblings: `images/hello-world/`,
  `images/zad-waker/`, `images/postgresql-with-dictionaries/`, …). Contains the
  Go module + a multi-stage `Dockerfile` (build stage compiles the static
  binary; final stage is `scratch`/distroless-static with just the binary + CA
  certs). Add a short `README.md` and register it in `images/README.md`.
- **Build/publish:** follow the repo's established pattern —
  `docker buildx build --platform linux/amd64,linux/arm64 -t
  ghcr.io/minbzk/base-images/e2e-allservices:latest --push .` (same
  `ghcr.io/minbzk/base-images/*` namespace the current hello-world uses, so both
  sandbox and prod can pull it with the existing pull path). A Taskfile task
  (mirroring how other images are built) is the tidy home for this.
- **Fast sandbox iteration:** during development, push to the in-cluster
  `rig-registry` and pull via the existing `rig-registry-pull` secret instead of
  `kind load` — see `docs/sandbox-image-deploy-via-registry.md`. Because the
  final image is a single tiny layer, even a full re-push is seconds.
- **Pull secret:** the E2E projects pull user workloads the same way any
  component image is pulled; publishing under `ghcr.io/minbzk/base-images` keeps
  it consistent with today's `RUNNABLE_IMAGE` (no new pull-secret wiring needed).

## Open questions / next steps (resume here)

1. **Confirm language:** Go static binary (recommended) vs Rust — pick based on
   who implements it. Everything below assumes Go.
2. **Reach for `/status` in the test:** decide the access path the assertion
   uses — public ingress URL (realistic, but needs the route up) vs
   `kubectl port-forward`/`exec` (deterministic, no DNS/ingress dependency).
   Port-forward is the safer default for CI.
3. **OIDC token grant availability:** the plan requires a client-credentials
   token grab by default. Confirm the platform provisions the confidential client
   with the client-credentials (service-account) grant enabled; if some project
   shapes don't, define the discovery-only fallback precisely (and make sure the
   log/status clearly marks it as a downgrade, not a silent pass).
4. **Extra-schema write target:** decide whether the per-schema write uses a
   `TEMP` table `SET search_path` to the schema, or a real (then-dropped) table in
   the schema — the latter proves `CREATE` privilege in that specific schema,
   which is closer to what a real app needs. Lean to the real table.
5. **Strict probe mode:** should `/status?strict=1` (503 on failure) ever back a
   readiness probe, or is the platform's fixed tcpSocket:8080 the only probe? The
   platform generates the probe today, so this is informational unless
   configurable probes (`features/futures/configurable-health-probes.md`) land.
6. **Retire `rig-world`:** once the new fixture is proven on sandbox, remove
   `rig-world` from the E2E path and note it in the suite docs.
7. **Scan generator location + drift guard:** decide where the spec generator
   lives (`scripts/generate_probe_spec.py` importing the service registry) and add
   a CI/test that regenerates and diffs it against the committed spec, so a new
   service or env var that isn't reflected fails the build rather than silently
   going untested. Confirm the `*Variables` enums + catalog expose enough to
   derive each service's probe kind (they may need a tiny `probe_kind` hint per
   service if it can't be inferred from the var shape).
8. **Build the image, publish it, flip `RUNNABLE_IMAGE`, extend
   `test_sandbox_all_services.py` with the `/status` assertion, and validate
   end-to-end on the sandbox** before calling this done.
