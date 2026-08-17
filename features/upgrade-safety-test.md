# Upgrade-safety test: do existing project files still work?

A repeatable test that answers the one question that matters to users when a
release changes how a project file is read: **do the existing project files still
pass, or does someone silently lose something?** It has two layers -- a cheap
offline one that covers every file, and a real upgrade on the sandbox over a
sample -- and a mechanical yardstick so "does it still work" is a readable diff,
not eyeballed screens.

- **Layer 1 (offline replay):** `operations-manager/python/tests/test_upgrade_safety_replay.py`
- **Layer 2 tooling:** the `--probe-image` flag on
  `operations-manager/python/scripts/migrate_project_to_sandbox.py`, the removal
  summarizer `operations-manager/python/scripts/compare_deployments_diff.py`, and the
  identity check `operations-manager/python/scripts/compare_service_identity.py`
- **Tasks:** `task test-upgrade-safety`, `task upgrade-safety-diff`,
  `task upgrade-safety-identity`

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

There is no room to roll out every project, so this is deliberately a sample. It was
composed on 3 August 2026 by scanning all 47 production files on services, deployment
and component count, and the presence of invites. **Six projects:**

| Project | Dep | Cmp | Why |
|---|---|---|---|
| `wies` | 18 | 3 | Far the most deployments; tests scale and the per-deployment paths. |
| `regel-k4c` | 6 | 9 | The most components, plus `metrics-scraper`. |
| `amt-odc` | 1 | 1 | Six services in a small project: `minio-storage`, `persistent-storage`, `temp-storage`. |
| `mozad-dle` | 1 | 1 | The bare case: only `publish-on-web`. A project without services must survive too. |
| `openp-4pw` | 1 | 1 | Carries `invite` and `redis`, otherwise absent; one deployment, so cheap. |
| `dp-bn7` | 1 | 1 | Carries `invite` and `authorization-wall`, and is the project that stalled silently on a schema gap for months. The one file that most has to pass. |

Together they cover ten of the fifteen services: `keycloak`, `postgresql-database`,
`publish-on-web`, `persistent-storage`, `temp-storage`, `minio-storage`,
`metrics-scraper`, `invite`, `redis`, `authorization-wall`. Not covered because they
appear in no production file (newer than the data): `sleep-mode`, `health-check`,
`cross-domain-access`, `resource-tuning`. If room remains, the cheapest carriers of the
gaps are `tva-d62` (`attachments`) and `algor-odc` (`namespace-postgresql-database`, the
path RC-17's `scope: project` now makes equivalent -- the most interesting, but it costs
its own CNPG cluster).

Switch the OPI image **once**, not back and forth per project. Claim the shared sandbox
first (`orch sandbox claim`) and release it at the end, also on failure:

1. `orch sandbox claim` -- the sandbox is a single shared cluster; hold it for the run.
2. Sandbox on the server, OPI on the image production runs now (pinned, not
   `latest` -- see "Which old OPI image" below).
