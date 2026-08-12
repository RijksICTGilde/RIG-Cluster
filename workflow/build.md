- UI components must use the Lord of the Components tags (`<c-*>`, NLDD-thema) as much as possible; if a component seems to be missing, add it to the list in request_for_components.md with a detailed request for it so it can be built later. See `features/lotc-bouwlijn.md`.
- Python imports must always be at the top of the file, never inline or local. Use `ruff check --select I --fix` to sort and organize imports, then `ruff format .` to format.
- **Build fast, test locally.** Only run the tests for what you changed (`uv run pytest tests/<file> -x -q --tb=short`), never the full suite. The unit suite is ~7900 tests and the browser suite ~350; running either in full costs minutes per attempt and is not your job — the full run happens once before the merge to main. Pick the files that cover your change; if you cannot name them, that is the signal your change lacks a test, not that you should run everything.
- **Look at the screen for anything visual.** A green browser test does not prove a table renders as a table. Take a screenshot of what you built and judge it. This has been the cause of six reworks.
- **Never sleep for a fixed period when you can wait for the condition.** A task whose tests take three minutes has taken two hours, and the difference was waiting, not working. `sleep 300` costs five minutes even when the thing was ready in ten seconds, and it costs another five when it was not. Wait on the thing itself:
  - `until <check>; do sleep 5; done` returns the moment the condition holds. Poll a *local* command every 5-15s — it costs nothing. A remote API with rate limits: 30s.
  - Use the waits that already exist rather than building your own: `kubectl wait --for=condition=Ready pod/... --timeout=120s`, `kubectl rollout status deployment/... --timeout=120s`, `wait_for_task()` in the test helpers. They return on success instead of on the clock.
  - **A timeout is a safety net, not a waiting mechanism.** `--timeout=600` means "something is wrong if it takes this long", never "come back in ten minutes". If your wait only ever ends at the timeout, you are not waiting, you are guessing.
  - Waiting on something that reports progress? Read the progress, do not re-check on a timer. A rollout, a task and a pod all say when they are done.
- **Ask the thing that knows, not the clock.** Sleeping until something is "probably done" is guessing twice: about the time, and about the outcome. Every state you might sleep on has an owner that will tell you:
  - a task → the task endpoint (`wait_for_task()` in the test helpers wraps it, and returns the *outcome*, not just "finished");
  - a deployment's health and sync → ArgoCD, via `opi/services/argocd_overview.py` for a whole project in one query;
  - a pod → `kubectl wait` / `kubectl rollout status`;
  - a project's state → the API, which is also what the zad-cli talks to and will keep talking to.

  This is not only about speed. E2E wizard tests once checked the *file state* instead of asking the task how it ended, and stayed green through a broken create — a sleep long enough to "be safe" hides a failure exactly as well as it hides a delay.
