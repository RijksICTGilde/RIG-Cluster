# Upgrade-safety test — run report (2026-08-04, server-sandbox)

**This is the execution report for RC-22.** It runs the draaiboek in
`features/upgrade-safety-test.md` on `kind-rig-sandbox`: bring OPI up on the image
production runs today, provision the six converted production projects, upgrade OPI to
the release build **once**, re-migrate every project, and judge what changed.

## Verdict (one sentence)

**For the project files that could be brought into a real "existing" state in the
sandbox, the release migrates them safely: service identity is byte-for-byte identical
(every database, realm, client, bucket and published host unchanged), nothing is
removed from the generated manifests, and the project-file migration is lossless — but
two of the six projects (`wies`, `regel-k4c`) could not be provisioned even on the OLD
image, so their live migration could not be pre-tested here, and one pre-measured
expected difference (alias AGE-encryption) did not occur.**

## What ran where

- Cluster: `kind-rig-sandbox` (server, 192.168.1.101), driven directly via `kubectl`.
- OLD/baseline OPI: `ghcr.io/minbzk/base-images/operations-manager/operations-manager:2026.07.27.0941-9d9c0764-dirty` (commit `f9e58071`, branch `main`, 2026-07-22) — the tag the odcn overlay pins.
- NEW/release OPI: derived image `operations-manager:rc22-release-3d263f16` built from the task base branch HEAD `3d263f16` (release = `branches-samenvoegen-naar-main` merged). Built as a lightweight derived image FROM the last-built `rc17-7c052912` (dependencies verified unchanged: only coverage/marker config differs in `pyproject.toml`) to avoid the known full-build OOM of the Kind control-plane.
- Image swapped **once** (OLD → NEW), never back and forth.

## Baseline commits recorded (step 5)

| Repo | Baseline (OLD image) | After upgrade + migrate |
|---|---|---|
| zad-deployments | `263a4e67c0ab32bb3f09033c70ec3a8d46bc2f9b` | clean-4 `f16caf9`, HEAD (w/ regel-k4c partial) `6bcef8f` |
| zad-projects | `01e6dcf1da451d42abdfb5b2233953e8353e6d15` | `fe90cc6` |

## Layer 1 (offline replay) — PASSED, as supporting evidence

`RIG_PROJECTS_DIR=<rig-cluster-projects-github/projects> task test-upgrade-safety` →
**9 passed**. Every one of the **47 real production files** migrates through
`migrate_to_latest()` and passes the full validation chain the new code runs before any
write. No file silently fails the new read path (the dp-bn7-class risk). This is the
cheap, total-coverage layer and it is green.

## Per-project result (Layer 2)

| Project | Baseline provision (OLD) | Re-migrate (NEW) | Identity | Deploy-diff | Project-file migrate |
|---|---|---|---|---|---|
| `mozad-dle` | PASS | PASS | identical | no removals | v2.6, lossless |
| `dp-bn7` | PASS | PASS | identical | no removals | v2.6 + invite relocated, lossless |
| `amt-odc` | PASS | PASS | identical | no removals | v2.6, lossless |
| `openp-4pw` | PASS (after repair E) | PASS | identical | no removals | v2.6 + invite relocated, lossless |
| `wies` | **FAIL** (clone-order) | FAIL (same) | n/a | n/a | file migrated to v2.6 (lossless) |
| `regel-k4c` | **FAIL** (dotted domain) | FAIL (same) | n/a | n/a | file migrated to v2.6 (lossless) |

`wies` and `regel-k4c` fail **identically on the OLD image**, so their failures are not
upgrade regressions — they are environmental limits of replaying these two files on this
sandbox (details F/G/H below). Their **project files still migrated to v2.6 losslessly**
(the migration runs before provisioning), so they are covered by the project-file diff
even though they have no live deployment state.

## The three mechanical checks

### 1. Identity check (`upgrade-safety-identity`) — the headline gate — **PASS**

> Service identity is IDENTICAL across both sides (4 baseline / 4 upgraded deployments
> compared). Every database, realm, client, bucket and published host resolves to the
> same value.

