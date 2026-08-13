# E2E + API tests (Playwright)

End-to-end tests that drive the real ZAD portal (wizard, API, delete flow) in a browser and
verify the outcome. There is a **ready-made base to build on** - fixtures already handle the
browser, authentication, screenshots and tracing. **Do not write your own browser launch or login
code**; take the fixtures below and go.

## TL;DR for adding a test

```python
# tests/e2e/test_my_thing.py
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]   # or just [pytest.mark.e2e] for local mode

def test_dashboard_loads(sandbox_url, sandbox_page, capture):
    sandbox_page.goto(f"{sandbox_url}/dashboard")     # already logged in - no auth code needed
    sandbox_page.wait_for_load_state("networkidle")
    capture(sandbox_page, "dashboard")                # screenshot -> tests/e2e/artifacts/
    assert "Dashboard" in (sandbox_page.text_content("body") or "")
```

That's the whole setup. `sandbox_page` is an authenticated, traced, screenshot-on-failure browser
page. Run it with `task test-e2e-sandbox`.

## Two modes (pick the cheaper one that proves your point)

| Mode | Marker | Auth | Externals | Run | Use when |
|---|---|---|---|---|---|
| **Local (in-process)** | `pytest.mark.e2e` | pre-signed cookie for `test@example.com` (`auth_page`) | mocked (no DB/Keycloak/git/k8s) | `task test-e2e` | UI/form/wizard *flow* logic - fast, deterministic, no sandbox |
| **Sandbox (live)** | `pytest.mark.e2e` + `pytest.mark.sandbox` | pre-signed cookie for `admin@sandbox.rijksapp.dev` (`sandbox_page`) | the real running sandbox | `task test-e2e-sandbox` | proving a change actually works end-to-end + lands in Forgejo |

Both are excluded from the default `pytest` run (`addopts = ... -m 'not ... and not e2e'`), so they
only run when you select the marker. Prefer **local** unless you specifically need the live cluster.

Local interactive dev server (auth disabled, port 8111) for poking by hand:
`cd operations-manager/python && uv run python -m tests.e2e.testserver`.

## Reusable building blocks - use these, don't rebuild

All in `tests/e2e/conftest.py` (fixtures) and `tests/e2e/helpers/` (page objects / clients).

**Fixtures (just add them as test arguments):**

| Fixture | Gives you | Mode |
|---|---|---|
| `auth_page` | authenticated Playwright `Page` (local in-process app) | local |
| `app_server` | base URL of the in-process app | local |
| `sandbox_url` | live sandbox base URL (skips the test if `E2E_BASE_URL` unset) | sandbox |
| `sandbox_page` | authenticated `Page` on the live sandbox, **with per-test trace + on-failure screenshot** | sandbox |
| `sandbox_context` | the authenticated browser context (session-scoped) if you need your own page | sandbox |
| `forgejo` | read-only `ForgejoClient` to verify `projects/{name}.yaml` | sandbox |
| `capture` | `capture(page, "label")` -> saves a named screenshot to the artifact dir | either |
| `artifact_dir` / `screenshot_dir` | output dirs for traces/screenshots | either |

**Helpers (`tests/e2e/helpers/`):**

- `wizard.py::WizardHelper` - create-project wizard page object (`open_create_wizard`,
  `fill_identity`, `fill_team`, `fill_component`, `click_next`, `submit_wizard`, ...). Also
  `_unique_project_name()` for collision-free names.
- `edit_modal.py::EditModalHelper` - drives detail-page modal edits.
- `project_actions.py::delete_project_via_ui` - drives the danger-zone delete modal.
- `forgejo.py::ForgejoClient` - `project_file_exists`, `get_project_yaml`, `wait_for_new_project`
  (discovers the random-postfix technical name by diffing the repo), `wait_for_component`,
  `wait_for_project_gone`.
- `sandbox_api.py` - `read_api_key` (scrapes the per-project key from the details page),
  `add_component` (calls the v2 endpoint + polls the task, surfacing real failures),
  `delete_project_via_api` (force teardown / cleanup safety net).

There is deliberately no separate cleanup registry: a suite that creates projects owns
their teardown in a module fixture's `finally`, calling `delete_project_via_api`. That
keeps the cleanup next to the thing that created it, and it runs even when the test
fails halfway.

