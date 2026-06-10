# Umami Analytics as a Service

**Status**: Idea / evaluation (not committed)

## What It Is

A privacy-friendly, **cookieless** web-analytics capability provisioned as a ZAD
service — the same model ZAD already uses for MinIO, Keycloak and PostgreSQL. A project
opts in via `services:`, ZAD provisions a site in a shared [Umami](https://umami.is)
instance, injects the tracking snippet into the deployed app, and (optionally) renders
simple usage graphs inside the ZAD UI.

This document captures the evaluation so the team can decide whether to build it. It is
**not** an implementation commitment.

## Why

- A user (Ravi) asked whether we can see *how often* the "ai-verordening beslishulp" is
  used — basic usage counts, **not** navigation tracking through it.
- ZAD has **no analytics today**, so the honest answer was "we can't measure that."
- The need is generic: "is this thing being used at all", per project, with minimal
  privacy impact and no operational burden on the project teams.

## Why Umami

| Tool | License | Backend | Cookieless | Fit for ZAD |
|---|---|---|---|---|
| **Umami** | MIT | **PostgreSQL** | ✅ default | ⭐ Best fit |
| GoatCounter | EUPL/MIT | SQLite/Postgres | ✅ default | Great if you want *minimal* |
| Plausible CE | AGPL | Postgres + ClickHouse | ✅ default | Heavier (needs ClickHouse) |
| Matomo | GPL | MySQL/MariaDB | ⚠️ opt-in | Heavy, PHP, more than you need |

Umami wins because it is **MIT-licensed** (no AGPL copyleft), stores in **PostgreSQL**
(which ZAD already provisions and has a connector for — no new datastore type), is
cookieless by default, and exposes a **REST API** to both provision sites and pull
aggregate stats (so graphs can live in the ZAD UI). GoatCounter is the runner-up if the
bar is literally "count hits per page"; Plausible CE and Matomo are heavier than the
need warrants.

## Cookieless = No Consent Banner

Umami sets no cookies and reads/writes nothing on the device — unique visitors are
counted with a daily-rotating hash (IP + user-agent + salt), so no persistent
identifier exists. Consequences for a Dutch government context:

- **No cookie banner / consent flow** — the cookie rule (Tw art. 11.7a) only applies
  when you store/read info on the device. Cookieless analytics fall outside it.
- **No new PII to govern** — aggregate page-view counts with no individual identifier
  are about as low-impact as analytics gets under the AVG.

So there is **no cookie-consent service to build** — that is a feature, not a gap.

## Multi-Tenancy Model

One Umami instance holds **many** websites, and a user can be scoped to see **only**
their own site:

- Umami's data model: **Users → Teams → Websites**. Website visibility is team-based.
- Per project: one **Team** + one **Website** + a user with the **`team-view-only`**
  role → that user sees their project's site and nothing else.
- Scope is at the **team level** (you cannot scope a user to a single website
  directly), so the pattern is **one team per project**.

## Login / SSO

**Free self-hosted Umami has no native Keycloak/OIDC SSO** (upstream feature request
[umami#3163](https://github.com/umami-software/umami/issues/3163) is open with no ETA).
A subtlety: fronting Umami with oauth2-proxy and *disabling* Umami's own auth would gate
the instance but collapse per-site isolation (Umami would no longer know *which* user is
viewing, so it shows everything). **Per-site isolation therefore requires Umami's own
user accounts regardless of how login is fronted.**

**Recommended path: ZAD-provisioned local accounts.** ZAD calls the Umami API to create
a user + team + website per project and assigns `team-view-only`. Works today, all
first-party, no third-party code. Users authenticate with Umami credentials that ZAD
provisions/manages.

**Future option (not now): real Keycloak SSO** via the community
[`kibblewhite/umami-oidc`](https://github.com/kibblewhite/umami-oidc) fork. It maps a
Keycloak identity to a Umami user/team, but it is a small third-party fork — adopting it
needs a **supply-chain / security review** and a commitment to track upstream. Revisit
if upstream #3163 lands.

## Proposed ZAD Integration (sketch)

Follows the existing service pattern (cf. MinIO/Keycloak):

- **`opi/services/services_enums.py`** — add `ANALYTICS = "analytics"` to `ServiceType`.
- **`opi/services/services.py`** — add a `ServiceDefinition`: `name="Analytics"`,
  `scope="deployment"`, `requires=["services/postgresql-database"]`, variables
  `UMAMI_SCRIPT_URL` + `UMAMI_WEBSITE_ID` injected into the deployment.
- **`opi/connectors/umami.py`** — HTTP client: admin login → bearer token; create
  user / team / website; assign `team-view-only`; fetch
  `/api/websites/:id/sessions/stats`.
- **`opi/manager/analytics_manager.py`** — `create_resources_for_deployment()`, wired
  into `project_manager.py` alongside the existing minio/keycloak managers (~line 3877).
- **Tracking snippet** injected into the deployed app via the generated manifests; the
  ZAD UI calls `/api/websites/:id/sessions/stats` and renders the counts with the
  existing ROOS chart components.
- **Operational note:** Umami's self-hosted bearer tokens want **Redis** for
  persistence — provisioning a small Redis (or accepting session-only tokens) is part of
  standing up the shared instance.

Example project YAML once built:

```yaml
services:
  - analytics
```

## Open Questions

- **Shared instance vs per-project instance** — lean **shared** (one Umami + one DB,
  per-project Teams). Cookieless aggregate counts carry no PII, so the isolation
  argument for separate instances is weak, and shared is far simpler to operate (KISS).
- **Opt-in vs default-on** — does every ZAD app get analytics automatically, or only
  when `analytics` is in the project YAML?
- **Per-route vs per-app** — Ravi's ask ("is the beslishulp used") is per-route
  pageviews, which Umami gives for free. Anything beyond pageviews-per-path?

## References

- Umami API docs — https://umami.is/docs (`/api/...` endpoints)
- SSO feature request — https://github.com/umami-software/umami/issues/3163
- Community OIDC fork — https://github.com/kibblewhite/umami-oidc
