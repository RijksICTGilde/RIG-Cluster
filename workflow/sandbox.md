# Sandbox Cluster

The sandbox is a full, throwaway copy of the ZAD platform you run on your own machine. It is where you exercise real end-to-end behaviour (wizard, API, project-file writes, ArgoCD deploys) without touching production.

## It is local, not hosted

`https://zad.sandbox.rijksapp.dev` looks like a hosted URL but it is not. The whole `*.sandbox.rijksapp.dev` wildcard resolves to `127.0.0.1` (verify with `dig zad.sandbox.rijksapp.dev +short`). Everything runs in a local **Kind** cluster named `rig-sandbox` inside Docker on whatever machine ran `task sandbox:setup`.

Two things make the loopback URL work with valid TLS:
1. Public DNS points `*.sandbox.rijksapp.dev` at `127.0.0.1`.
2. A real Let's Encrypt wildcard cert (issued via TransIP DNS-01, stored AGE-encrypted in `security/tls/sandbox-wildcard/`) is served by the in-cluster ingress, so the browser and Playwright get a trusted cert even though the traffic never leaves the machine.

There is **no remote server and no shared state**. If a colleague also runs the sandbox, they have their own independent cluster. "Live sandbox" in the tests just means "a Kind sandbox is currently running and reachable at those URLs."

(On the shared Linux dev server the same Kind cluster sits behind Caddy on ports 8880/8443 — see `docs/sandbox-on-dev-server.md`. Still local Kind.)

## What runs in it

`sandboxed-local` is one of three cluster types (alongside `local` and `odcn-production`). Unlike `local` (external GitHub + git daemon), the sandbox runs an **in-cluster Forgejo** that is the GitOps source of truth. `task sandbox:setup` (~5-10 min) brings up: ingress-nginx, PostgreSQL (CNPG), Forgejo, Keycloak, MinIO, a container registry, Prometheus, ArgoCD, and the Operations Manager.

ZAD drives three Forgejo repos, exactly as in production: `zad-projects` (one YAML per project, source of truth), `zad-argo-user-applications`, and `zad-deployments`.

### Service URLs and credentials

From `docs/sandbox-reference.md` (printed at the end of setup). All dev-only, fixed because the cluster is disposable.

| Service | URL | Credentials |
|---|---|---|
| ZAD portal (Operations Manager) | https://zad.sandbox.rijksapp.dev | admin / admin1234 |
| ArgoCD | https://argo.sandbox.rijksapp.dev | admin / admin1234 |
| Forgejo (git) | https://forgejo.sandbox.rijksapp.dev | rig-admin / admin1234 |
| Keycloak | https://keycloak.sandbox.rijksapp.dev | admin / admin1234 |
| MinIO | https://minio.sandbox.rijksapp.dev | admin / admin1234 |
| Prometheus | https://prometheus.sandbox.rijksapp.dev | - |
| Registry | https://registry.sandbox.rijksapp.dev | - |

The OpenAPI spec for the running portal is at `https://zad.sandbox.rijksapp.dev/openapi.json` — fetch it to see the current API surface.

## Bringing it up

```bash
task sandbox:setup                     # full setup (needs the developer AGE key to decrypt the wildcard cert)
task sandbox:sync                      # push infra changes to in-cluster Forgejo
task sandbox:update-operations-manager # rebuild + redeploy OPI
task sandbox:skaffold-dev              # hot-reload OPI at localhost:9595
task sandbox:destroy                   # tear it all down
```

Prerequisites: `docs/sandbox-prerequisites.md` (kind, kubectl, kustomize, sops, age, pwgen, yq, Docker). The developer AGE private key (`security/developer-key.txt`) is obtained out-of-band and is required to decrypt the wildcard cert. The sandbox uses its own generated key, `security/sandbox-key.txt`, for its runtime secrets.

## Testing YOUR code: rebuild, redeploy, verify the running version

**Critical and easy to get wrong.** The sandbox runs whatever Operations Manager
image was last deployed — by default a released image from GHCR, or a previous
build. Running the sandbox E2E suite against it without rebuilding tests **stale
code**, not your PR. Your changes must be in the running pod first, and you must
*verify* that before trusting a green run.

