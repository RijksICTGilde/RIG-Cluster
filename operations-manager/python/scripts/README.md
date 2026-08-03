# Operations Manager utility scripts

Standalone operational tools that use the `opi` package and its dependencies (python-keycloak,
grafana clients, etc.). **Run them from `operations-manager/python`** so `opi` and the venv are on
the path:

```bash
cd operations-manager/python
uv run python scripts/<tool>.py [args]
```

Most tools talk to live infrastructure and read credentials/config from environment variables or
`opi.core.config.settings`. Check each script's module docstring for its exact inputs.

> Note: there is a second, separate scripts folder at the repo root (`/scripts`) with a few
> **shell** utilities (`pod-resources.sh`, `resourcequota-compare.sh`, `traffic-generator.sh`).
> The **Python** tooling lives here.

## Keycloak

| Tool | What it does |
|---|---|
| `keycloak_flow_tool.py` | Inspect/rebuild the ZAD **auto-link first-broker-login flow** on a live realm (`inspect <realm>`, `rebuild <realm> [--confirm-link]`, `inspect-all`). Rebuilds with explicit execution priorities so `idp-create-user-if-unique` precedes the handle-existing subflow (see `features/keycloak-auto-link.md`). Needs `KEYCLOAK_ADMIN_PASSWORD` (optional `KEYCLOAK_URL`, `KEYCLOAK_ADMIN_USER`). |
| `setup_keycloak_client_scope.py` | Set up custom client scopes (organization-attribute passthrough) for Keycloak clients. |
| `keycloak_self_service_report.py` | Pre-flight report before disabling the `UPDATE_PASSWORD` required action: per realm, users with that action still pending (they would get stuck at login, blocking, non-zero exit) and federated users who already have a password credential (they keep the SSO bypass, cleanup). Report only. Needs `KEYCLOAK_ADMIN_PASSWORD`; see `features/futures/keycloak-sso-bypass-voorkomen.md`. |

## Grafana / observability

| Tool | What it does |
|---|---|
| `grafana_loki_logs.py` | Query OPI production logs from Grafana Loki (`kubectl logs` only keeps ~3h). Token in `.env.odcn-production.secrets`. |
| `grafana_prometheus_test.py` | Test Grafana-proxied Prometheus queries. |
| `grafana_auth_test.py` | Test Grafana OAuth authentication. |
| `log_watch/watch.py` | Periodic OPI production-log triage (standalone verbose CLI); see `log_watch/config.example.py`. |

## Diagnostics

| Tool | What it does |
|---|---|
| `argo_diagnostics.py` | ArgoCD diagnostics. |
| `diagnose_oom.py` | OOM-kill diagnostics for tuning/analysis. |

## Project-file maintenance

| Tool | What it does |
|---|---|
| `add_domain_approvals.py` | Add `domains` configuration blocks to project files. |
| `fix_shared_service_revisions.py` | Fix shared-service revisions caused by a YAML anchor/alias bug. |
| `migrate_project_to_sandbox.py` | Migrate production project files to the sandbox cluster. `--probe-image` additionally swaps every component workload for the e2e-allservices probe and moves the inbound port to 8080 (RC-19 upgrade-safety test). |
| `compare_deployments_diff.py` | Summarize a zad-deployments `git diff` into disappeared keys (env vars, secrets, ingress, mounts, schemas) per project, for the RC-19 upgrade-safety test (`features/upgrade-safety-test.md`). Reads a ref-pair via git or a diff via `--stdin`. |

## Infra / DNS

| Tool | What it does |
|---|---|
| `transip_delete_dns.py` | Delete TransIP DNS records (the TransIP API key is IP-whitelisted; calls must originate from an allowed IP). |