## Do / Don't

- **Do** take `sandbox_page` / `auth_page` - they are already logged in. **Don't** call
  `browser.new_context()`, add cookies, or script a Keycloak login in a test.
- **Do** verify against Forgejo (`forgejo` fixture) - the committed project file is the source of
  truth. **Don't** assert on deployment/pod health unless that's the point of the test.
- **Do** reuse `WizardHelper` / helpers. **Don't** hard-code DOM selectors in the test if a helper
  already encapsulates them.
- **Do** reach a form control with `veldbesturing(page, pad)` (or `veldbesturing_eindigend_op`
  for a service-config field, whose path carries a `_services-config/...` prefix).
  **Don't** write `page.locator("[name='...']")` and `fill()` it: under NLDD that resolves to
  the custom element (`<nldd-text-field>`), and `fill()`/`input_value()` on it is a hard error
  ("Element is not an `<input>`"), not an empty field. This cost a whole suite its setup.
- **Don't** wait for `networkidle` after an action that lands on a page which polls itself
  (the progress page after creating a project polls with htmx, so the network never goes
  idle and the wait always times out). Wait for the landing itself - `submit_wizard()` does.
- **Do** name projects with `_unique_project_name()` and register cleanup. **Don't** leave test
  projects on the sandbox.
- **Do** keep new tests behind the right marker(s) so they never run in the default suite.

## Reference example

`tests/e2e/test_sandbox_flows.py` is the canonical lifecycle: create via UI -> add component via
API -> delete via UI, each verified against Forgejo, plus `test_version_endpoint`. Copy its shape.

### When the project file is not the whole answer

`tests/e2e/test_sandbox_repetitie.py` covers the two things the second dress rehearsal
(`docs/generale-repetitie-2026-08-13.md`) had to prove, and both need an assertion that Forgejo
cannot give:

- **A refused action must report itself in the task's own `status`.** The refusal happens in the
  work, not in the HTTP response (that is a plain `202`), so the outcome is only readable from
  `GET /api/tasks/{id}`. Polling the *file* would show nothing changed and call that success -
  which is exactly how the fault this guards against stayed invisible.
- **A setting that only exists once it is rendered.** The TLS override per deployment-component
  is stored on the deployment-component layer *and* has to come out as an annotation on one
  deployment's ingress and not the other's. The file proves where it is stored; only
  `kubectl get ingress` proves what it produced. The helper there degrades to a `skip` when
  kubectl is absent, so the file-level assertions still run.

The rule of thumb: assert against Forgejo for *what was stored*, against the task endpoint for
*how it ended*, and against the cluster for *what it rendered*. Reaching for a `sleep` in place
of any of the three is what makes a test green through a broken run.

### When the cluster object is still not the answer

`tests/e2e/test_sandbox_tls_override.py` (RC-96) goes one step further than the ingress
annotation above: for a *certificate*, neither the project file nor the ingress is the
proof -- the proof is the certificate a client is handed on the connection. It walks the
whole chain per deployment (file -> ingress -> `kubernetes.io/tls` secret -> a real TLS
handshake with SNI via `openssl s_client`) and compares a self-signed certificate against
the platform's Let's Encrypt wildcard.

The trap it encodes: **pick the port the ingress listens on**. On the dev server Caddy owns
443 and Kind publishes the ingress on 8843, and Caddy terminates TLS with that same
wildcard -- so a handshake on 443 reports the platform certificate for every host, and the
first run of that suite failed on a deployment whose file, ingress and secret were all
correct. The module probes the Kind ports first (`E2E_TLS_ENDPOINT` overrides).

### When only an outside caller can see the fault

`tests/e2e/test_sandbox_restore_van_buiten.py` walks the whole restore road the way the
zad-cli walks it: create a project with a database and a bucket, back it up, take the
reference name **out of the read endpoint**, and restore with it. Two rounds of fixes
(RC-81, RC-82) were unit-tested green while the road stayed impassable, because the
read side and the write side were each tested against their own idea of the name and
never against each other. The suite asserts the handover: the name a caller can read is
a name the restore route accepts, for both kinds, plus the failure classification
(`InvalidTarget`) that only appears once the restore gets far enough to reach the
destination gate.

