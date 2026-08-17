# Plan: kube-secrets-to-env helper

Status: **planned, not built.** Low priority (we don't expect to use it often). This document
captures the plan so a future session can build it consistently.

## Problem

Most credentials we need for ad-hoc operational tooling (Keycloak admin password, MinIO/Postgres
admin, Grafana token) live in Kubernetes secrets in the cluster. When we run the Python tools in
`operations-manager/python/scripts/` (e.g. `keycloak_flow_tool.py`, which reads
`os.environ["KEYCLOAK_ADMIN_PASSWORD"]`), we currently either paste the password into the terminal
(so it ends up in the shell history *and* in an assistant conversation) or hand-fetch it with
`kubectl ... | base64 -d`, which prints the value. Both leak secrets.

Goal: a small helper that loads selected secrets from a k8s namespace into the **current shell
session** as env vars, so the tools read them from `os.environ` and the values never appear in
terminal output or a chat.

## Approach (chosen)

A shell helper in `operations-manager/python/scripts/` that **emits `export` lines which fetch the
secret themselves**, loaded with `eval`:

```bash
# scripts/kube-secrets-env.sh  (prints export lines; does NOT print secret values)
eval "$(scripts/kube-secrets-env.sh)"                 # prod default (rig-prd-operations)
eval "$(scripts/kube-secrets-env.sh rig-system)"      # sandbox
```

Each emitted line looks like:

```
export KEYCLOAK_ADMIN_PASSWORD="$(kubectl -n rig-prd-operations get secret keycloak-admin-credentials -o jsonpath='{.data.KEYCLOAK_ADMIN_PASSWORD}' | base64 --decode)"
```

Why this shape:
- The script's **stdout contains only kubectl recipes, no secret values**; `eval` runs them, so the
  decoded value only ever lands in the shell env, never on stdout.
- **Nothing is written to disk** (no `.env.secrets` file to forget about).
- **Temporary**: the env lives only for the shell session — gone when it closes.
- Env-var names match what the tools already read (`KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_URL`,
  `KEYCLOAK_ADMIN_USER`, `GRAFANA_TOKEN`, ...), so no tool changes are needed.

### Secret mapping

A small table inside the script (env var → secret name → key), grouped so a caller can pick a set:

| Env var | Namespace secret | Key |
|---|---|---|
| `KEYCLOAK_ADMIN_USER` | `keycloak-admin-credentials` | `KEYCLOAK_ADMIN` |
| `KEYCLOAK_ADMIN_PASSWORD` | `keycloak-admin-credentials` | `KEYCLOAK_ADMIN_PASSWORD` |
| `KEYCLOAK_URL` | (static) | `https://keycloak.rijksapp.nl` |
| `MINIO_ROOT_PASSWORD` | `minio-admin-credentials` | `...` |
| `GRAFANA_TOKEN` | (from `.env.odcn-production.secrets`, not a k8s secret) | — |

Namespace defaults to `rig-prd-operations`; pass `rig-system` for sandbox. Keep the list short and
extend on demand rather than dumping every secret.

## Alternatives considered

- **Taskfile task** (`eval "$(task secrets:env)"`): works, but adds a task for something rare and
  the user preferred a scripts-folder helper. Rejected.
- **Git-ignored `.env.secrets` file** (already a supported pattern: `operations-manager/python/.env.secrets`
  is git-ignored) that tools `source`. Simpler, but writes secrets to disk. Keep as a fallback for
  people who prefer a persistent file.
- **direnv `.envrc`**: nicest auto-load UX but needs direnv installed and an extra gitignore entry.

## Security notes

- The script must **never `echo`/print a decoded value** — only the `export ...="$(kubectl ...)"`
  recipe lines.
- Materialising credentials can trip the assistant's auto-mode safety classifier; running the
  helper may need an explicit permission rule (e.g. allow `scripts/kube-secrets-env.sh`).
- This helper does not fix already-leaked secrets. Any password pasted into a terminal/chat must be
  rotated (and the corresponding k8s secret updated) regardless.
- Requires read access to the namespace's secrets (`kubectl get secret`), so it is an operator tool,
  not something to wire into runtime code.

## Out of scope

- No writing to disk, no persistent credential store, no changes to the Python tools (they already
  read `os.environ`).

## Build checklist (when we do it)

1. `operations-manager/python/scripts/kube-secrets-env.sh` with the mapping + namespace arg,
   emitting `export` recipe lines, `set -euo pipefail`, usage in a header comment.
2. Add a row to `operations-manager/python/scripts/README.md` and mention the
   `eval "$(...)"` usage.
3. Verify: `eval "$(scripts/kube-secrets-env.sh)"` sets `KEYCLOAK_ADMIN_PASSWORD` without printing
   it, then `keycloak_flow_tool.py inspect <realm>` works with no password on the command line.
