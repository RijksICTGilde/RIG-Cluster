# Merge

What must be true before a PR lands on `main`. `review.md` covers the branch; this covers the **merge result**, which is a different thing and the reason this file exists.

## Why this is separate from review

A green review says the branch is sound. It says nothing about the branch combined with everything that landed on `main` since the branch started. Two changes can each be correct and still be wrong together, and `git merge` will not notice when they touch different lines.

This is not hypothetical. On RC-211, `main` and the task branch had both added `set_connection_limit` to the same fake class in `tests/test_clone_attempt_flag.py`, on different lines:

- `git merge` reported no conflict;
- the full unit suite on the merge result gave 11538 passed, 0 failed;
- `uv run ruff check .` gave F811, "Redefinition of unused `set_connection_limit` from line 84".

The second definition silently shadowed the first, and the two behaved differently: one always returned `unchanged`, the other tracked the limit per user and distinguished `updated` from `unchanged`. Both branches were green on their own gates. The defect existed only in the combination, and nothing ran the combination.

Four times in one day two tasks solved the same thing independently. Three announced themselves with a hard conflict. This one did not. That is the dangerous shape, and the only place it is visible is the merge result.

## The gate

Build the merge result first — merge `main` into the branch, no rebase and no force-push — then run **on that result**:

```
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

All three clean. These take seconds and they are exactly what catches this class: a duplicate definition or an unused import does not survive a linter, however green the tests are.

Plus the tests for what the PR touched, as in `review.md`:

```
uv run pytest tests/<file> -x -q --tb=short
```

The full suite is **not** required here. It takes close to half an hour and the cluster-dependent integration and e2e tests need a live sandbox. If the merge introduces risk the fast gates cannot see — a changed lifecycle path, a project-file write — run the relevant sandbox test as `review.md` describes.

## When the gate is red

**Do not fix it as part of the merge.** A red gate on a clean merge means two changes are doing the same job in different ways, and choosing which one survives is a decision about behaviour. Whoever is merging is the wrong person to make it: they did not write either side and did not review either intent.

Instead:

1. `git merge --abort`
2. Put the gate output on the PR, plus which two changes collide and what each was trying to achieve. `git log main -- <file>` usually finds the other task.
3. State the options you can see, one line each, and ask for a decision.
4. Send it back for rework.

Say explicitly that both sides were green before the merge and red after. That is the evidence it is the combination and not the branch, and it saves the builder the search.

## When the base has not moved

If `main` is still on the exact commit the task branched from, nothing can have interfered: the branch's gates *are* the merge result's gates and the merge can go straight through. The orchestrator checks this and only routes a merge through a session when the base has actually moved.

## What a merge never includes

Nothing beyond resolving the merge itself. No tidied import, no typo fixed in passing, no better name. Every line changed outside a conflict resolution is a change nobody reviewed, arriving on `main` under a commit message that says "merge".