3. Set up the sample projects (they are already converted and waiting, see "Pre-staged
   on 3 August"). Let everything provision until all apps are healthy.
4. Record the generated state: the zad-deployments repo commit at this point is the
   baseline. Also record the zad-projects commit (the refresh in step 6 rewrites those
   files, and that is where the migration lands -- diff them too, see below).
5. Swap the OPI image to the new build. Once.
6. Reopen and refresh each project so everything is regenerated.
7. Compare (removals, identity, project files).
8. `orch sandbox release` -- free the sandbox for the next PR, even if a step failed.

To repeat the test, rebuild the environment clean rather than switching the image
back (old OPI cannot read a newer schema); a clean second setup is what you want
for a fair second measurement anyway.

### Pre-staged on 3 August

The six projects are already converted and waiting in
`https://git.claude.robbertuittenbroek.nl/robbert/zad-upgrade-test-projects.git`
(private; a session with the ordinary Forgejo credentials can reach it, the server
itself cannot). Its README states exactly what was converted and what was deliberately
removed.

The old-OPI baseline image is
`ghcr.io/minbzk/base-images/operations-manager/operations-manager:2026.07.27.0941-9d9c0764-dirty`
-- exactly what the odcn overlay pins today, and present on ghcr so the sandbox can pull
it. Note the server-sandbox may sit on a newer branch and has to go back to this first.

### Converting the sample projects (local)

Swap every component workload for the e2e-allservices probe so `/status` verifies
each binding, and move the ports so the probe (which listens on 8080) is reachable:

```bash
cd operations-manager/python
uv run python scripts/migrate_project_to_sandbox.py \
    wies regel-k4c amt-odc mozad-dle openp-4pw dp-bn7 \
    --probe-image --output-dir /tmp/sandbox-projects
```

`--probe-image` is opt-in: without it the script does a plain migration and keeps
the original images and ports. Push the converted files in `/tmp/sandbox-projects`
to the sandbox Forgejo `zad-projects` repo. (For the standard run the projects are
already staged, see "Pre-staged on 3 August"; you only re-run this when reconverting.)

## How "still works" becomes mechanical

Four complementary checks: what disappeared, whether a service is still the same
thing, whether the project files themselves stayed whole, and whether it all still
round-trips live.

**1. The removal diff (what disappeared).** The zad-deployments repo holds everything
OPI generates for a project: manifests, secrets, configmaps, RBAC, network policies.
Record the commit after step 4, then after step 6 diff against it. Every removed
env var, secret key, ingress, mount or schema shows up as a removed line. The
summarizer turns that into a per-project list:

```bash
# Record the baseline BEFORE the image swap (step 4):
git -C /path/to/zad-deployments rev-parse HEAD

# After the upgrade + refresh (step 7):
REPO=/path/to/zad-deployments BASELINE=<sha> task upgrade-safety-diff
```

**2. The identity check (is a service still the SAME thing).** The removal diff proves
nothing vanished, but it identifies a line by its key and ignores value changes on
purpose -- so a deployment that after the upgrade connects cleanly to a *different*
database passes it silently. That is the worst case, so it has its own check. It
decrypts the generated secrets on both sides (SOPS+AGE, offline, needs the sandbox
key) and asserts that each deployment's database host/name/user/schema, OIDC
url/realm/client-id, bucket name and published Ingress host are unchanged. This one is
pass/fail, not a judged report -- a difference here is a finding:

```bash
# A baseline worktree at the pre-upgrade commit, compared to the upgraded tree:
git -C /path/to/zad-deployments worktree add /tmp/zad-baseline <baseline-sha>
BASELINE_DIR=/tmp/zad-baseline TARGET_DIR=/path/to/zad-deployments \
    KEY_FILE=../../security/sandbox-key.txt task upgrade-safety-identity
```

**3. The project-file diff (the migration itself).** The refresh with the new OPI
rewrites the project files in zad-projects, and that is exactly where the migration
lands -- a key that disappears there weighs more than in a manifest, because a manifest
can be regenerated. Record the zad-projects commit before the swap and summarize the
same way (the summarizer reads any git diff):

```bash
# Before the swap: git -C /path/to/zad-projects rev-parse HEAD
REPO=/path/to/zad-projects BASELINE=<sha> task upgrade-safety-diff
```

**4. The live probe (what a diff cannot see).** Whether database grants and
`search_path`, Keycloak realms/clients/roles, and buckets/policies still hold, and
whether ArgoCD syncs everything healthy, is not visible in a diff. That is what the
e2e-allservices probe is for: it binds every service and reports per service on
`/status` whether it round-trips.

A removal or project-file difference is not automatically a bug. This release changes
some things on purpose, so those two are **judged diffs**: every difference is either
explained and wanted, or it is a bug. The identity check (2) is the exception -- it
must be clean.

### The differences measured in advance

The outcome is not "byte-identical" -- this release changes things deliberately. For
the six-project sample those were measured up front so they are fixed rather than
explained away afterwards. Exactly four kinds, in the project-file diff:

1. `schema-version` goes to 2.6.
2. `openp-4pw` and `dp-bn7`: the invites move from a top-level `invites:` block into
   `services/invite/config` (why both were chosen).
3. ~~Alias values get AGE-encrypted on the next save: two blocks in `wies`, one in
   `openp-4pw`.~~ **Disproven in ronde 2 (2026-08-04).** Measured clean, with the aliases
   arriving unaltered from production: 0 of 15 encrypted in `openp-4pw` and 0 of 10 in
   `wies`, before and after, with the alias content identical on both sides. These aliases
   are pure `$`-references (`$DATABASE_SERVER_HOST` and the like) that carry no literal
   secret, so OPI leaves them plaintext -- which is correct. Ronde 1 could not settle this
   because its aliases had been repaired by hand.
4. The uniform service-declaration normalization: `- keycloak:` / `- persistent-storage:`
   become uniform records, and `config.keycloak` moves into the nested keycloak service
   config. Judged lossless in both rounds.
5. For the two invite carriers, the invite keys are renamed from snake_case to kebab-case
   and `settings` is flattened: `default_language` -> `default-language` (up out of
   `settings`), `realm_roles` -> `realm-roles`, `application_url` -> `application-url`,
   `contact_email` -> `contact-email`, `success_title` -> `success-title`,
   `success_button` -> `success-button`. All values carry across. A naive key comparison
   reports this as 11 lost keys; that is a measurement artefact, not loss.
6. Only when `KEYCLOAK_ENFORCE_ADMIN_OTP=true`: a `totp_secret` is added per realm entry.
7. Nothing else. None of the six uses an uppercase reference (so the new
   `user-env-vars` interpolation changes nothing here), and none has a duplicate
   service entry (so that repair does nothing here either).

Anything outside this list, and any identity-check (2) difference at all, is a finding.

### Environment traps this test walks into

Learned the hard way in ronde 1 and 2; check these before starting.

- **Strip settings the old image does not know, and put them ALL back before the swap.**
  Settings is `extra=forbid`, so the pinned old image crash-loops on anything newer than
  itself -- `SLEEP_MODE_*` and `KEYCLOAK_ENFORCE_ADMIN_OTP`. Take the backup from git, not
  from the live configmap: the live one may already be missing lines a previous round
  stripped, and then the new side silently runs without them.
- **The conversion must carry `clone-from.status` and the service revisions.** Without them
  an existing project becomes a fresh project, and a `mode: once` clone that production
  finished long ago is retried -- against a source that may no longer exist. Use
  `--as-existing-project`.
- **The conversion must replace the resource profile, not just the workload.** With
  production requests, the six projects ask ~19 Gi on a ~16 Gi node while the probe uses
  almost nothing, and the OPI pod itself stops being schedulable. Since deleting runs
  through OPI, that is a deadlock. Use the probe profile (32Mi/10m request).
- **Set the OPI deployment strategy to `Recreate` for the image swap**, or a full node
  leaves the new pod `Pending` forever next to the old one.
- **`sops` must be on the machine** -- the identity check shells out to `sops --decrypt`.
- **Compare the SOPS files decrypted.** The raw diff is dominated by re-encryption churn
  (fresh IV per encryption). Decrypting both sides turns "probably just churn" into a real
  statement about what changed.

## Decisions taken (were open in the plan)

1. **Layer 1 source:** both. Committed sanitized fixtures always run (CI coverage +
   regression guard); real files via `RIG_PROJECTS_DIR`, skipped with a message
   when absent. No production files are committed.
2. **Outcome on a difference:** Layer 1 fails (a file that no longer validates is a
   regression). The Layer 2 removal and project-file diffs are reports to judge,
   because wanted differences exist; the Layer 2 identity check is pass/fail (a
   service that resolves to a different thing is always a finding).
3. **Project workloads:** replaced entirely by the probe (`--probe-image`). The
   target is the project file and the service delivery, not the users' apps.
4. **Which old OPI image:** a pinned image of what production runs now. Take the
   CalVer tag from the odcn overlay (`bootstrap/rig-system/.../overlays/odcn*`) or
   the commit production is on -- pin it, never `latest`.
5. **After the run:** clean up via `scripts/sandbox_project_tool.py delete
   <project>`, which runs the real OPI teardown and so exercises the delete path
   too. Or leave them for a next round. Either way `orch sandbox release` frees the
   shared cluster (also on failure).
6. **Invite carriers included:** yes, two (`openp-4pw`, `dp-bn7`). The migration
   relocates top-level `invites:` into `services/invite/config` for four production
   projects and cleans up stale data for four others -- a real rewrite of existing
   files, so exactly the path to guard. Both carriers are one-deployment/one-component,
   so they cost almost nothing.

## Out of scope

Getting the projects' own applications running, performance measurements, and the
production conversion itself. This test proves the release is safe for existing
files; rolling it out is a separate decision.
