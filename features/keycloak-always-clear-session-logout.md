# Keycloak Always-Clear-Session Logout

## What it is

A generic Keycloak `RealmResourceProvider` endpoint that **always clears the user's Keycloak session** — regardless of whether the upstream IdP responds — before redirecting the browser to a configurable downstream logout URL.

Intended for federated IdPs whose "logout" is a one-way redirect to a logout page with no SAML `LogoutResponse` and no OIDC callback. Without intervention, Keycloak's standard broker-logout flow parks the local session waiting for a response that never arrives, so the user's Keycloak session stays alive until idle timeout.

**Dit heeft een keerzijde die je moet kennen voordat je ergens anders in de uitlogketen gaat zoeken: de keten eindigt hier.** De browser landt op de uitlogpagina van de IdP en komt niet terug, dus `AuthenticationManager.finishBrowserLogout` draait nooit: de enige plek die de `KEYCLOAK_SESSION`-cookie wist en de front-channel-logout-iframes rendert. Zie "De terugreis komt niet" hieronder.

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

The JAR is built into the image the Keycloak pod runs (`images/keycloak-zad/`) and lands in `/opt/keycloak/providers/`.

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

To remove the JAR contribution entirely: pin an earlier image tag in `keycloak/controller/base/deployment.yaml`, or delete the factory registration in `META-INF/services/...RealmResourceProviderFactory` and build a new image.

## Verification

Before flipping the SLO URL on the target IdP:

1. Deploy the new JAR to sandbox with `task publish-keycloak-image` + `task sandbox:sync`.
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

Die blauwdruk geldt alleen bij het aanmaken van een client. Bestaande federatieclients worden bijgetrokken door `_ensure_idp_and_platform_client_configuration` (`opi/manager/keycloak_manager.py`), dat deze attributen meeneemt in zijn reconcile. Clients van voor de blauwdruksleutel misten anders voorgoed hun `backchannel.logout.url`, en dan bereikte de uitlog hun projectrealm nooit.

This is independent of the `backchannelSupported=false` setting on the `rig-platform-oidc` IdP in ZAD realms — that setting governs the *upstream* (ZAD → rig-platform) logout direction; the federation-client attribute governs the *downstream* (rig-platform → ZAD) direction.

### Why we explicitly store `issuer` on `rig-platform-oidc`

The `rig-platform-oidc` IdP in each ZAD realm carries an explicit `issuer` field:

```yaml
issuer: "{{ keycloak_url }}/realms/{{ platform_realm_name }}"
```

This duplicates information that *should* be auto-resolved from `discoveryUrl`. We set it explicitly because **Keycloak's runtime resolution of the issuer from `discoveryUrl` fails at backchannel-logout token verification time**, even when the discovery URL itself returns the correct issuer claim. The symptom is a `LogoutToken verification with identity provider failed` event in the receiving realm and a lingering user session despite a successful POST. Storing the issuer directly on the IdP bypasses the lookup and verification succeeds.

Verified in production 2026-05-15: manually setting the field on one IdP via the admin UI immediately fixed verification; this commit makes the fix declarative across all project realms.

The chain continues recursively: a ZAD realm receiving the backchannel logout will in turn notify any of its own clients that have `backchannel.logout.url` configured. Dat vraagt een backchannel-endpoint in de applicatie zelf, en dat hebben onze applicaties niet. De front-channel-kant daarvan is `logout-path`, zie `features/keycloak-logout-path.md` - met de beperking uit de volgende paragraaf.

## De terugreis komt niet

Dit is de kennis die ontbrak toen deze shim werd gebouwd, en die op 31 augustus en 2 september 2026 op productie is gemeten bij `docs.rijksapp.nl`.

Na een klik op uitloggen waren de Keycloak-sessies weg (`mb-docs-helmfile-odcn-production` en `rig-platform`, de BZK-sessie bleef zoals verwacht), maar de applicatie hield haar cookie en toonde de gebruiker nog als ingelogd. Niet omdat de propagatie faalde, maar omdat de applicatie het nooit hoort: zij stuurt de browser naar het end-session endpoint en rekent erop dat die via `post_logout_redirect_uri` terugkomt om de eigen sessie te wissen. De shim laat de keten eindigen op de BZK-uitlogpagina, dus die terugreis komt nooit.

Twee gevolgen, beide gemeten in het harnas (`operations-manager/python/tests/e2e/test_keycloak_logout_harness.py`, marker `keycloak_harness`):

