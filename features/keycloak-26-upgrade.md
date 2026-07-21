# Keycloak 26 Upgrade (25.0.6 to 26.7.0)

Migration plan for upgrading the shared Keycloak from 25.0.6 to 26.7.0. Written 2026-07-20;
version numbers refer to that date (26.7.0 is the only Keycloak version receiving security
fixes, 25.0 has been out of support since October 2024).

## Why

- Keycloak only maintains the latest minor release. We are roughly two years of security
  fixes behind.
- 26.x brings persistent user sessions by default (sessions survive pod restarts, which we
  hit regularly through apiserver-hiccup liveness kills).
- 26.x properly supports admin event expiration, which we need for the audit-event
  retention introduced after the user-impersonation post-mortem
  (`docs/post-mortems/user-impersonation-oidc-email-claim.md`).

## Current state

| Piece | Where | Now |
|---|---|---|
| Image pin (all environments) | `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml` | `quay.io/keycloak/keycloak:25.0.6` |
| Custom provider JAR | built from `keycloak-migration/custom-mapper/`, fetched by init container from GitHub release | `keycloak-saml-nameid-mapper-1.1.0.jar`, pom pins `keycloak.version` 25.0.6 |
| Login theme | init container, MinBZK release | `keycloak-nl-design-system.jar` v1.4.2 |
| OPI admin client | `operations-manager/python` | `python-keycloak>=5.3.0`, compatible with 26.x, no change needed |
| Database | CNPG `rig-db`, database `keycloak` | no backups configured (relevant for rollback) |

The custom JAR ships four SPIs, all of which must survive the upgrade:

1. `saml-unrestricted-xpath-idp-mapper` (SSO-Rijk NameID extraction; login breaks without it)
2. `always-clear-session-logout` endpoint (eager logout shim towards BZK)
3. `RequireClientRoleAuthenticator` (client access restriction feature)
4. `rig-metrics-listener` + `/rig-metrics` endpoint (Prometheus realm metrics)

The pom depends on private Keycloak APIs (`keycloak-services`, `keycloak-model-jpa`,
`keycloak-server-spi-private`). These have no compatibility guarantee between minors and are
the most likely thing to break.

## Phase 0: retarget the extensions

1. In `keycloak-migration/custom-mapper/pom.xml` set `keycloak.version` to `26.7.0`, bump the
   artifact version to `1.2.0`, build via `task build-keycloak-custom-mapper` (plus
   `task test-keycloak-custom-mapper` / `test-keycloak-custom-mapper-docker`) and fix what
   breaks. Highest risk: the metrics code (JPA user counts) and the logout endpoint. The
   build task echoes the versioned JAR path, so bump the version there too.
2. Publish `keycloak-saml-nameid-mapper-1.2.0.jar` manually as a GitHub release on
   `RijksICTGilde/RIG-Cluster` (there is no release task in the Taskfile; v1.1.0 was
   published by hand; the init container downloads by release URL).
3. Validate the MinBZK theme v1.4.2 against 26.7 in the sandbox. It dates from January 2026
   so it is probably built against 26.x, but its release notes state no compatibility. If it
   breaks, raise an issue at MinBZK/keycloak-theme.

## Phase 1: modernize the deployment manifest

All changes in `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml`:

4. Image to `quay.io/keycloak/keycloak:26.7.0`; init container URL to the 1.2.0 JAR.
5. Rename bootstrap admin variables (old names are deprecated in 26):
   `KEYCLOAK_ADMIN` to `KC_BOOTSTRAP_ADMIN_USERNAME`,
   `KEYCLOAK_ADMIN_PASSWORD` to `KC_BOOTSTRAP_ADMIN_PASSWORD`.
6. Remove dead or removed options:
   - `KC_HOSTNAME_PORT` (hostname v1 option, gone in 26; hostname v2 is already active,
     the odcn overlay sets `KC_HOSTNAME` to a full URL which is the v2 style)
   - `KC_PROXY_ADDRESS_FORWARDING` (legacy Wildfly-era variable, already inert)
7. Resolve the conflicting proxy config: the args pass `--proxy-headers=forwarded` while the
   env sets `KC_PROXY_HEADERS=xforwarded`. Keep one; `xforwarded` matches our HAProxy and
   nginx ingress setup.
8. SPI option format: `--spi-theme-welcome-theme` becomes `--spi-theme--welcome-theme`
   (single-dash format is deprecated in 26). Same for
   `KC_SPI_ADMIN_REALM_RESTAPI_EXTENSION_ENABLED` if kept.
9. Optional: move the readiness probe from `/realms/master:8080` to `/health/ready:9000`
   (`KC_HEALTH_ENABLED` is already true; 9000 is the management interface and the intended
   path in 26).

## Phase 2: sandbox validation

10. Roll the sandbox (`task sandbox:setup` or image bump plus sync). The schema migration
    from 25 to 26.7 runs automatically on first boot; a direct jump is supported.
11. Test checklist:
    - SSO-Rijk login on rig-platform (exercises the XPath mapper and FORCE attribute mappers)
    - eager logout via `always-clear-session-logout`
    - `GET /realms/master/rig-metrics` returns realm metrics
    - client access restriction (RequireClientRoleAuthenticator)
    - login and account theme pages render (nl-design-system)
    - OPI startup bootstrap replays cleanly (realm create/update path, including the
      audit-event settings from the realm blueprints)
    - invite flow and wizard-driven realm provisioning

## Phase 3: production

12. Take a `pg_dump` of the `keycloak` database on rig-db first. There are no automated
    backups of this database and the schema migration is not reversible; the dump plus the
    old image is the rollback path.
13. Commit and let ArgoCD sync. Single replica, so a short outage during the migration is
    expected; no rolling-upgrade concerns.
14. Watch startup logs for the migration, then verify SSO-Rijk login, an OPI reprocess of
    one project, and the rig-metrics endpoint.
15. Note: persistent user sessions become the default. Sessions survive restarts from now
    on, at the cost of some database growth (session tables).

## Phase 4: after the upgrade

16. Add `adminEventsExpiration` to the realm blueprints
    (`operations-manager/python/opi/configs/keycloak/*.yaml`) and the
    `create_realm` connector path, mirroring how `eventsExpiration` is applied. Keycloak 25
    had no supported admin event expiration; 26 does. This closes the "admin events grow
    unbounded" caveat from the audit-event rollout, worth doing soon because OPI generates
    admin events on every reconcile.
17. Check the size of `admin_event_entity` and `event_entity` a few weeks after enabling
    audit events and set retention accordingly.

## Dependencies

- `keycloak-migration/custom-mapper/` (JAR source, Maven)
- MinBZK/keycloak-theme (login theme)
- OPI realm blueprints: `operations-manager/python/opi/configs/keycloak/*.yaml`
- OPI connector: `operations-manager/python/opi/connectors/keycloak.py` (`create_realm`)
- Kustomize overlays: `infrastructure/bootstrap/infrastructure/keycloak/controller/overlays/`