This only applies to the **local sandbox flow** (a Kind cluster you drive yourself,
or the dclaude orchestrator's sandbox stage on the dev server). It is **not** part
of any GitHub CI/CD — GitHub does not run the sandbox.

### 1. Build + load + redeploy your code

```bash
task sandbox:update-operations-manager
```

This builds the OPI image from the current tree (`--target application`), `kind load`s
it into the cluster, redeploys, and stamps the deployment with
`ZAD_VERSION=$(git describe --tags --always)` + `ZAD_BUILD_DATE`. Wait for the rollout
to finish (`kubectl -n rig-system rollout status deployment/operations-manager`).

### 2. Verify the running version matches your commit

Do not skip this — it is the check that catches "I tested the old image":

```bash
EXPECT=$(git describe --tags --always)
RUNNING=$(curl -sk https://zad.sandbox.rijksapp.dev/version | jq -r '.version // .zad_version // empty')
echo "expect=$EXPECT running=$RUNNING"
# they must match before you run sandbox tests
```

`GET /version` returns the build metadata stamped at deploy time. If it is empty or
does not match, the deploy did not land (or injected no version) — redeploy before
testing. Only run the sandbox E2E suite once the running version is your build.

### In the dclaude orchestrator flow (dev server)

The **sandbox test stage** does step 1 automatically: the runner checks out the PR
branch, builds the image with a per-PR tag, `kind load`s it, swaps the deployment
image (`imagePullPolicy: IfNotPresent`, so the loaded image is used and not re-pulled),
waits for the rollout, then runs the tests. Step 2 (version verification) should be
part of the PR's own sandbox test so a stale/failed deploy fails the stage loudly —
add a `test_version_endpoint`-style assertion that the running `/version` equals the
commit under test. You do **not** run `task sandbox:update-operations-manager` by hand
there; but you **do** when testing manually in a dclaude session.

## Running tests against the live sandbox

The E2E harness (`operations-manager/python/tests/e2e/`) has two modes:
- **Local** (default): a mocked in-memory FastAPI test server, no cluster needed. Run with `-m "e2e and not sandbox"`.
- **Live sandbox**: real browser + API calls against the running Kind cluster, with results verified against the Forgejo `zad-projects` repo. Marked `@pytest.mark.sandbox`, gated on `E2E_BASE_URL`.

Run the sandbox lifecycle suite:

```bash
task test-e2e-sandbox
# which is roughly:
E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
E2E_SECRET_KEY=sandbox-dev-secret-key-fixed-for-stable-sessions-32min \
FORGEJO_URL=https://forgejo.sandbox.rijksapp.dev \
FORGEJO_USER=rig-admin FORGEJO_PASSWORD=admin1234 \
uv run pytest tests/e2e/ -m "e2e and sandbox" -v --timeout=300
```

If `E2E_BASE_URL` is unset, every sandbox test skips (the `sandbox_url` fixture). Details and rationale are in `features/e2e-sandbox-tests.md`.

### How auth works in sandbox tests

Tests do not perform a Keycloak login. They forge a pre-signed Starlette session cookie for an allowlisted user, signed with `E2E_SECRET_KEY`. That value **must equal the sandbox's real `SECRET_KEY`** (`sandbox-dev-secret-key-fixed-for-stable-sessions-32min`) or the cookie is rejected. Per-project API calls (add-component, delete) use the project's `X-API-Key`, scraped from the project-details page by `sandbox_api.read_api_key`.

### What the lifecycle suite proves

`test_sandbox_flows.py` is the template for "did the project file actually change":
- `test_version_endpoint` — public `GET /version` returns build metadata.
- `test_create_project_via_ui` — create a project through the wizard, then assert the YAML **exists in Forgejo `zad-projects`**.
- `test_add_component_via_api` — `POST /api/v2/projects/{name}/components`, then assert the component **lands in the Forgejo project file**.
- `test_delete_project_via_ui` — delete via the danger-zone modal, then assert the Forgejo file **disappears**.

The pattern to copy: drive the change through the real UI or API, then read back the authoritative project YAML from Forgejo (`ForgejoClient`) rather than trusting the HTTP response alone.

## Deploying your PR to the shared sandbox (dclaude sessions on the dev server)

On the shared dev server the sandbox is a **single** Kind cluster used by **one PR at a time**. When your task genuinely needs real end-to-end validation, you build your PR's image, put it on the cluster, and check `/version` — with two baked commands. Only do this when you actually need to test against the sandbox (it is a scarce, shared, locked resource).

### The two commands

```bash
sandbox-deploy      # claim the lock → build operations-manager from THIS repo
                    # → load into Kind → roll out → verify /version
# ... run your E2E against https://zad.sandbox.rijksapp.dev ...
sandbox-release     # free the lock for the next PR — ALWAYS run when done
```

- `sandbox-deploy` **holds** the lock so you can iterate: change code, run `sandbox-deploy` again to redeploy. It runs `task version:generate` first so `/version` reflects your commit, builds with `--network=host` (DNS), `kind load`s the image, rolls it out, and confirms the running `GET /version` matches what you built.
- `sandbox-release` frees the lock. The lease auto-expires if you forget, but always release so others aren't blocked.
- `orch sandbox status` shows who holds the sandbox and who is queued.

### The locking rule

Exactly one PR deploys at a time. `sandbox-deploy` calls `orch sandbox claim`; if another PR holds it you get a clear "busy" message — wait and retry, never force it. This stops the single cluster thrashing between different PR versions.

### Verifying the right version is live

`/version` reads `opi/version.json` first (baked from git), then falls back to the `ZAD_VERSION` env — so a build that didn't regenerate `version.json` shows a stale commit. `sandbox-deploy` handles this; to check by hand:

```bash
curl -sk https://zad.sandbox.rijksapp.dev/version    # compare against: git rev-parse --short HEAD
```

### Then run the E2E suite

With the sandbox on your commit, point the tests at it: `E2E_BASE_URL=https://zad.sandbox.rijksapp.dev` (+ `E2E_SECRET_KEY`) then `task test-e2e-sandbox`. `test_version_endpoint` confirms the build metadata end-to-end.