1. **Een applicatie moet haar eigen sessie wissen vóór de redirect.** ZAD doet dat (`opi/api/auth_routes.py`, `request.session.clear()` voor de redirect) en heeft dit probleem daarom niet. Het contract staat in `instructions/uitloggen-in-een-applicatie.md`.
2. **`frontchannel.logout.url` op een applicatieclient wordt binnen deze federatie niet aangeroepen**, ook niet als hij netjes gezet is. Buiten de federatie (inloggen op het projectrealm zelf) wel: daar draait `finishBrowserLogout` in de browser van de gebruiker en belt Keycloak het pad. Binnen de federatie bereikt de uitlog het projectrealm als server-naar-server POST, en daar valt niets te renderen. Zie "Waarom de backchannel-notificatie faalt" hieronder.

### Repareer dit niet door de shim om te draaien

Het naïeve omdraaien is gemeten en is slechter: `finishBrowserLogout` in plaats van `backchannelLogout` laat de applicatierealms juist overleven (de backchannel-notificatie valt weg), BZK wordt niet meer bezocht, en het Keycloak-log geeft `LOGOUT_ERROR ... error="logout_failed", reason="There was an error during the local logout"` op beide applicatierealms. Dat prototype is bewust niet uitgerold. Zie `browserLogoutAllClients` in Keycloak 25.0.6: die doet beide mechanismen, maar slaat client-sessies over die al op `LOGGING_OUT` staan, en daar botst het. De `logout_failed` die dat prototype opleverde heeft dezelfde oorzaak als die van de ongewijzigde shim, hieronder: ook daar liep de notificatie naar de applicatierealms over de backchannel.

### Waarom de backchannel-notificatie faalt: de front-channel-vlag

Dezelfde `logout_failed` is ook op de ONgewijzigde shim te zien, op BEIDE applicatierealms, en de sessies blijven staan. Dat leek een ordeningsvraag (waarom faalt ook het realm dat niet in de browserketen zit?) maar is het niet. De oorzaak is de schakelaar **Front channel logout** op de applicatieclient, die OPI op beide clients van elke deployment aanzet (`frontchannelLogout: True`, `opi/connectors/keycloak.py`).

De keten in Keycloak 25.0.6, van de POST tot de blijvende sessie:

1. De shim POST een `logout_token` naar `backchannel.logout.url` van elke federatieclient. Dat is een server-naar-server POST: er is geen browser en geen browserketen. Daarom faalt `app2` precies zoals `app`, en daarmee valt de vraag weg in plaats van beantwoord te worden.
2. Het ontvangende realm komt via `LogoutEndpoint.backchannelLogout` uit op `AuthenticationManager.backchannelLogout`. Die VERWIJDERT de sessie, en berekent daarna pas `localLogoutSucceeded`.
3. `backchannelLogoutAll` loopt over de clientsessies van de usersessie (`userSession.getAuthenticatedClientSessions()`) en slaat elke client over waarvan de front-channel-vlag aan staat (`AuthenticationManager`, `if (client.isFrontchannelLogout() ...) return null`). Zo'n clientsessie wordt dus nooit op `LOGGED_OUT` gezet.
4. `checkUserSessionOnlyHasLoggedOutClients` loopt over dezelfde clientsessies, rekent juist die overgeslagen client als "niet uitgelogd" en geeft `false`.
5. Het endpoint antwoordt daarop 501 `"There was an error during the local logout"`, en `KeycloakErrorHandler.getResponse` zet `tx.setRollbackOnly()`. **Daarmee wordt het verwijderen uit stap 2 teruggedraaid.**

`logout_failed` in het log en de blijvende sessie zijn dus een en hetzelfde feit, geen twee. En een 501 op dit endpoint betekent niet dat het logout-token werd afgewezen; dat geeft `invalid_token`.

Wat hierboven staat is in het harnas gemeten, met de vlag als enige variabele: met de vlag uit verdwijnt `logout_failed` en gaan de sessies van BEIDE applicatierealms weg. De verwachting is dus dat elk project dat OPI uitrolt vandaag zijn projectrealm-sessie houdt na een uitlog binnen de federatie, want OPI zet die vlag aan.

**Dat is nog niet op productie nagemeten, en de nulmeting van 31-08 en 02-09-2026 wijst de andere kant op:** daar was de sessie van `mb-docs-helmfile-odcn-production` na een klik juist weg, terwijl die client de vlag ook aan heeft. Die meting is één browsersessie waarvan de precieze klikreeks niet meer te reconstrueren is (er was een tweede klik in het spel, met `session_expired`), dus ze weerlegt het mechanisme niet, maar ze bevestigt het ook niet. Wie hier verder komt: meet op productie één klik, en lees de sessie uit de admin-API in plaats van uit de cookie.

