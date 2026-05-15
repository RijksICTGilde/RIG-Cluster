# Keycloak 25 → 26 Upgrade

## Why

We run `quay.io/keycloak/keycloak:25.0.6`, released September 2024. Keycloak 25 went end-of-life when 26 shipped in October 2024. The runtime is past upstream support, lacks security patches and bug fixes from the 26.x line, and the gap widens every month it sits.

This is a deliberate piece of work, sequenced **after** the always-clear-session logout endpoint (v1.1.0 of the custom mapper JAR) has shipped and stabilised on 25.0.6. Mixing the two makes failure modes impossible to attribute.

## Scope

In scope:
- Runtime image bump on `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml` and all overlays.
- `keycloak-migration/custom-mapper/pom.xml` Keycloak dep bump + recompile + retest of all four custom providers:
  - `UnrestrictedXPathAttributeMapper`
  - `RequireClientRoleAuthenticator`
  - `RigMetricsEndpoint`
  - `AlwaysClearSessionLogoutEndpoint`
- `deployment.yaml` env var audit against 26.x deprecations (`KC_HOSTNAME`, `KC_HOSTNAME_PORT`, `KC_PROXY_ADDRESS_FORWARDING`, etc.).
- NL design system theme JAR compatibility check (currently `v1.4.2`).
- Postgres DB migration drill — Keycloak runs forward-only schema migrations on first 26.x boot.
- Sandbox-first rollout, then production with a maintenance window.

Out of scope:
- Realm config refactors. The bootstrap YAMLs stay as-is unless a 26.x change forces a field-level edit.
- Theme rework beyond compatibility.
- Switching to a different Keycloak distribution.

## Phases

### Phase 0 — Investigation

Output: a section appended to this doc with concrete findings before any code changes land.

- [ ] Verify the latest stable 26.x patch tag on `quay.io/keycloak/keycloak`.
- [ ] Read the official Keycloak 25→26 upgrade guide and every 26.0 → 26.latest changelog. Build a list of:
  - Removed/renamed env vars (initial hits: `KC_HOSTNAME` semantics change to full URL, `KC_HOSTNAME_PORT` removed, `KC_PROXY_ADDRESS_FORWARDING` removed).
  - SPI signature changes in `AuthenticationManager`, `CookieProvider`/`CookieType`, `IdentityProviderMapper`, `RealmResourceProvider`.
  - Theme/Freemarker breaking changes.
  - Admin REST API path/payload changes affecting `connectors/keycloak.py`.
- [ ] Check whether `MinBZK/keycloak-theme` has a 26-compatible release. If not, scope the theme work as a prerequisite ticket.

### Phase 1 — Custom JAR compiles against 26.x

Branch off `main`. Do not bundle with logout-endpoint work.

- [ ] Bump `<keycloak.version>` in `keycloak-migration/custom-mapper/pom.xml`.
- [ ] `mvn clean package`. Fix compile breakages. Most likely candidates:
  - `AuthenticationManager.backchannelLogout` signature drift.
  - `CookieProvider` / `CookieType` API shape.
  - `IdentityProviderMapper` abstract method changes.
- [ ] Bump artifact version (`1.1.0` → `1.2.0`) to mark the 26-targeted build.
- [ ] Run `task test-keycloak-custom-mapper` and `task test-keycloak-custom-mapper-docker`.

### Phase 2 — Sandbox runtime

- [ ] Bump image tag in the sandbox overlay only.
- [ ] Audit `deployment.yaml` env vars; fix anything 26 has removed or changed.
- [ ] Update `KC_HOSTNAME` semantics (full URL form).
- [ ] Roll the sandbox Keycloak pod. Verify on first boot:
  - DB migrations run cleanly (watch logs).
  - Pod becomes ready.
  - Admin console reachable.
  - Existing OIDC `sso-rijk` login path works (sandbox OIDC into prod).
  - `RigMetricsEndpoint` still scrapeable from Prometheus.
  - `UnrestrictedXPathAttributeMapper` still extracts NameID correctly (verified via test login + user attribute inspection).
  - `AlwaysClearSessionLogoutEndpoint` URL still routes (`curl` against the realm path; expect `404 "Identity provider not configured"` for unknown alias).
  - NL design system theme renders without errors.

### Phase 3 — Production rollout

- [ ] Schedule a maintenance window with stakeholders. Keycloak DB migrations are forward-only.
- [ ] Take a Postgres dump of the production Keycloak DB.
- [ ] Verify the dump can be restored to a scratch namespace.
- [ ] Bump image tag in the production overlay.
- [ ] Watch the rollout. DB migrations run on first 26.x pod startup; this can take longer than a normal restart depending on table sizes.
- [ ] Run the full Phase 2 verification matrix against production.
- [ ] Validate the BZK SAML federation end-to-end:
  - Fresh login → user identity created/updated correctly.
  - Logout via the always-clear-session endpoint → Keycloak session terminated → browser lands on BZK logout page.

## Risks

- **DB migration is forward-only.** Rollback path: restore the pre-upgrade Postgres dump and redeploy the old image. Validate the restore path *before* the production cutover.
- **`KC_HOSTNAME` semantics change** is the highest-probability footgun. The 25.x value `keycloak.kind` (a hostname) is invalid in 26.x — it expects a full URL like `https://keycloak.rijksapp.nl`. Affects every cluster overlay and the `KC_HOSTNAME_PORT` setting can be removed.
- **Custom JAR runtime breakage** despite a clean compile. The compile catches signature drift; runtime catches behaviour drift (e.g. lifecycle order changes in `RealmResourceProvider`, exception types).
- **Theme JAR incompatibility.** If `MinBZK/keycloak-theme` hasn't shipped a 26-compatible build, this is a hard blocker until either an upstream release lands or we patch the theme ourselves.
- **Admin REST API drift.** `operations-manager/python/opi/connectors/keycloak.py` talks to the admin API extensively. Any path/payload change there manifests as OPI start-up or reconcile failure. Test OPI's full Keycloak setup flow against sandbox 26.x before declaring Phase 2 done.

## Sequencing

1. Logout endpoint (v1.1.0) ships on 25.0.6 → stabilise.
2. Phase 0 investigation → findings appended to this doc → confirm scope is still right.
3. Phase 1 + 2 on a feature branch.
4. Phase 3 with a scheduled maintenance window.

No phase starts before the previous is signed off.

## Open questions

- Target 26.x patch: latest stable, or one minor behind for risk reduction? Decide after Phase 0 changelog review.
- Does the cluster team have any 26.x-specific blockers (e.g. cert-manager / ingress compatibility)?
- Operations Manager realm-reconciliation behaviour against a 26.x admin API — needs explicit smoke test.