Run over the clean-4 set (baseline `263a4e6` vs upgraded `f16caf9`). The generated
secrets are AGE-encrypted to each **project's own** key, so decryption used a keyring of
the sandbox cluster key **plus** each project's private key (decrypted from its file with
the cluster key). Exit 0 — no deployment resolves to a different database/realm/bucket/host
after the upgrade. This was the pre-declared pass/fail criterion and it passes.

### 2. Deployments removal diff (`upgrade-safety-diff` on zad-deployments) — **clean**

The raw diff reports 42 "removed" lines across amt-odc/dp-bn7/openp-4pw, but **every one
is inside a `*.sops.yaml` file** and is SOPS re-encryption churn (fresh IV/data-key per
encryption), all categorised `other`. Filtering the encrypted files out:

> No removals detected in the diff — nothing disappeared from zad-deployments.

Zero removals in plaintext manifests (no env var, ingress, mount or schema disappeared),
and check 1 already proved the resolved secret values are unchanged. Clean.

### 3. Project-file migration diff (`upgrade-safety-diff` on zad-projects) — **judged: lossless, with one missing expected diff and one unlisted (lossless) rewrite**

Expected differences observed:
1. **`schema-version` → 2.6** on all six files (from `2`/`2.2`). ✔
2. **Invites relocated** from top-level `invites:` into `services/invite/config` for
   `openp-4pw` and `dp-bn7` (all sub-fields — settings, active, realm_roles — carried
   across). ✔

Expected difference **NOT observed:**
3. **Alias AGE-encryption did NOT occur.** The plan expected two alias blocks in `wies`
   and one in `openp-4pw` to become AGE-encrypted on save. In this run the alias values
   stayed **plaintext service-variable references** (`$DATABASE_SERVER_HOST`,
   `postgresql://…$DATABASE_PASSWORD@…`). Pure `$`-references carry no literal secret, so
   OPI leaves them plaintext — which looks correct. Caveat: these aliases were **restored
   by hand** in this run (see repair E), so the pre-measured expectation may have been
   taken against different alias content. Flagged, not judged a regression.