### Twee clients dragen de vlag, en hij weegt per clientsessie

OPI zet de vlag per deployment op TWEE clients: `create_deployment_client` op `<client>` en `_ensure_public_client` onvoorwaardelijk op `<client>-public` (`opi/connectors/keycloak.py`). Dat is geen detail voor de vork hieronder, want beide lussen uit de keten lopen over `userSession.getAuthenticatedClientSessions()`: een client met de vlag aan houdt de sessie alleen tegen als de gebruiker er ook een clientsessie op heeft. Beide kanten zijn in het harnas gemeten, met de vlag uit op `<realm>-client` en aan op `<realm>-client-public`: zonder een sessie op de client met de vlag gaan de sessies weg en blijft `logout_failed` weg, met een sessie erop blijven ze staan en is `logout_failed` terug.

Welke van de twee clients een applicatie gebruikt weet OPI niet, en een project met een backend op de confidential client en een keycloak-js-frontend op de public client gebruikt ze beide. "De vlag uit" betekent daarom: uit op beide clients van de deployment.

### De fan-out binnen de federatie: waarom hij er niet is

Een front-channel fan-out heeft een browser nodig om iframes in te renderen. Binnen de federatie worden de projectrealms bereikt met een POST, en daar zit geen browser bij. Twee kandidaten zijn gemeten en beide vallen af (de toetsen staan in het harnas):

- **De afgebroken terugreis hervatten.** Wat de shim weghaalt is een GET naar `broker/<alias>/endpoint/logout_response?state=<sessie-id>`, het punt waar het applicatierealm `finishBrowserLogout` zou draaien. Na de shim weigert dat endpoint: het eist `state == LOGGING_OUT`, en de rollback uit stap 5 laat die stand niet staan. En het geldt hoe dan ook alleen voor het ENE realm waarin de gebruiker klikte; de andere projectrealms wachten nergens op.
- **Het realm binnengaan met `initiating_idp`.** Dat werkt: het realm slaat de brokerstap over, draait `finishBrowserLogout` en belt het pad. Maar zonder `id_token_hint` blijft `confirmationNeeded` waar (`LogoutEndpoint` 190-294) en staat er eerst een bevestigingsscherm, en een id_token voor een applicatierealm kan `rig-platform` niet maken. In een iframe klikt niemand.

Daarmee is de keuze een vork, en beide kanten kosten iets. De vlag is namelijk ook de VOORWAARDE voor het pad: Keycloak gebruikt `frontchannel.logout.url` alleen voor clients waarvan de vlag aan staat.

| | vlag aan (vandaag) | vlag uit op beide deployment-clients |
|---|---|---|
| buiten de federatie | `logout-path` wordt gebeld | wordt niet gebeld |
| binnen de federatie | wordt niet gebeld, **en de projectrealm-sessie blijft staan** | wordt niet gebeld, **alle projectrealm-sessies gaan weg** |

De rechterkolom is gemeten met de vlag uit op `<client>` EN `<client>-public`, met een sessie op elk van de twee. Blijft hij op een van de twee aan en heeft de gebruiker daar een sessie, dan is de uitkomst die van de linkerkolom.

Welke kant de goede is, is een productkeuze en geen codedetail: links houdt een functie die alleen buiten de federatie werkt, rechts kiest voor sessies die echt sterven. Een derde weg zou zijn om `backchannel.logout.url` van de federatieclients naar een eigen endpoint te laten wijzen dat de front-channel-clients zelf op `LOGGED_OUT` zet voordat het `backchannelLogout` aanroept; dat haalt de 501 en de rollback weg en houdt de vlag aan, maar belt het pad nog steeds niet. Dat is niet gebouwd.

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

- `instructions/uitloggen-in-een-applicatie.md` - het contract voor de applicatiekant: wis je eigen sessie vóór de redirect
- `features/keycloak-logout-path.md` - het opt-in `logout-path` dat de front-channel logout URL op de client zet
- BZK federation chain context: `[[project_keycloak_slo]]` — every downstream OIDC broker (`rig-platform-oidc` in ZAD realms) must have `backchannelSupported=false` so the chain stays frontchannel up to this point.
- Existing `RealmResourceProvider` pattern this mirrors: `nl.minbzk.rig.keycloak.metrics.RigMetricsEndpoint`.
