# Review

Checklist for reviewing a PR on this repo. These are strong expectations, not absolute gates: apply judgement, but if you skip one, say why in the review.

## Code quality

- UI components use Jinja ROOS components wherever possible — check `references/jinja_roos_copied.md`. A `<button>` where `<c-button>` belongs is a finding.
- No duplicated logic. Look for similarly named patterns elsewhere before accepting a new helper; prefer reuse (DRY).
- Methods live in the file/class where they belong (connectors for external calls, managers for orchestration, services for business logic, forms for wizard/edit behaviour).
- Python imports are at the top of the file, never inline or local. Verify with `ruff check --select I`.
- Post-dev validation passes: `ruff check . --fix`, `ruff format .`, `pyright`.

## Test coverage

The goal: a reviewer should be able to see that every behaviour the PR changes is exercised by a test. Match the test type to what changed.

- **API endpoints.** Any added or changed endpoint should have a test. The current surface is dynamic — read it from the OpenAPI spec of a running instance (`https://zad.sandbox.rijksapp.dev/openapi.json`, or `http://localhost:9595/openapi.json` under skaffold) rather than a hand-kept list. Cover the auth mode (per-project `X-API-Key`, admin/master key, or session), the success path, and at least the obvious failure (missing/invalid key, unknown project, validation error). Router/unit tests live at `tests/test_*_router.py`; cross-cutting API behaviour in `tests/integration/`.

- **Project-file changes.** If the change creates, updates, or deletes a project file, verify the *file*, not just the HTTP response. In unit tests, assert against the `save_and_commit_project` / `get_contents` path in `project_manager.py`. In sandbox E2E, read the resulting YAML back from the Forgejo `zad-projects` repo with `ForgejoClient` (the pattern in `tests/e2e/test_sandbox_flows.py`). A green 200 that did not actually write the file is the classic silent failure here.

- **UI and wizard flows.** Any change to the create wizard, the edit wizard, a modal mini-wizard (identity, team/member management, services/add-service, backup/restore, the "webadres wijzigen" domain edit), or the self-service portal should have a Playwright test in `tests/e2e/`. Use `WizardHelper` / `EditModalHelper`. Cover the happy path plus the validation/error rendering the change touches — modal-edit wizards have historically swallowed failures silently, so assert that errors are actually shown.

- **Lifecycle-affecting changes.** If the change alters how a project is provisioned, deployed, or torn down, prefer a live-sandbox test (`-m "e2e and sandbox"`) that drives it through the real UI/API and verifies the outcome against Forgejo and/or the cluster.

- **Run what you touched.** Only run tests for changed files (`uv run pytest tests/<file> -x -q --tb=short`); never the full suite blindly. Coverage floor is 90%.

## When something is missing

If a PR changes an endpoint, a project-file write, or a UI/wizard flow without a corresponding test, call it out and, where practical, add the test. New ROOS components that don't exist yet go into `request_for_components.md` with a detailed request rather than a raw `<button>` workaround.