Unlisted but **lossless** rewrite (beyond the plan's "exactly four kinds"):
4. **Uniform service-declaration normalization.** The release rewrites the services list
   and component service bindings from the old key-style (`- keycloak:` / `-
   persistent-storage:`) to the uniform record form (`- name: keycloak, config:` / `-
   reference: persistent-storage, config:`), relocates the realm admin creds from
   top-level `config.keycloak` into the nested keycloak service config, folds
   `publish-on-web`/`authorization-wall`/`domains` blocks into their uniform records, and
   **enriches** `publish-on-web` with the materialized domain-approval state. Judged **no
   data loss**: every relocated key reappears in the new structure and the **Keycloak
   realm admin password decrypts intact** after the move (verified for amt-odc). This is
   the RC-5 uniform-declaration migration surfacing because the OLD baseline image writes
   key-style; it is not in the plan's expected-four list but it removes nothing.

**No unaccounted removals** were found in any of the six project files after crediting
schema-bump, invite-relocation, uniform-normalization and SOPS/AGE churn.

## Problems hit this run, and how to prevent them next time

Ordered roughly as encountered. C, D and E were fixed by re-converting/​re-pushing (C, D by
the owner; E by hand this run); B and E-repair are the only sandbox-side repairs I made.

- **A. Pre-staged repo access.** `zad-upgrade-test-projects` returned 404 until access
  was granted. *Prevent:* grant the session read access before dispatch; verify with
  `git ls-remote`.
- **B. Baseline image vs newer configmap (sandbox repair, reported).** The OLD image
  crash-loops on `SLEEP_MODE_*` env vars (Settings `extra=forbid`) that postdate it. I
  stripped those three lines from `operations-manager-config` to boot the baseline and
  **restored them before the new-image swap** (backup kept). *Prevent:* the draaiboek
  should note that pinning an old baseline image needs a configmap without settings newer
  than that image.
- **C. AGE key mismatch.** The pre-staged files were encrypted to a sandbox key that is
  not the one this cluster holds (`sops-age-key`, `age1d0glu…`). Re-converted against this
  cluster's public key. *Prevent:* the conversion must target the live cluster's
  `sops-age-key` public key, not a repo `security/sandbox-key.txt`; verify one
  `age-private-key` block decrypts with the cluster key before staging.
- **D. API-key format.** First re-key minted `rma_…` keys the standard read script cannot
  scrape; re-pushed in OPI's native 32-char format. *Prevent:* mint api-keys in OPI's
  native format.
- **E. Aliases blanked to `SANDBOX-PLACEHOLDER` (sandbox repair, reported).** The
  conversion replaced **alias** values with a placeholder, but aliases are
  service-variable references (no secrets) and OPI requires ≥1 `$`-reference → validation
  fails on both images. I restored the exact references from the raw production files for
  `openp-4pw` (15) and `wies` (10). *Prevent:* the migrate script must sanitize only
  `user-env-vars`, never `aliases`.
- **F. Do not re-stage after OPI writes back.** Force-resetting project files after OPI
  persisted realm creds caused "admin user already exists / password unrecoverable"
  refusals and required manual Keycloak cleanup (master-realm admin users + project realms
  survived project delete). *Prevent:* stage once, let OPI persist, never overwrite; and
  check whether `delete_project_manager` fully removes the master-realm admin user.
- **G. `wies` clone-ordering (product finding).** 15 PR deployments `clone-from:
  {reference: staging}`, but `pr-274` is ordered before `staging` in the file; OPI
  processes deployments in file order and aborts on the first failure, so `staging` is
  never created and no clone can succeed. Production created these incrementally, so the
  source always pre-existed. *Prevent / fix:* topologically order deployments by
  `clone-from` before processing (real product improvement), or order clone sources first
  in the converted file.
- **H. `regel-k4c` dotted-domain (sandbox-config finding).** A deployment uses
  `domain-format: component.subdomain`, which the enforcer only allows when the base
  domain is configured dots-capable; `sandbox.rijksapp.dev` is not. Fails on both images.
  *Prevent:* give the sandbox domain a `supports-dots` allowed-domain config, or map
  dotted formats to a dot-free sandbox subdomain during conversion.

## What I could NOT do (honest gaps)

- **`wies` and `regel-k4c` were never provisioned** (baseline or upgrade) — G and H block
  them independent of the release, so their **live** migration (identity + deployment
  diff) was not pre-tested. Their **project-file** migration to v2.6 was exercised and is
  lossless.
- **Expected diff #3 (alias AGE-encryption) was not reproduced** — see check 3(3);
  aliases stayed plaintext references, and I restored them by hand, so this needs a clean
  re-measurement.
- **`regel-k4c` partially generated on the NEW image** (6 of its deployments wrote
  manifests before the dotted-domain deployment failed). Real risk worth noting: a project
  can partially regenerate and then abort. Its partial output was excluded from checks 1/2
  by comparing the clean-4 point `f16caf9`.
- **Live probe (`/status`) not used as a gate.** Pod health was mixed (mozad-dle, dp-bn7
  healthy; openp-4pw probe crash-looped; amt-odc had no workload pods) but that is the
  separate live-probe concern, not one of the three checks, which read the git-generated
  state that `refresh` (HTTP 200) had already committed.
- **`orch sandbox claim/release` is unavailable** on this host's `orch` build (no
  `sandbox` subcommand; `sandbox-deploy` mis-reports "busy"). I held the cluster as the
  sole in-progress task and drove it directly. No lock could be taken or released through
  orch.

## Cluster end-state and cleanup

- OPI is left on the release image `rc22-release-3d263f16`; the configmap is restored to
  its original content (sleep-mode lines back). The next PR's `sandbox-deploy` will
  redeploy over it.
- The 4 provisioned projects (`mozad-dle`, `dp-bn7`, `amt-odc`, `openp-4pw`) and the
  partial `regel-k4c` state are **left in place** for inspection; they were **not** deleted.
  Clean up with `scripts/sandbox_project_tool.py delete <project>` (runs the real
  teardown) plus a Keycloak master-realm admin-user sweep if a delete leaves one behind
  (see F).
