# Ephemeral Database Console

On-request, auto-expiring web database client (pgweb or dbgate) for a single
deployment's PostgreSQL database. A project member opens it from the project
page, works for up to an hour, and OPI tears it down automatically. It is a
temporary, self-service alternative to port-forwarding or handing out database
credentials.

## What it is

When a member starts a console, OPI provisions a small bundle in the project's
namespace, **directly via kubectl (outside git/ArgoCD)**:

- a **Pod** running the chosen tool (pgweb or dbgate), fronted by the existing
  oauth2-proxy "auth wall" sidecar;
- a **Service** + **Ingress** on a temporary hostname `dbconsole-<id>.<base>`;
- a **Secret** (DB connection + OIDC client secret + cookie secret) and a
  **ConfigMap** holding the allowed-email list and the sign-in page;
- a dedicated **Keycloak OIDC client** in the ZAD realm.

Access control matches the platform model: Keycloak only authenticates (any ZAD
user can log in); the auth wall's `--authenticated-emails-file` restricts access
to the project's members (`users[].email`). In production the ingress also
inherits the VPN-only `ip_whitelist`, so the console is VPN-gated **and**
oauth2-gated **and** email-list-gated.

The console pod carries the **target deployment's `deployment` label**, so the
existing tenant NetworkPolicy already permits its egress to PostgreSQL; no new
network policy is created.

## How to use it

1. Open a project, select a deployment, scroll to the **Acties** section.
2. In the **Databaseconsole** card pick a mode and a tool, then **Console
   starten**:
   - **Modus**: *Alleen lezen* (read-only) or *Lezen en schrijven* (read-write).
   - **Tool**: *pgweb* (default, lightweight) or *dbgate* (richer UI).
3. Click **Console openen** to open the tool in a new tab and log in via Keycloak.
4. The console disappears automatically at expiry, or click **Nu stoppen**.

Only one console can be active per deployment at a time.

## Read-only vs read-write

- **Read-write** reuses the deployment's own database user (already scoped to its
  schema).
- **Read-only** provisions an ephemeral PostgreSQL role with `CONNECT` + `USAGE`
  + `SELECT` only (plus default privileges), dropped at teardown. This is the
  real boundary; the tool's own read-only flag is only a convenience hint.

## Lifecycle

- A background **reaper** (in the OPI lifespan) sweeps every
  `DB_CONSOLE_REAP_INTERVAL_SECONDS` and tears down expired bundles (Pod,
  Service, Ingress, Secret, ConfigMaps, the Keycloak client, and the read-only
  role). It reads all state from cluster labels/annotations, so it is
  restart-safe and also garbage-collects orphaned `dbconsole-*` OIDC clients.
- Each Pod additionally sets `activeDeadlineSeconds` to the TTL as a hard
  self-terminate backstop if the reaper is ever down.

## Configuration (`opi/core/config.py`)

| Setting | Default | Purpose |
|---|---|---|
| `DB_CONSOLE_ENABLED` | `true` | Master switch (gates the reaper and the routes). |
| `DB_CONSOLE_TTL_SECONDS` | `3600` | Session lifetime. |
| `DB_CONSOLE_REAP_INTERVAL_SECONDS` | `60` | Reaper sweep interval. |
| `DB_CONSOLE_PGWEB_IMAGE` | `sosedoff/pgweb:0.16.2` | pgweb image. |
| `DB_CONSOLE_DBGATE_IMAGE` | `dbgate/dbgate:6.6.1` | dbgate image. |

> **Production**: override the image settings with a mirror reachable on the
> cluster (e.g. `rcr.rijksapps.nl/...`); docker.io/ghcr are blocked on ODCN.

TLS uses the cluster's default/wildcard ingress certificate (the ingress emits
`tls: - {}`), so ephemeral hostnames do not issue per-host certificates.

## Dependencies / requirements

- The platform OIDC discovery URL (`OIDC_DISCOVERY_URL`) must be configured; the
  console authenticates against that (ZAD) realm.
- The target deployment must have a PostgreSQL database (a `<deployment>-database`
  secret). Namespace-dedicated databases use the `<project>-postgres-superuser`
  secret to provision the read-only role.
- The cluster's default ingress certificate must cover the app base domain.

## Key files

- `opi/manager/db_console_manager.py` - provisioning + teardown orchestration.
- `opi/core/db_console_reaper.py` - TTL reaper + orphan-client GC (lifespan).
- `opi/web/router_db_console.py` - member-gated start/status/stop routes.
- `manifests/db-console-pod.yaml.jinja`, `db-console-secret.yaml.jinja`,
  `db-console-emails-configmap.yaml.jinja` - the bundle.
- `manifests/sidecar-authorization-wall.yaml.jinja` - now switches to
  `--authenticated-emails-file` when an emails ConfigMap is provided (backward
  compatible: normal deployments keep `--email-domain=*`).
- `opi/connectors/postgres.py` - `grant_readonly_on_schema`.
- `opi/connectors/keycloak.py` - `delete_oidc_client`, `list_client_ids_by_prefix`.
- `opi/templates/project-details/_db-console-panel.html.j2` - the UI panel.
