# E2E Test Isolation

## What It Is

The rule that every local E2E test starts from the same known state, whatever ran before
it and whatever runs beside it. Two mechanisms, both in `tests/e2e/conftest.py`:

1. **`own_project`** - a test file gets its own private copy of a fixture project, under a
   name derived from the file (`test_edit_wizard.py` -> `e2e-edit-wizard`).
2. **`_reset_projects_after_test`** - an autouse fixture that puts the in-memory project
   registry back to the fixture state after every local E2E test.

## Why It Is Needed

Everything expensive in the E2E harness is session-scoped: the FastAPI app, the browser,
the signed session. The in-memory project registry lives in that app - and it is also the
project store's write-through cache, so *every* project-file write a test makes (an edited
description, a flipped backup schedule, a project created through the wizard) is still
there for the next test.

Five test files used to share one seeded project, `test-project-detail`. That is the recipe
for "passes alone, fails in company": one file changes the project, the next file asserts on
what it expected to find. It is also what makes running tests in parallel impossible.

## How To Use It

Take the `own_project` fixture and use the name it yields. Say which fixture file to copy
with a module-level `PROJECT_TEMPLATE` (default: `test-project-detail`):

```python
pytestmark = pytest.mark.e2e

#: Which fixture project the own_project fixture copies for this file.
PROJECT_TEMPLATE = "test-project-detail"


def test_detail_page_renders(app_server: str, auth_page: Page, own_project: str) -> None:
    auth_page.goto(f"{app_server}/projects/details/{own_project}")
    ...
```

Do **not** hardcode `test-project-detail` (or any other seeded name) in a test that changes
project data. Read-only use of a seeded project is fine - `test_wizard_cross_domain_policy.py`
uses `test-project` as a peer it only ever selects.

The reset is not something you opt into: it runs for every test that uses `app_server`. A
test that creates a project and forgets to remove it is therefore harmless.

Sandbox tests are untouched by both: they talk to a real cluster and own their teardown in
a module fixture's `finally` (`sandbox_api.delete_project_via_api`).

## Configuration

| Piece | Where | Default |
|---|---|---|
| `PROJECT_TEMPLATE` | module-level constant in the test file | `test-project-detail` |
| Fixture project files | `tests/e2e/fixtures/projects/*.yaml` | seeded on app start |
| Copy/reset helpers | `tests/e2e/testserver.py` | `register_project_copy`, `reset_seeded_projects` |

`rename_project` moves the name everywhere it is repeated (the deployment namespace, a
repository's `project_name`), so a copy does not keep pointing at the original.

## Verifying It

The suite must be green in more than one order - that is the only way to know the coupling
is gone rather than moved:

```bash
task test-e2e                                  # normal order
task test-e2e-parallel                         # 2 workers, per-file distribution
task test-e2e-parallel WORKERS=4               # more workers on a bigger machine

# reverse file order
uv run pytest $(ls -1 tests/e2e/test_*.py | grep -v sandbox | sort -r) \
  -m "e2e and not sandbox" -q --timeout=300
```

`tests/e2e/test_project_isolation.py` guards both mechanisms directly: one test dirties the
state on purpose, the next asserts it came back clean.

## Dependencies

- `pytest-xdist` (test group) for the parallel form.
- No changes to application code: all of this lives in the test harness.
