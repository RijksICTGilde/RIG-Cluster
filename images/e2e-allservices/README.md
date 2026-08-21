# e2e-allservices

A minimal, fast-booting test workload for the RIG-Cluster / ZAD platform. On
startup it does a **real write-and-read-back round-trip against every platform
service it is bound to** — PostgreSQL (incl. every extra schema and the
read-only role), Redis, MinIO/S3, Keycloak/OIDC, and mounted PVCs — logs each
step to stdout, and reports the outcome over HTTP. When this image boots green,
the whole binding actually works with the exact credentials the platform
injected.

It replaces the trivial `hello-world` workload as the E2E fixture (which only
proved a pod could start and pass a TCP probe) and doubles as a smoke-test
workload a human can deploy to sanity-check a cluster in a browser.

See `features/e2e-allservices-image.md` for the full design and rationale.

## What it checks

Coverage is **scan-driven**: `probe_spec.json` is generated from OPI's own
service registry by `scripts/generate_probe_spec.py` and embedded in the binary,
so a new service or injected env var is picked up automatically (a drift test
fails the build otherwise). Each bound service maps to one reusable *probe kind*:

| Kind | Round-trip |
|---|---|
| `sql` | connect, `SELECT 1`; per schema (default + every `DATABASE_SCHEMA_{POSTFIX}`) create a table, insert, read back, drop; RO role must read and be refused writes |
| `redis` | `PING`, then `SET`/`GET`/`DEL` on `{REDIS_PREFIX}:e2e-healthcheck` (honours the ACL) |
| `s3` | `PutObject`/`GetObject`/compare/`RemoveObject` in the bound bucket |
| `oidc` | GET discovery doc (assert `issuer` + `token_endpoint`), then a client-credentials token grab (falls back to discovery-only, clearly marked) |
| `path` | write a file, `fsync`, read back, compare, delete for each mounted path |
| `metadata` | assert presence and echo (e.g. `DEPLOYMENT_NAME`, `PUBLIC_HOST`) |
| `vlam` | `GET {VLAM_API_URL}/v1/models`; a models document passes, a 401/403 passes too (only VLAM itself can answer that, so the path stands), anything else fails with the suspect hop named |

For every bound service it also asserts **all** injected env vars are present and
non-empty — a dropped or renamed variable is itself a provisioning bug.

A service whose connection vars are absent is reported `skipped`, not failed, so
the same image works for a project that binds only some services.

## HTTP surface

- `GET /` — a plain human page: a `Hello, world` banner plus a live table of
  service -> OK/FAIL/skipped + latency.
- `POST /send-testmail` — sends one real message through the platform relay
  (STARTTLS + AUTH, so it counts against the project's daily budget). Manual by
  design: the periodic mail probe stays `metadata` (presence only) so the check
  round never spends budget. The page shows a "Stuur testmail" form when the
  send-email service is bound; the redirect carries the subject line so you can
  find the message at the receiving end (sandbox: Mailpit on the sink, port 8025).
- `POST /vlam-chat` — does one real chat completion against
  `{VLAM_API_URL}/v1/chat/completions` with a token you type into the form, and
  puts the answer on the page. Manual for the same reason the testmail button is:
  the periodic `vlam` probe carries no credential, so it proves the chain but says
  nothing about whether a project's token opens the door and a model answers. The
  form appears only when the vlam service is bound; without it the path is a 404.
  There is no streaming and no conversation history — this is a proof, not a chat
  client.
- `GET /healthz` — `200 OK` once the process is listening (liveness only).
- `GET /status` — JSON, the payload the E2E suite asserts on: per-service
  `bound` / `ok` / `latency_ms` / details, plus an overall `all_ok`. Add
  `?strict=1` to get `503` when not all bound services verify.

The server binds `:8080` immediately (before the first check round) so the
platform's `tcpSocket:8080` probe passes at once.

## The two buttons are on a public page

With `publish-on-web` bound, this status page is reachable by anyone, and both
buttons act for real. That is a deliberate, bounded trade-off, and it is the same
one for both. Neither button hands a visitor anything they could not already do
without it: the mail button sends as an account whose credentials the platform
injected here anyway, and the chat button needs a VLAM token the visitor must
supply themselves — the pod holds none. Both destinations are fixed to the
injected address and one path each, so neither is a proxy that can be pointed
somewhere else. What a visitor can do is spend: a message off the project's daily
relay budget, or a chat completion off whatever the supplied token pays for.

The one thing the chat button does widen is reach: VLAM is cluster-internal, and
this button lets anyone holding a valid VLAM token ask it one question from
outside. That is bounded to the single chat-completion path with the caller's own
credential, and this is a test workload rather than an application handling
anyone's data — but if that trade is not wanted for a given deployment, do not
bind `publish-on-web` to it, and the page is internal again.

The token gets the stricter treatment, because it is the only secret that arrives
from outside: it is used for one outgoing request and then dropped. It is never
stored, never written to the log, and never rendered back into the page — not
even as the value of the form field it came from, and not when the far end echoes
it back at us: it is stripped from a quoted error body and from the model's own
answer alike, because "repeat your context" is the same echo. The question and the answer
stay off the log too, which is why the chat button answers the POST in place
instead of redirecting: a redirect would carry the answer through a query string
and into every access log between here and the browser.

## Platform constraints it satisfies

- Runs cleanly as an **arbitrary non-root UID** (distroless-static, no writable
  state) under the platform's forced `runAsNonRoot` / `fsGroup: 1001`
  securityContext.
- Listens on **TCP 8080** on all interfaces.
- Tiny and instant-booting: a single static Go binary, no interpreter warm-up.

## Build

The probe spec must be current before building (a committed-vs-registry drift
test enforces this):

```bash
uv run python scripts/generate_probe_spec.py
```

Then, via the Taskfile (regenerates the spec, then builds + pushes multi-arch):

```bash
task publish-e2e-allservices
```

Or directly:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/minbzk/base-images/e2e-allservices:latest --push images/e2e-allservices
```

On a network-restricted host where the build container cannot reach
`proxy.golang.org`, add `--network=host` to a single-arch `docker build` (module
download then uses the host network), matching how this repo builds its other
images.

## Local development

```bash
# Build and run, all services unbound (only the platform metadata check is "bound"):
docker build --network=host -t e2e-allservices:local images/e2e-allservices
docker run --rm --user 1001:1001 -p 8080:8080 \
  -e DEPLOYMENT_NAME=dev -e COMPONENT_NAME=web e2e-allservices:local
curl -s localhost:8080/status | jq
```

Point env vars at real services (see the tables in
`features/e2e-allservices-image.md`) to exercise each probe kind.
