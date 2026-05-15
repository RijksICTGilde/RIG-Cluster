# Keycloak Always-Clear-Session Logout

## What it is

A generic Keycloak `RealmResourceProvider` endpoint that **always clears the user's Keycloak session** — regardless of whether the upstream IdP responds — before redirecting the browser to a configurable downstream logout URL.

Intended for federated IdPs whose "logout" is a one-way redirect to a logout page with no SAML `LogoutResponse` and no OIDC callback. Without intervention, Keycloak's standard broker-logout flow parks the local session waiting for a response that never arrives, so the user's Keycloak session stays alive until idle timeout.

The endpoint flips the order: Keycloak still drives the standard logout chain, but when it reaches an IdP's logout URL — pointed at this endpoint instead of the upstream — the endpoint:

1. Resolves the user from the Keycloak identity cookie.
2. Calls `AuthenticationManager.backchannelLogout` to terminate the user session and notify OIDC clients.
3. Expires the identity, session, and auth-session cookies on the response.
4. Returns a 302 to the configured downstream logout URL.

The user lands on the upstream logout page with their Keycloak session already gone. No callback expected.

## How it's wired

### Java provider

- Module: `keycloak-migration/custom-mapper/` (bundled into the `keycloak-saml-nameid-mapper` JAR).
- Package: `nl.minbzk.rig.keycloak.logout`
- Classes:
  - `AlwaysClearSessionLogoutEndpointFactory` — `RealmResourceProviderFactory`, `getId() = "always-clear-session-logout"`.
  - `AlwaysClearSessionLogoutEndpoint` — `RealmResourceProvider`.
  - `AlwaysClearSessionLogoutEndpoint.LogoutResource` — JAX-RS bean carrying `@Provider` (required by RESTEasy in Keycloak Quarkus, see [keycloak#25882](https://github.com/keycloak/keycloak/issues/25882)). Handles `GET` on `/{idpAlias}` — matches SAML HTTP-Redirect binding. (Add `@POST` if an IdP ever switches to HTTP-POST binding for logout.)
- Registered in `src/main/resources/META-INF/services/org.keycloak.services.resource.RealmResourceProviderFactory`.

The JAR is fetched by the Keycloak init container from GitHub releases (`v1.1.0+`) and dropped into `/opt/keycloak/providers/`.

### URL pattern

```
{keycloak-base}/realms/{realm}/always-clear-session-logout/{idpAlias}
```

The `{idpAlias}` path segment names which IdP in the current realm to operate on. The endpoint reads that IdP's `downstreamLogoutUrl` config key to know where to redirect after clearing the session.

### IdP configuration

For each IdP that needs this behavior:

```yaml
config:
  singleLogoutServiceUrl: "{{ keycloak_url }}/realms/{{ realm_name }}/always-clear-session-logout/<idp-alias>"
  downstreamLogoutUrl: "<upstream-logout-page-url>"
```

- `singleLogoutServiceUrl` — where Keycloak sends the SAML `LogoutRequest`. Points at this endpoint with the IdP's own alias as the last path segment.
- `downstreamLogoutUrl` — passthrough config key, read at runtime via `realm.getIdentityProviderByAlias(alias).getConfig()`. `KeycloakYamlHandler._process_identity_providers` passes arbitrary `config:` keys through unfiltered.

## Current use

| Realm | IdP alias | Upstream | Why |
|---|---|---|---|
| `rig-platform` (prod) | `sso-rijk` | BZK SSO-Rijk | BZK's logout endpoint shows a logout page but does not send a SAML `LogoutResponse`. |

To enable for another IdP: set the two `config:` fields above in the realm's bootstrap YAML. No Java changes needed.

## Scope

- **Production only at present.** Local and sandbox use OIDC-into-production for their `sso-rijk` alias, so they don't traverse the SAML chain and don't need the shim.
- The endpoint is **dormant unless an IdP's SLO URL points at it.** Deploying the JAR has no behavior change on its own.

## Operational notes

- **No request signature verification.** The endpoint trusts the user's Keycloak identity cookie, not the SAML `LogoutRequest` query params. A spurious GET with no cookie just 302s to the configured downstream URL and does nothing else — harmless.
- **`logoutBroker=false` is critical** in the `backchannelLogout` call. Setting it to true would cause Keycloak to walk the broker logout chain again, re-redirecting to this same IdP's SLO URL — this endpoint — causing recursion.
- **No callback expected from upstream.** That is the entire point. Keycloak's standard logout flow's "wait for `LogoutResponse`" step is bypassed by killing the session ahead of the redirect.
- **Cookies are expired** on the outgoing response (`IDENTITY`, `SESSION`, `AUTH_SESSION_ID`). The next request to Keycloak will not carry a stale cookie referencing a removed session.

## Disabling / rollback

Per-IdP revert: in the IdP's YAML `config:`, point `singleLogoutServiceUrl` back at the original upstream URL and remove `downstreamLogoutUrl`. After re-reconciliation the IdP is back to stock behavior — though the Java code stays loaded in the JAR (dormant; only fires when an IdP's SLO URL points at it).

To remove the JAR contribution entirely: revert the version in `deployment.yaml` and `patch-custom-mapper.yaml`, or delete the factory registration in `META-INF/services/...RealmResourceProviderFactory`.

## Verification

Before flipping the SLO URL on the target IdP:

1. Deploy the new JAR (`v1.1.0+`) to sandbox via the standard Keycloak deployment update.
2. From outside the cluster, confirm routing with an alias that does NOT exist in the realm:

   ```bash
   curl -v https://keycloak.<sandbox-host>/realms/rig-platform/always-clear-session-logout/__missing__
   ```

   Expect `404` with body `"Identity provider not configured"`. **A connection error or different 404 with HTML body means routing is broken** — fix before proceeding.
3. After flipping the SLO URL in production: log in via the full chain, log out, confirm:
   - The browser lands on the upstream logout page.
   - The user's Keycloak session no longer appears in `GET /admin/realms/<realm>/users/{id}/sessions`.
   - A subsequent visit to a protected app requires re-authentication (no stale cookie).

## Downstream propagation: clearing ZAD realm sessions too

The shim only terminates the session in the realm where it runs (typically `rig-platform`). User sessions in downstream realms (e.g. `wies-odcn-production`) that federate to `rig-platform` via OIDC are separate session records and would otherwise linger.

To kill those too, the **federation client in `rig-platform`** (the OIDC client that each project realm uses to talk to `rig-platform`) is configured under `platformClients` in `sso-support.yaml` / `sso-only.yaml`:

```yaml
attributes:
  backchannel.logout.url: "{{ keycloak_url }}/realms/{{ project_realm_name }}/protocol/openid-connect/logout/backchannel-logout"
  backchannel.logout.session.required: "true"
  post.logout.redirect.uris: "+"
```

When the shim calls `AuthenticationManager.backchannelLogout` on the `rig-platform` user session, Keycloak walks that user's client sessions and POSTs an OIDC `logout_token` to every client with `backchannel.logout.url` set. Each downstream realm receives this at its standard OIDC backchannel-logout endpoint and clears its user session locally.

This is independent of the `backchannelSupported=false` setting on the `rig-platform-oidc` IdP in ZAD realms — that setting governs the *upstream* (ZAD → rig-platform) logout direction; the federation-client attribute governs the *downstream* (rig-platform → ZAD) direction.

### Why we explicitly store `issuer` on `rig-platform-oidc`

The `rig-platform-oidc` IdP in each ZAD realm carries an explicit `issuer` field:

```yaml
issuer: "{{ keycloak_url }}/realms/{{ platform_realm_name }}"
```

This duplicates information that *should* be auto-resolved from `discoveryUrl`. We set it explicitly because **Keycloak's runtime resolution of the issuer from `discoveryUrl` fails at backchannel-logout token verification time**, even when the discovery URL itself returns the correct issuer claim. The symptom is a `LogoutToken verification with identity provider failed` event in the receiving realm and a lingering user session despite a successful POST. Storing the issuer directly on the IdP bypasses the lookup and verification succeeds.

Verified in production 2026-05-15: manually setting the field on one IdP via the admin UI immediately fixed verification; this commit makes the fix declarative across all project realms.

The chain continues recursively: a ZAD realm receiving the backchannel logout will in turn notify any of its own clients that have `backchannel.logout.url` configured. App-side clients are out of scope for this feature — adding them is a per-app concern.

## Companion: ForceAuthn upstream

Clearing local sessions is only half the picture — if BZK retains its own cookie, the next login to any of our apps would silently SSO through to BZK and re-issue local sessions without prompting the user. To prevent that, `sso-rijk` is configured with `forceAuthn: "true"`:

```yaml
config:
  forceAuthn: "true"
```

This adds `ForceAuthn="true"` to every SAML AuthnRequest. BZK re-prompts the user even if it holds a session. Critically, this only fires when Keycloak itself has no session for the user — SSO between our apps via the rig-platform session continues to be silent.

Effective combined behavior:
- Logout → local Keycloak sessions cleared (this endpoint + the backchannel chain).
- Next visit to any app → no rig-platform session → SAML AuthnRequest to BZK with ForceAuthn=true → BZK re-prompts.

Caveat: BZK must honor the standard SAML `ForceAuthn` attribute. If their implementation ignores it, this no-ops and the silent-auth window persists.

## Related

- BZK federation chain context: `[[project_keycloak_slo]]` — every downstream OIDC broker (`rig-platform-oidc` in ZAD realms) must have `backchannelSupported=false` so the chain stays frontchannel up to this point.
- Existing `RealmResourceProvider` pattern this mirrors: `nl.minbzk.rig.keycloak.metrics.RigMetricsEndpoint`.