## How to run

```bash
task test-e2e            # local in-process, no sandbox needed
task test-e2e-sandbox    # against the live sandbox (sets the env vars below)

# Direct (sandbox):
cd operations-manager/python
E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
E2E_SECRET_KEY=sandbox-dev-secret-key-fixed-for-stable-sessions-32min \
FORGEJO_URL=https://forgejo.sandbox.rijksapp.dev \
uv run pytest tests/e2e/test_sandbox_flows.py -m "e2e and sandbox" -v
```

Chromium is installed on demand by the Taskfile targets (`uv run playwright install chromium`).

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `E2E_BASE_URL` | (unset - sandbox tests skip) | Portal base URL. Set to the sandbox URL to run sandbox tests. |
| `E2E_SECRET_KEY` | `sandbox-e2e-test-secret-key-min32chars` | **Must** equal the sandbox's real `SECRET_KEY` (`sandbox-dev-secret-key-fixed-for-stable-sessions-32min`) so the pre-signed cookie validates. The Taskfile sets this. |
| `FORGEJO_URL` | `https://forgejo.sandbox.rijksapp.dev` | Forgejo host for verifying project files. |
| `FORGEJO_USER` / `FORGEJO_PASSWORD` | `rig-admin` / `admin1234` | Forgejo basic-auth. |
| `FORGEJO_PROJECTS_REPO` | `rig-admin/zad-projects` | Repo holding `projects/{name}.yaml`. |
| `FORGEJO_VERIFY_SSL` | `true` | Set `false` to skip TLS verification. |
| `E2E_ARTIFACT_DIR` | `tests/e2e/artifacts` | Where traces + event screenshots are written. |
| `E2E_TRACE` | `1` | Playwright tracing (screenshots of every action). Set `0` to disable. |

## Authentication (why there is no login code)

Tests authenticate with a **pre-signed Starlette session cookie** - the fixture signs a cookie for
an allowlisted user with the app's `SECRET_KEY` and injects it into the browser context. No
Keycloak round-trip, no login form. This is why tests never contain auth code: it lives once, in
the `auth_page` / `sandbox_page` fixtures.

Trade-off: sandbox mode depends on `E2E_SECRET_KEY` matching the running sandbox `SECRET_KEY`. A
future **real-login fixture** (admin/admin1234 against the Keycloak form, which the `sso-support`
realm shows) would remove that dependency and enable a dedicated test user - see "Adding a test
user" below.

## Screenshots and traces of events

Three layers, all under `E2E_ARTIFACT_DIR` (sandbox mode):

- **Per-test trace** (`trace-<test>.zip`): screenshot of every action.
  View: `uv run playwright show-trace tests/e2e/artifacts/trace-<test>.zip`.
- **Named event screenshots**: `capture(page, "label")` -> `<test>-<label>.png`.
- **On-failure screenshot** (`FAILED-<test>.png`): automatic when a sandbox test fails.

## Adding a test user (future)

Add a second Keycloak user to the sandbox realm config, add its email to `ALLOWED_EMAILS` in the
sandbox configmap, then either add a `SANDBOX_TEST_USER`-style constant and sign a cookie for it, or
implement the real-login fixture mentioned above.

## Known constraints on the current sandbox

- **Component-add requires current OPI code on the sandbox.** Older builds wrote `components[].path`
  as the string `"/"`, which the v2 schema rejects (`path` must be an array). Fix is in
  `opi/utils/project_utils.py` (`build_component_config` emits `[{"match": path}]`). A stale pod
  fails `test_add_component_via_api` with "components/N/path: '/' is not of type 'array'". Check
  `curl <sandbox>/version` to confirm the running build.
- **UI delete of an unhealthy project is slow.** The web delete is synchronous and force-less; it
  removes the project file only once ArgoCD teardown succeeds. The test uses
  `nginxinc/nginx-unprivileged:stable-alpine` (listens on 8080, non-root) so the pod is healthy and
  teardown is quick. Change `_RUNNABLE_IMAGE` in `test_sandbox_flows.py` if needed.

## See also

- `features/version-endpoint.md` - the `/version` endpoint used to confirm which build is running.
