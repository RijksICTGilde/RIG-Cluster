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
- `downstreamLogoutUrl` — passthrough config key, read at runtime via `realm.getIdentityProviderByAlias(alias).getConfig()`. `KeycloakYamlHandler` passes arbitrary `config:` keys through unfiltered (see `keycloak_yaml_handler.py:381–397`).

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

## Related

- BZK federation chain context: `[[project_keycloak_slo]]` — every downstream OIDC broker (`rig-platform-oidc` in ZAD realms) must have `backchannelSupported=false` so the chain stays frontchannel up to this point.
- Existing `RealmResourceProvider` pattern this mirrors: `nl.minbzk.rig.keycloak.metrics.RigMetricsEndpoint`.
