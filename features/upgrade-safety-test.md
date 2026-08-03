# Upgrade-safety test: do existing project files still work?

A repeatable test that answers the one question that matters to users when a
release changes how a project file is read: **do the existing project files still
pass, or does someone silently lose something?** It has two layers -- a cheap
offline one that covers every file, and a real upgrade on the sandbox over a
sample -- and a mechanical yardstick so "does it still work" is a readable diff,
not eyeballed screens.

- **Layer 1 (offline replay):** `operations-manager/python/tests/test_upgrade_safety_replay.py`
- **Layer 2 tooling:** the `--probe-image` flag on
  `operations-manager/python/scripts/migrate_project_to_sandbox.py` and the
  summarizer `operations-manager/python/scripts/compare_deployments_diff.py`
- **Tasks:** `task test-upgrade-safety`, `task upgrade-safety-diff`

## Why

Changes to how a file is read are a stealth risk. `dp-bn7` fell over on a schema
gap on every reprocess for months without anyone seeing it: the processing failed,
the user saw no error, and nothing changed anymore. This test exists to catch that
class -- a file that no longer passes the new validation and would stall silently
on its next reconcile.

## The key boundary (read this first)

The server does not have the production AGE key and must not get it. That is a
hard constraint that decides where each step runs:

- **Conversion is a local step.** `migrate_project_to_sandbox.py` decrypts
  `age-private-key` with the production key and re-encrypts it with the sandbox
  key. That only works on a machine that has `security/key.txt` -- your
  workstation. What goes to the server's Forgejo is only the converted output:
  encrypted with the sandbox key, with no production secret in it. The server
  never sees production secrets, not even encrypted.
- **Layer 1 needs no key at all.** Migration and validation work on the structure
  of the file; the AGE blocks are opaque strings that are never decrypted. So
  Layer 1 runs on the production files as they are, with no key, anywhere.

Roles, therefore: **convert locally by hand, run on the server with only sandbox
secrets.**

## Layer 1: offline replay over all project files (cheap, covers everything)

Takes each project file, runs it through `migrate_to_latest()` and then the exact
validation chain production runs before any write
(`validate_project_schema` -> `validate_project_structure`, which ends in the
per-service `validate_service_configs`), and reports per file. No cluster, no git,
no SOPS, no key. This is a CI test, so the next schema change runs into it
automatically.

Important: it validates the **migrated** data, not the raw file -- production
migrates in memory first and validates after, so validating the raw file would
measure something else.

```bash
# Always available: replays the committed sanitized fixtures.
task test-upgrade-safety

# Bites on the real files: point it at a zad-projects checkout.
RIG_PROJECTS_DIR=/path/to/zad-projects/projects task test-upgrade-safety
```

Without `RIG_PROJECTS_DIR` the real-files test skips with a clear message and the
fixtures still run everywhere. A failure is a real finding about the reading code
(a file that no longer validates), not a broken test.

## Layer 2: a real upgrade on the sandbox, over a sample

There is no room to roll out every project, so this is deliberately a sample:
**wies, regelrecht, moza and amt** -- enough service variety to hit the
interesting paths, small enough to see the turnaround. (At least one project that
uses invites is worth including, since those are relocated from a top-level
`invites:` block; swap one sample or add a fifth if none of the four is one.)

Switch the OPI image **once**, not back and forth per project:

1. Sandbox on the server, OPI on the image production runs now (pinned, not
   `latest` -- see "Which old OPI image" below).
2. Convert and set up the sample projects. Let everything provision until all apps
   are healthy.
3. Record the generated state: the zad-deployments repo commit at this point is
   the baseline.
4. Swap the OPI image to the new build. Once.
5. Reopen and refresh each project so everything is regenerated.
6. Compare.

To repeat the test, rebuild the environment clean rather than switching the image
back (old OPI cannot read a newer schema); a clean second setup is what you want
for a fair second measurement anyway.

### Converting the sample projects (local)

Swap every component workload for the e2e-allservices probe so `/status` verifies
each binding, and move the ports so the probe (which listens on 8080) is reachable:

```bash
cd operations-manager/python
uv run python scripts/migrate_project_to_sandbox.py wies regelrecht moza amt \
    --probe-image --output-dir /tmp/sandbox-projects
```

`--probe-image` is opt-in: without it the script does a plain migration and keeps
the original images and ports. Push the converted files in `/tmp/sandbox-projects`
to the sandbox Forgejo `zad-projects` repo.

## How "still works" becomes mechanical

Two complementary checks.

**The diff (what disappeared).** The zad-deployments repo holds everything OPI
generates for a project: manifests, secrets, configmaps, RBAC, network policies.
Record the commit after step 3, then after step 5 diff against it. Every removed
env var, secret key, ingress, mount or schema shows up as a removed line. The
summarizer turns that into a per-project list:

```bash
# Record the baseline BEFORE the image swap (step 3):
git -C /path/to/zad-deployments rev-parse HEAD

# After the upgrade + refresh (step 6):
REPO=/path/to/zad-deployments BASELINE=<sha> task upgrade-safety-diff
```

**The live probe (what a diff cannot see).** Whether database grants and
`search_path`, Keycloak realms/clients/roles, and buckets/policies still hold, and
whether ArgoCD syncs everything healthy, is not visible in a diff. That is what the
e2e-allservices probe is for: it binds every service and reports per service on
`/status` whether it round-trips.

A difference is not automatically a bug. This release changes some things on
purpose (the one-off migration to v2.6). The outcome is therefore a **judged
diff**: every difference is either explained and wanted, or it is a bug.

## Decisions taken (were open in the plan)

1. **Layer 1 source:** both. Committed sanitized fixtures always run (CI coverage +
   regression guard); real files via `RIG_PROJECTS_DIR`, skipped with a message
   when absent. No production files are committed.
2. **Outcome on a difference:** Layer 1 fails (a file that no longer validates is a
   regression). The Layer 2 diff is a report to judge, because wanted differences
   exist.
3. **Project workloads:** replaced entirely by the probe (`--probe-image`). The
   target is the project file and the service delivery, not the users' apps.
4. **Which old OPI image:** a pinned image of what production runs now. Take the
   CalVer tag from the odcn overlay (`bootstrap/rig-system/.../overlays/odcn*`) or
   the commit production is on -- pin it, never `latest`.
5. **After the run:** clean up via `scripts/sandbox_project_tool.py delete
   <project>`, which runs the real OPI teardown and so exercises the delete path
   too. Or leave them for a next round.

## Out of scope

Getting the projects' own applications running, performance measurements, and the
production conversion itself. This test proves the release is safe for existing
files; rolling it out is a separate decision.
