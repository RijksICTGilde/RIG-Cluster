# Deel A: authenticatie en web-laag

Whitebox-toets op de code van de Operations Manager (ZAD), branch `main`, stand 2026-09-26. Alleen gelezen: geen wijzigingen, geen kubectl, geen netwerk. Regelnummers verwijzen naar `operations-manager/python/` tenzij anders vermeld.

## Onderzocht

**Bestanden volledig gelezen**: `opi/api/auth_routes.py`, `opi/core/secret_key.py`, `opi/middleware/authorization.py`, `opi/core/auth_decorators.py`, `opi/services/user_service.py`, `opi/api/user_token_auth.py`, `opi/utils/api_keys.py`, `opi/api/endpoint_util.py`, `opi/api/params.py`, `opi/utils/totp.py`, `opi/utils/csrf.py`, `opi/middleware/security_headers.py`, `opi/core/errors.py`, `opi/api/invite_routes.py`, `opi/manager/invite_manager.py`, `opi/web/lotc_router.py`.

**Gericht gelezen**: `opi/server.py` (app-creatie, middleware-volgorde, foutafhandeling, routers, `__main__`), `opi/core/config.py` (beveiligingsvlaggen), `opi/core/startup.py` (OAuth-registratie), `opi/api/logs_websocket_router.py` (handshake, origin, sessie), `opi/api/logs_router.py` (`/pods`), `opi/api/task_router.py`, `opi/api/router.py` (IPRateLimiter), `opi/api/v2/router.py` (bearer-routes), `opi/services/project_service.py` en `opi/manager/project_manager.py` (opslag API-key), `opi/services/catalog/invite/__init__.py` en `editables.py`, `opi/forms/editables/validators.py` (InviteKeyValidator), `opi/connectors/keycloak.py` (`create_user`, `send_verify_email`), `opi/web/router.py` (publieke routes, `/weergave`), `opi/web/lotc_fixtures.py`, `opi/web/lotc_switch.py` (`render`, `project_tab_url`), `opi/forms/widgets/lotc.py`, `opi/web/router_wizard.py` (`_summary_text`), `manifests/sidecar-authorization-wall.yaml.jinja`, `operations-manager/Dockerfile`, `operations-manager/docker-entrypoint.sh`, `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml` en de sleutelnamen in `operations-manager-env-secrets.yaml`, `docs/post-mortems/user-impersonation-oidc-email-claim.md`, `features/keycloak-always-clear-session-logout.md`.

**Gedaan**: routeinventaris via AST over `opi/api`, `opi/web` en `opi/server.py` (180 routes: 75 `requires_sso`, 55 `validate_api_token`, 7 `validate_admin_api_key`, 3 `validate_master_api_key`, 2 `validate_user_token`, 37 zonder decorator, elk van die 37 handmatig beoordeeld). Grep op `|safe`, `Markup(`, `autoescape`, `X-API-Key`, `request.session`, `x-forwarded-for`, `is_admin`, `backchannel`.

**Niet gedaan**: de bron van authlib, oauth2-proxy en Starlette niet gelezen (aannames daarover staan als AANNEMELIJK). `opi/forms/renderer.py` niet regel voor regel op elke widget geaudit. `opi/middleware/maintenance.py` en `openapi_etag.py` niet gelezen. Templates niet integraal gelezen, alleen de `|safe`-plekken en de invite-templates. Keycloak-realmconfiguratie (first-broker-login, token-lifetimes, redirect-URI-lijst) niet gecontroleerd: dat is geen code in deze repo. Geen dynamische test.

## Sterke plekken

1. **`email_verified` wordt op alle drie de paden afgedwongen**, precies zoals de post-mortem belooft. Sessiepad: `opi/api/auth_routes.py:119` `if user_info.get("email_verified") is not True:` gevolgd door een redirect zonder sessie. Bearer-pad: `opi/api/user_token_auth.py:212-213` `if claims.get("email_verified") is not True: raise UserTokenError(...)`. Authorization-wall-sidecar: `manifests/sidecar-authorization-wall.yaml.jinja:26` `--insecure-oidc-allow-unverified-email=false`.
2. **Bearer-tokenverificatie is strak**: vaste lijst asymmetrische algoritmen (`user_token_auth.py:42`), issuer uit het discovery-document en audience uit config als `essential` (`:161-165`), `exp` met 60 s leeway (`:184`), JWKS-cache met TTL en een refetch bij onbekende `kid` die op 60 s gethrottled is zodat een anonieme aanroeper geen netwerkstorm kan veroorzaken (`:97-113`). Identiteit en autorisatie zijn gescheiden: `authorize_claims` toetst daarna aan dezelfde allowlist als de UI (`:215`).
3. **CSRF is centraal en niet opt-in**: `opi/utils/csrf.py:90-96` dwingt voor elke onveilige methode buiten `/api/`, `/static/`, `/auth/` en de probes zowel een double-submit token (`:201-213`, `secrets.compare_digest`) als een exacte host-match op Origin/Referer af (`:292-311`). Dat laatste is de echte verdediging tegen een tenant op een zuster-subdomein; de localhost-uitzondering geldt alleen bij `DEBUG` en alleen als de request zelf naar localhost gaat (`:280-283`).
4. **Sessiecookie-vlaggen**: `opi/server.py:633-639` `same_site="lax"`, `https_only=not settings.DEBUG`, `max_age=SESSION_MAX_AGE_SECONDS` (8 uur); httponly is de Starlette-default. `DEBUG` staat default `False` (`config.py:181`) en de prod-overlay zet hem niet.
5. **SECRET_KEY zonder default in de bron**: `opi/core/secret_key.py:33-46` genereert per proces een random sleutel als hij ontbreekt, `:49-67` weigert te starten onder 32 tekens. Productie levert hem via het SOPS-secret (sleutelnaam `SECRET_KEY` in `operations-manager-env-secrets.yaml`), dus stabiel over pods.
6. **WebSocket-handshake doet alles in de goede volgorde**: Origin-check tegen Host (`logs_websocket_router.py:78-118`), sessiecookie zelf geverifieerd met dezelfde `TimestampSigner` en `max_age` (`:134-179`), allowlist (`:388`), projectautorisatie (`:397`), verbindingslimiet (`:405`), en pas dan `accept()` (`:412`).
7. **Project-API-key**: timing-safe vergeleken (`endpoint_util.py:68` `secrets.compare_digest`), altijd gebonden aan het project uit de route (`:56-64`, een route zonder `project_name` faalt gesloten), en AGE-versleuteld in het projectbestand (`project_manager.py:7145-7149` `$.config.api-key` plus `decrypt_age_content`).
8. **Admin- en master-key falen gesloten**: `endpoint_util.py:127-132` en `:168-173` antwoorden 501 als de sleutel niet geconfigureerd is. De productie-secret bevat geen `ADMIN_API_KEY` en geen `MASTER_API_KEY`, dus die tien endpoints staan in productie uit.
9. **Foutafhandeling lekt niets**: `opi/core/errors.py` levert een zelfstandige pagina of `application/problem+json` met alleen een kenmerk; `server.py` `_fout_antwoord` vervangt de uitzonderingstekst door `GENERIEKE_FOUTTEKST`, en `log_render_failure` (`errors.py:130-137`) houdt sjabloonpad en bronregel in de log.
10. **Security headers**: HSTS met preload bij https, `nosniff`, `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `base-uri 'self'`, `form-action` beperkt tot self plus Keycloak (`security_headers.py:54-85`).
11. **Invite-flow kent de basisdingen**: PKCE S256 met verifier in de server-side sessie en `state` (`invite_routes.py:36-50, 96-100, 429-438, 547-556`), `prompt=login` (`:101`), 128-bit gegenereerde keys (`services/catalog/invite/__init__.py:66-78`), lokale accounts krijgen `emailVerified=False` plus required action `VERIFY_EMAIL` en de provider daarvan wordt expliciet aangezet zodat Keycloak hem niet stil overslaat (`connectors/keycloak.py:3639-3663`), wachtwoordbeleid 12 tekens met drie klassen (`invite_manager.py:459-485`). Domeinrestrictie wordt op beide paden met een `@`-voorvoegsel getoetst (`invite_manager.py:94-99`, `invite_routes.py:769-772`), dus `evil-domein.nl` matcht `domein.nl` niet.
12. **Geen open redirects gevonden**: na login vast naar `/dashboard` (`auth_routes.py:145`); `/weergave` accepteert alleen een lokaal pad en weigert `//` (`web/router.py:2762`); Starlette percent-codeert een backslash, dus `/\evil` werkt ook niet.
13. **Padtraversal in de LOTC-preview is dichtgezet met een allowlist** die bij het opstarten wordt opgebouwd (`lotc_router.py:38-59`, `lotc_fixtures.py:48-57`).
14. **Autoescape staat aan en `|safe` is gemotiveerd**: `opi/core/templates_lotc.py:78-80` zet `autoescape = True`; elke `|safe` in `opi/templates_lotc` krijgt HTML die de renderer zelf bouwde, met waarden door `html.escape` (`router_wizard.py:1837-1849`) of door Jinja-autoescape in de widgetsjablonen (`forms/widgets/lotc.py:40-91`). Gebruikersinvoer die ik naging (projectnaam, display-name, `?domain=` en `?code=` op de invite-foutpagina) landt via `{{ }}` in autoescape (`bg/invite-error.html.j2:40-44`).
15. **Intrekking werkt ondanks stateless sessies**: de middleware toetst de allowlist op elk verzoek (`authorization.py:116`), en admin-pagina's toetsen zelf `require_platform_admin` (`auth_decorators.py:65-79`), niet alleen het menu. Projectautorisatie zit op één plek (`services/project_authorization.py:40`).

## Bevindingen

### A-1 Ongeauthenticeerde, onbegrensde accountaanmaak en mailverzending via invite-registratie

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 5.16 (identiteitsbeheer), 8.26 (beveiligingseisen applicaties), 8.6 (capaciteitsbeheer)
- **Beschrijving**: `POST /invite/{key}/register` is publiek en maakt per aanroep een Keycloak-gebruiker aan in het projectrealm en stuurt een verificatiemail. Er is geen rate-limiting, geen `max_uses`, geen vervaldatum op een invite. De enige limiter in de codebase (`IPRateLimiter`) staat op de subdomein-check (`opi/api/router.py:343`, `opi/web/router_self_service.py:24`, `opi/api/v2/router.py:626`) en nergens in de invite-flow. Een invite blijft geldig tot iemand hem uit het projectbestand haalt.
- **Bewijs**: `opi/api/invite_routes.py:729` `@invite_router.post("/{key}/register", ...)` zonder decorator of limiter; `opi/manager/invite_manager.py:427-443` `created_user = await keycloak.create_user(...)` gevolgd door `await keycloak.send_verify_email(realm_name, user_id)`; `opi/handlers/project_file_handler.py:3590-3608` `get_invite_by_key` toetst alleen aanwezigheid in `active`, geen tijd of teller; `grep -rn "expir\|max_uses"` in de invite-code levert niets op.
- **Aanvalsscenario**: een aanvaller met één geldige invite-link (die links worden per mail rondgestuurd) scriptt duizenden registraties met adressen binnen het toegestane domein. Effect: het projectrealm vervuilt met accounts die al wel de rollen uit de invite dragen (`invite_manager.py:449`), de mailrelay verstuurt duizenden verificatiemails naar echte adressen van de organisatie (mail bombing en reputatieschade voor het afzenddomein), en Keycloak-beheerders moeten met de hand opruimen. Met `restrict_domain` leeg is elk adres toegestaan.
- **Aanbeveling**: rate-limit op `/invite/{key}` en `/invite/{key}/register` per key en per client (de bestaande `IPRateLimiter` hergebruiken); `max_uses` en `expires` op een invite met controle in `get_invite_by_key`; overweeg het aanmaken van de gebruiker pas na e-mailverificatie (Keycloak-registratielink) in plaats van vooraf met rollen.

### A-2 Invite-keys zijn zelf te kiezen vanaf 3 tekens en worden op INFO gelogd

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 5.17 (authenticatie-informatie), 8.15 (logging), 8.5 (beveiligde authenticatie)
- **Beschrijving**: een invite-key is een bearer-geheim: wie hem kent kan een account met rollen krijgen. De gegenereerde key heeft 128 bit, maar een projectbeheerder mag hem zelf kiezen en de validator accepteert alles vanaf 3 tekens. In combinatie met A-1 (geen throttling op de publieke lookup) is een korte key te raden. Daarnaast logt de SSO-start de key op INFO-niveau, dus elk logsysteem met leesrechten (Loki, Grafana) bevat werkende invite-links.
- **Bewijs**: `opi/services/catalog/invite/editables.py:53-57` "A self-chosen key is kept as-is"; `opi/forms/editables/validators.py:261` `if len(value_str) < 3 or len(value_str) > 64:`; `opi/api/invite_routes.py:449` `logger.info(f"Starting invite SSO flow for key '{key}', ...")`; `:219,222` loggen de key op DEBUG.
- **Aanvalsscenario**: een beheerder kiest `wies` of `2026` als key omdat dat mooi in de mail staat; een buitenstaander probeert woordenlijst-keys op `/invite/{key}` (200 met landingspagina versus 200 met "niet gevonden", triviaal te onderscheiden) en krijgt een account met de invite-rollen. Of: iemand met alleen log-leesrecht kopieert een key uit Loki en meldt zich aan.
- **Aanbeveling**: minimumlengte 16 en entropie-eis voor zelfgekozen keys, of zelfkiezen schrappen; key nooit loggen (hash of de eerste 4 tekens); een vaste 404 op onbekende keys, met throttling uit A-1.

### A-3 Sessies zijn stateless getekende cookies zonder intrekking en zonder absolute levensduur

- **Ernst**: Midden
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.5 (beveiligde authenticatie), 5.15 (toegangsbeveiliging); ASVS 3.3.1, 3.3.2
- **Beschrijving**: Starlette `SessionMiddleware` bewaart de sessie in de cookie zelf, getekend met `SECRET_KEY`. Uitloggen wist de cookie in de browser, maar een eerder gekopieerde cookie blijft geldig. `max_age` is een schuivend venster: de cookie wordt bij elk antwoord opnieuw getekend, dus een aanvaller die hem gebruikt houdt hem eindeloos in leven. Er is geen server-side sessieregister, geen absolute maximumduur, en de Keycloak-backchannel-logout die `features/keycloak-always-clear-session-logout.md` beschrijft bereikt OPI niet (geen ontvanger in de code: `grep backchannel opi` levert alleen de realm-instelling `backchannelSupported: "false"` in `connectors/keycloak.py:1177,1339`). De middleware toetst wel de allowlist per verzoek (sterke plek 15), dus verwijderen uit `ALLOWED_EMAILS` werkt; individueel uitloggen van één gestolen sessie kan niet.
- **Bewijs**: `opi/server.py:633-639` `SessionMiddleware, secret_key=..., max_age=settings.SESSION_MAX_AGE_SECONDS`; `opi/core/config.py:183-188` "The session cookie is re-signed on every response, so this acts as a sliding window"; `opi/api/auth_routes.py:193` `request.session.clear()` is het enige dat logout doet aan de OPI-kant.
- **Aanvalsscenario**: een sessiecookie lekt (malware, gedeelde werkplek, XSS elders op het domein). De gebruiker logt keurig uit en meldt het. De beheerder kan die ene sessie niet doden; de enige knop is `SECRET_KEY` roteren, wat iedereen uitlogt. Ondertussen houdt de aanvaller de cookie met een verzoek per uur onbeperkt geldig en heeft toegang tot alle projecten van het slachtoffer, inclusief API-keys en secrets die de UI toont.
- **Aanbeveling**: voeg een absolute levensduur toe (bijvoorbeeld `issued_at` in de sessie, weigeren na 12 uur) en een server-side revocatielijst op `sub` plus `issued_at` (in Redis, die al aanwezig is) die bij logout en bij een admin-actie gevuld wordt; overweeg `sid` uit het id_token op te slaan zodat Keycloak-backchannel-logout wel verwerkt kan worden.

### A-4 Proxy-headers van iedereen vertrouwd; rate-limiter leest het linker XFF-adres

- **Ernst**: Laag
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.20 (netwerkbeveiliging), 8.26
- **Beschrijving**: `ProxyHeadersMiddleware(trusted_hosts=["*"])` accepteert `X-Forwarded-For` en `X-Forwarded-Proto` van elke afzender. De `IPRateLimiter` neemt het linker adres uit XFF als client-IP, en dat is precies het deel dat de client zelf meestuurt (de ingress voegt het echte adres rechts toe; zie ook het geheugenitem over XFF). De fingerprint-laag helpt niet: die bestaat uit headers die de aanvaller zelf kiest.
- **Bewijs**: `opi/server.py:645` `app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])`; `opi/api/router.py:151-162` `forwarded_for.split(",")[0].strip()` "Take the first IP (original client)"; `:127-141` fingerprint uit User-Agent, Accept-Language, Accept-Encoding.
- **Aanvalsscenario**: een aanvaller roteert `X-Forwarded-For` per verzoek en omzeilt de subdomein-check-limiter volledig; als A-1 met dezelfde limiter wordt opgelost, is die dan meteen omzeilbaar. Via `X-Forwarded-Proto: http` kan een aanvaller voor eigen verzoeken HSTS en de `secure`-vlag van de CSRF-cookie uitzetten, wat alleen zichzelf raakt.
- **Aanbeveling**: `trusted_hosts` beperken tot de pod-CIDR van de ingress; in `get_client_ip` het rechter adres nemen dat de eigen proxy toevoegde (`X-Forwarded-For` één van rechts), of het adres uit de ingress-specifieke header.

### A-5 CSP staat `'unsafe-inline'` toe voor scripts en stijlen

- **Ernst**: Laag
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.26, 8.28 (veilig coderen)
- **Beschrijving**: met `script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net` biedt de CSP geen enkele bescherming tegen XSS: elke injectie mag inline uitvoeren. Concreet betekent dit dat sterke plek 14 (autoescape en discipline rond `|safe`) de enige verdedigingslinie is; één vergeten escape in een van de ~950 regels renderer of in een nieuw widgetsjabloon is direct uitvoerbaar. `cdn.jsdelivr.net` als hele host voegt daar niets aan toe of af zolang inline al mag, maar is ook een bekende CSP-bypass-host als inline ooit wordt dichtgezet.
- **Bewijs**: `opi/middleware/security_headers.py:56-57`; de motivatie staat erboven op `:48-49`: "HTMX event handlers are inline; 'unsafe-inline' required".
- **Aanvalsscenario**: geen zelfstandig pad; het is de reden dat een toekomstige XSS (bijvoorbeeld via een gebruikersveld dat niet door `_summary_text` gaat) volledige sessieovername oplevert in plaats van een geblokkeerde inline-uitvoering.
- **Aanbeveling**: nonce-based CSP (`'nonce-...'` plus `'strict-dynamic'`), HTMX-configuratie `htmx.config.inlineScriptNonce`, en Chart.js zelf hosten zodat `cdn.jsdelivr.net` kan vervallen; `style-src` kan `'unsafe-inline'` waarschijnlijk niet kwijt vanwege de componentbibliotheek, en dat is aanvaardbaar.

### A-6 `DEBUG_MODE` valt standaard op `reload`; `debug` opent een ongeauthenticeerde debugger op 0.0.0.0

- **Ernst**: Laag (Kritiek als de productiewaarde niet `production` is; zie open vragen)
- **Zekerheid**: BEVESTIGD voor de code, AANNEMELIJK voor productie
- **BIO2**: 8.9 (configuratiebeheer), 8.31 (scheiding ontwikkel/productie)
- **Beschrijving**: de container start met `exec python -m opi.server`, dus het `__main__`-blok kiest de modus. De default `DEBUG_MODE = "reload"` zet uvicorn met bestandsbewaking aan; `"debug"` start `debugpy.listen(("0.0.0.0", 5678))` en wacht op een client zonder enige authenticatie, en poort 5678 staat in de image ge-exposed. Productie zet `DEBUG_MODE` via het SOPS-secret, dus de waarde is uit de repo niet te lezen. Fail-open default: wie de variabele vergeet krijgt hot-reload in productie.
- **Bewijs**: `opi/core/config.py:191` `DEBUG_MODE: str = "reload"`; `opi/server.py:763-784`; `operations-manager/docker-entrypoint.sh:161` `exec python -m opi.server`; `operations-manager/Dockerfile:144` `EXPOSE 5678`; `bootstrap/.../odcn-production/operations-manager-env-secrets.yaml` bevat de sleutel `DEBUG_MODE` (waarde versleuteld).
- **Aanvalsscenario**: bij `debug` in productie kan elke pod in het cluster die poort 5678 van de OPI-pod bereikt (de NetworkPolicy niet gecontroleerd) willekeurige Python uitvoeren in het proces dat de AGE-sleutel, alle projectsecrets en de Keycloak-adminclient in handen heeft. Bij `reload` is het effect beperkt tot instabiliteit en een schrijfbare-volume-naar-code pad.
- **Aanbeveling**: default `"production"`; `debugpy` alleen binden op `127.0.0.1` en alleen buiten `ENVIRONMENT != "local"` weigeren; `EXPOSE 5678` uit de productie-image.

### A-7 OpenAPI en Swagger-UI zijn zonder authenticatie bereikbaar

- **Ernst**: Laag
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.3 (beperking toegang tot informatie), 8.26
- **Beschrijving**: `/docs`, `/redoc` en `/openapi.json` zijn FastAPI-routes zonder `_requires_sso`, dus de middleware laat ze door (default `False` voor gematchte routes). Het document beschrijft alle 110 API-operaties, parameters, foutcodes, de header-namen en cURL-voorbeelden. Hetzelfde geldt voor `/version` en voor `/lotc/pagina/architecture-overview` (publieke architectuurpagina, zie A-11).
- **Bewijs**: `opi/server.py:413-415` `docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json"`; `opi/middleware/authorization.py:150` `requires_sso = getattr(endpoint, "_requires_sso", False)`; de OpenAPI-routes komen niet voor in de AST-inventaris omdat FastAPI ze zelf registreert, en niets in `server.py` zet er een vlag op.
- **Aanvalsscenario**: verkenning. Een aanvaller leert zonder account de volledige API, welke admin-endpoints bestaan en welke header een sleutel verwacht. Geen directe toegang, wel een kaart.
- **Aanbeveling**: `/docs` en `/openapi.json` achter `requires_sso` (een eigen route die `get_openapi` serveert), of alleen op een intern hostname aanbieden.

### A-8 Geen PKCE op de hoofd-OIDC-flow

- **Ernst**: Laag
- **Zekerheid**: BEVESTIGD voor het ontbreken van PKCE; AANNEMELIJK voor `state` en `nonce` (authlib-default, authlib zelf niet gelezen)
- **BIO2**: 8.5, 8.24; NL GOV OpenID Connect-profiel vereist PKCE ook voor confidential clients
- **Beschrijving**: de registratie van de Keycloak-client geeft alleen `scope` mee; `code_challenge_method` ontbreekt, dus authlib vraagt geen PKCE aan. De client is confidential met een secret, dus de authorization code is zonder secret niet in te wisselen; PKCE is hier defense-in-depth (code-injectie via een gelekte redirect) en een profiel-eis, geen open gat. De invite-flow doet het wel goed (`invite_routes.py:99`).
- **Bewijs**: `opi/core/startup.py:382-390` `oauth.register(name="keycloak", ..., client_kwargs={"scope": "openid profile email"})`.
- **Aanvalsscenario**: authorization-code-injectie: een aanvaller die een code van het slachtoffer onderschept (referer-lek, logging bij een proxy) en in zijn eigen sessie inwisselt, bindt het account van het slachtoffer aan zijn browser. Zonder PKCE houdt alleen `state` (per sessie) dit tegen.
- **Aanbeveling**: `client_kwargs={"scope": ..., "code_challenge_method": "S256"}`; in Keycloak op de client `pkce.code.challenge.method=S256` afdwingen.

### A-9 ArgoCD-verbinding zonder TLS, met standaard admin-wachtwoord in de defaults

- **Ernst**: Laag
- **Zekerheid**: BEVESTIGD voor de configuratie; het connector-pad zelf valt buiten deel A
- **BIO2**: 8.20, 8.24, 8.9
- **Beschrijving**: `ARGOCD_USE_TLS=False` en `ARGOCD_VERIFY_SSL=False` zijn de defaults en de prod-overlay laat ze staan, dus het ArgoCD-admin-wachtwoord gaat in platte tekst over het clusternetwerk naar `argocd-server:80`. De defaults `ARGOCD_USERNAME="admin"`, `ARGOCD_PASSWORD="admin"` staan in de bron; productie overschrijft het wachtwoord via het secret.
- **Bewijs**: `opi/core/config.py:250-255`; `overlays/odcn-production/configmap.yaml:59-60` zet alleen host en poort.
- **Aanvalsscenario**: een workload met netwerktoegang in de OPI-namespace die verkeer kan meelezen (node-compromise, CNI-fout) leest het ArgoCD-admin-wachtwoord en krijgt daarmee schrijfrecht op elke applicatie in het cluster.
- **Aanbeveling**: ArgoCD-server met TLS benaderen (`ARGOCD_USE_TLS=true`, `ARGOCD_VERIFY_SSL=true` met de cluster-CA), of mTLS via de service mesh als die er is; defaults op `None` zodat een vergeten secret faalt.

### A-10 Invite-SSO-pad zonder `email_verified` en met binding op bestaand realm-account via e-mail

- **Ernst**: Laag
- **Zekerheid**: AANNEMELIJK (hangt af van de first-broker-login-flow van projectrealms, niet in deze repo)
- **BIO2**: 5.16, 8.5
- **Beschrijving**: de code kiest bewust geen `email_verified`-controle in `complete_sso_invite` en legt in een commentaar uit waarom: het pad geeft geen OPI-toegang, alleen rollen in het projectrealm, en de applicatie achter de wall dwingt `email_verified` af (sterke plek 1). Dat klopt voor de wall. Het pad bindt echter wel op `get_user_by_email` en kent daarna rollen toe. Gecombineerd met A-1 ontstaat pre-registratie: wie via een lokale invite `directeur@domein.nl` registreert (onbevestigd), bezit het realm-account met die e-mail vóór de echte directeur via SSO binnenkomt; wat er dan gebeurt (koppelen, weigeren, dubbel account) bepaalt de first-broker-login-flow van het realm.
- **Bewijs**: `opi/manager/invite_manager.py:295-304` "Intentionally no email_verified check here"; `:312-316` `existing_user = await keycloak.get_user_by_email(realm_name, email)` gevolgd door hergebruik van `user_id`; `:427-435` lokale aanmaak met `emailVerified=False`.
- **Aanvalsscenario**: zoals hierboven; effect is afhankelijk van realmconfiguratie en daarom Laag met voorbehoud.
- **Aanbeveling**: in de projectrealm-blueprint de first-broker-login-flow zo zetten dat een bestaand onbevestigd lokaal account nooit stil aan een IdP-identiteit gekoppeld wordt; op het SSO-invite-pad ten minste loggen wanneer aan een bestaand account met `emailVerified=false` rollen worden toegekend.

### A-11 Publieke LOTC-proefopstelling draait mee in de productie-image

- **Ernst**: Laag
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.31 (scheiding ontwikkel/test/productie), 8.3
- **Beschrijving**: alle routes onder `/lotc/` zijn publiek, staan onvoorwaardelijk in `server.py:688` en tonen beheerdersschermen (dashboard, gebruikersbeheer, goedkeuringen, gedeelde diensten, architectuuroverzicht) met fixture-data en een verzonnen `PREVIEW_USER`. De code zegt zelf dat dit op straat ligt en dat alleen verzonnen waarden erin mogen. Voor productie is het een onnodig aanvalsoppervlak (elke template die `base_lotc.html.j2` extendt is renderbaar), een informatiebron over interne structuur en menu's, en een verwarringsrisico (pagina's die eruitzien als ingelogd op het productiedomein).
- **Bewijs**: `opi/web/lotc_router.py:8-14` "Deze router is PUBLIEK ... geen enkele route hier draagt `requires_sso`"; `:35` `PREVIEW_USER`; `:74` `user = user or PREVIEW_USER`; `opi/server.py:688` `app.include_router(lotc_web_router, ...)` zonder voorwaarde.
- **Aanvalsscenario**: verkenning en social engineering ("kijk, ik zie het beheerdersdashboard"). Geen directe toegang tot echte data zolang de discipline uit het commentaar standhoudt; die discipline is niet afgedwongen.
- **Aanbeveling**: router alleen registreren als `ENVIRONMENT == "local"` of achter `requires_sso` plus platform-admin.

### A-12 Restanten: `/auth/user` debug-endpoint en logout via GET

- **Ernst**: Info
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.26, 8.28
- **Beschrijving**: `/auth/user` retourneert de eigen sessiegebruiker als JSON en heeft volgens de TODO geen aanroepers. `/auth/logout` is een GET zonder CSRF (`/auth/` is exempt), dus een derde site kan een gebruiker uitloggen (forced logout) via een `<img src>`.
- **Bewijs**: `opi/api/auth_routes.py:220-243` "TODO: probably remove. Unused manual debug endpoint"; `:165` `@auth_router.get("/logout")`; `opi/utils/csrf.py:52-56` `/auth/` in `CSRF_EXEMPT_PREFIXES`.
- **Aanvalsscenario**: hinder; geen gegevensverlies.
- **Aanbeveling**: `/auth/user` verwijderen; logout als POST met CSRF-token, of een bevestigingspagina.

### A-13 Hardcoded ontwikkelsleutels in bron en sandbox-overlay

- **Ernst**: Info
- **Zekerheid**: BEVESTIGD
- **BIO2**: 5.17, 8.9
- **Beschrijving**: `API_TOKEN` staat als hardcoded default in de bron en wordt alleen gebruikt als `USE_UNSAFE_API_KEY=True`; dan delen alle projecten dezelfde sleutel en logt de code hem op DEBUG. De sandbox-overlay zet vaste `SECRET_KEY` en `ADMIN_API_KEY` in een ConfigMap. Productie zet `USE_UNSAFE_API_KEY=false` en de andere waarden niet. Geen productierisico; wel een sleutel die in git staat en in een kopie-plak-ongeluk kan eindigen.
- **Bewijs**: `opi/core/config.py:261-262`; `opi/utils/api_keys.py:37-39` `logger.debug(f"Using unsafe API key from settings: {api_key}")`; `overlays/sandboxed-local/configmap.yaml:14,18`; `overlays/odcn-production/configmap.yaml:63` `USE_UNSAFE_API_KEY=false`.
- **Aanbeveling**: `API_TOKEN` default `None` en genereren bij `USE_UNSAFE_API_KEY`; het loggen van de sleutel schrappen.

### A-14 Sessiecookie draagt het id_token; één bearer-token levert alle beheerde project-API-keys

- **Ernst**: Info
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.24, 5.17
- **Beschrijving**: de Starlette-sessiecookie is getekend maar niet versleuteld; het id_token gaat erin voor de Keycloak-logout. Dat is de eigen gebruiker zijn eigen token, dus geen lek, maar het vergroot de cookie (id_tokens met groepen halen 4 KB) en zet een token in browseropslag dat daar niet hoeft te staan. Los daarvan geeft `GET /api/v2/projects` met een SSO-access-token de API-key van elk project dat de aanroeper beheert: een gelekt access token (kort geldig) levert langlevende projectsleutels op.
- **Bewijs**: `opi/api/auth_routes.py:137-138` `request.session["id_token"] = token["id_token"]`; `opi/api/v2/router.py:1134-1136` "List the projects this caller may see, with the API key of the ones they administer."
- **Aanbeveling**: alleen `sid` of een hash van het id_token bewaren; overweeg de API-key niet in de lijst maar per project achter een aparte, gelogde aanroep terug te geven.

### A-15 Authorization-wall-banner ongevalideerd naar oauth2-proxy `SignInMessage`

- **Ernst**: Info
- **Zekerheid**: AANNEMELIJK (oauth2-proxy rendert `SignInMessage` als `template.HTML`; bron niet gelezen)
- **BIO2**: 8.28
- **Beschrijving**: de bannertekst is een vrij veld zonder validator en landt via `--banner` in de aangepaste inlogpagina van de wall. Als oauth2-proxy die als HTML rendert, kan een projectbeheerder HTML en script op de inlogpagina van zijn eigen project zetten. De beheerder controleert de applicatie erachter toch al, dus geen privilege-escalatie; wel een plek waar de wall zijn eigen integriteit uit handen geeft.
- **Bewijs**: `opi/services/catalog/authorization_wall/editables.py:10-18` alleen `EmptyToNoneConverter`, geen validator; `manifests/sidecar-authorization-wall.yaml.jinja:20-22` `--banner=`; `:174-175` `<p>{{ .SignInMessage }}</p>`.
- **Aanbeveling**: een validator die HTML-tekens weigert, of de banner als platte tekst in de eigen template zetten.

### A-16 Niet-timing-safe sleutelvergelijking in dode code

- **Ernst**: Info
- **Zekerheid**: BEVESTIGD
- **BIO2**: 8.28
- **Beschrijving**: `get_project_by_api_key` vergelijkt met `==` over alle projecten. Geen aanroepers gevonden buiten de `ProjectStore`-doorgeefluik; alle echte paden gebruiken `secrets.compare_digest`. Vermelden zodat het niet opnieuw in gebruik komt.
- **Bewijs**: `opi/services/project_service.py:120` `if project.api_key == api_key:`; `grep get_by_api_key` levert alleen `project_store.py:172,330`.
- **Aanbeveling**: verwijderen of op `compare_digest` zetten.

## Tabellen

### CSRF-dekking

De afdwinging is centraal (`CSRFMiddleware`, `opi/utils/csrf.py:90-96`), dus de vraag is niet "welke route heeft de decorator" maar "welke route valt buiten de middleware". De routeinventaris (AST, 180 routes) is hieronder samengevat per klasse.

| Klasse | Onveilige methoden | Sessie-auth? | CSRF-controle | Beoordeling |
|---|---|---|---|---|
| `/api/**` met `validate_api_token` (55), `validate_admin_api_key` (7), `validate_master_api_key` (3), `validate_user_token` (2) | POST/PUT/DELETE | Nee, header-auth | Exempt (`/api/` prefix) | Correct: geen cookie, geen CSRF |
| `/api/tasks/{id}/:cancel`, `/api/tasks` (inline API-key-check) | POST | Nee | Exempt | Correct |
| `/api/logs/pods/{project_name}` | GET | Ja (sessie, `logs_router.py:253-260`) | n.v.t. (GET) | Correct; enige sessie-route onder `/api/` |
| `/api/logs/stream/{project_name}` (WebSocket) | handshake | Ja | Eigen Origin-check (`logs_websocket_router.py:78-118`) | Correct |
| `/auth/login`, `/auth/callback`, `/auth/logout`, `/auth/user` | alleen GET | Ja | Exempt (`/auth/` prefix) | `/auth/logout` GET zonder CSRF: forced logout (A-12) |
| `/invite/{key}/register` | POST | Nee (publiek) | Afgedwongen (niet exempt) | Correct; formulier levert `csrf_token` |
| Overige `/invite/**` | GET | Nee | n.v.t. | Correct |
| Web-routes met `requires_sso` (75, waarvan 31 POST/PUT/DELETE onder `/projects/**`, `/admin/**`, `/dashboard/**`, `/approvals/**`, `/ui/**`) | POST/PUT/DELETE | Ja | Afgedwongen (double-submit + Origin) | Correct |
| Publieke web-routes (`/`, `/introductie`, `/about`, `/eigen-domein`, `/permission-denied`, `/weergave`, `/projects/new`, `/lotc/**`) | alleen GET | Nee | n.v.t. | `/weergave` zet een cookie via GET (weergavevoorkeur, geen risico) |
| Probes en statisch (`/healthz`, `/readyz`, `/version`, `/metrics`, `/static/**`, `/favicon.ico`, `/.well-known/security.txt`) | GET | Nee | Exempt exact/prefix | Correct |

Geen enkele POST/PUT/DELETE buiten `/api/` en `/auth/` ontsnapt aan de middleware. HTMX: het token komt server-side in `hx-headers` (`csrf.py:76-88`), de cookie is `httponly` en `samesite=strict` (`:110-112`).

### Configuratievlaggen (`opi/core/config.py`) en wat productie ermee doet

| Vlag | Regel | Default | Effect als verzwakt | odcn-production |
|---|---|---|---|---|
| `DEBUG` | 181 | `False` | `https_only=False` op de sessiecookie, FastAPI-debugpagina's, localhost-CSRF-bypass | niet gezet (default) |
| `DEBUG_MODE` | 191 | `"reload"` | `reload`: hot-reload; `debug`: debugpy op 0.0.0.0:5678 zonder auth | in SOPS-secret, waarde niet leesbaar (A-6) |
| `SECRET_KEY` | 179 | random per proces | te kort: weigert start (`secret_key.py:59`) | in SOPS-secret |
| `SESSION_MAX_AGE_SECONDS` | 188 | 28800 | schuivend venster, geen absolute grens (A-3) | default |
| `USE_UNSAFE_API_KEY` | 262 | `False` | alle projecten delen `API_TOKEN` | `false` (configmap:63) |
| `API_TOKEN` | 261 | hardcoded | zie boven | niet gezet |
| `ADMIN_API_KEY` | 264 | `None` | `None` = admin-endpoints 501 | niet gezet: endpoints uit |
| `MASTER_API_KEY` | 263 | `None` | `None` = master-endpoints 501 | niet gezet: endpoints uit |
| `ALLOWED_EMAILS` / `ADMIN_EMAILS` | 216-217 | `None` | leeg = niemand mag erin (allowlist faalt gesloten) | 6 adressen / 1 admin |
| `ALLOW_PROJECTFILES_OVERWRITE` | 195 | `False` | overschrijven bestaande projectbestanden | niet gezet |
| `RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE` | 196 | `False` | wachtwoorden opnieuw zetten bij authfout | in SOPS-secret, waarde niet leesbaar |
| `SKIP_STARTUP_CHECKS` | 199 | `False` | slaat namespace/Keycloak/MinIO-checks over | niet gezet |
| `ARGOCD_USE_TLS` / `ARGOCD_VERIFY_SSL` | 254-255 | `False` / `False` | platte HTTP naar ArgoCD, geen certcontrole | niet gezet (A-9) |
| `ARGOCD_USERNAME` / `ARGOCD_PASSWORD` | 252-253 | `admin` / `admin` | standaardwachtwoord | wachtwoord in SOPS-secret |
| `REGISTRY_VERIFY_TLS` | (image-proxy) | zie deel B | geen certcontrole naar registry | `True` (configmap:76) |
| `KEYCLOAK_ENFORCE_ADMIN_OTP` | ~318 | `False` | geen OTP op project-realm-admins | `true` (configmap:54) |
| `CLI_TOKEN_AUDIENCE` | 210 | `"zad-api"` | audience voor bearer-tokens | default |
| `ENABLE_TRACEMALLOC` | 279 | `False` | geheugenprofilering, geen beveiligingsimpact | `false` |

## Open vragen

1. **Waarde van `DEBUG_MODE` in productie.** Staat in het SOPS-secret; zonder de AGE-sleutel niet te lezen. Als hij niet `production` is, schuift A-6 naar Hoog of Kritiek. Te verifiëren met `kubectl -n rig-prd-operations exec deployment/operations-manager -- env | grep DEBUG_MODE` (read-only, viel buiten mijn spelregels).
2. **authlib `state` en `nonce`.** Voor de hoofd-OIDC-flow leun ik op authlib's default (state in de sessie, nonce bij scope `openid`). De authlib-bron heb ik niet gelezen.
3. **oauth2-proxy `SignInMessage`.** Of het veld als HTML wordt gerenderd (A-15) heb ik niet in de oauth2-proxy-bron gecontroleerd.
4. **First-broker-login-flow van projectrealms.** Bepaalt of het pre-registratiescenario in A-10 tot koppeling leidt of tot een weigering. Staat in de realm-blueprint van de Keycloak-manager, niet in de hier gelezen code.
5. **Redirect-URI-lijst van de OPI-client in Keycloak.** `redirect_uri` wordt uit de `Host`-header gebouwd (`auth_routes.py:49`). Keycloak hoort een niet-geregistreerde URI te weigeren; of de lijst strak is (alleen `zad.rijksapp.nl` en `ADDITIONAL_DOMAINS`) heb ik niet kunnen zien.
6. **Overschrijft de ingress `X-Forwarded-For`?** Volgens het geheugenitem voegt de router het clientadres rechts toe (append). Dan is A-4 een bypass; bij overschrijven is het alleen een onjuiste keuze in de code.
7. **NetworkPolicy op poort 5678.** Bepaalt de reikwijdte van A-6 in de `debug`-stand; `overlays/odcn-production/network-policy.yaml` niet gelezen.
8. **Volledigheid van de XSS-toets.** `opi/forms/renderer.py` (950+ regels) en de widgetsjablonen zijn niet per veld nagelopen. De conventie (autoescape plus `_summary_text`) is sterk, maar door A-5 is één misser voldoende voor sessieovername; een gerichte fuzz met `<script>` in elk formulierveld van de wizard is de snelste manier om dit af te dekken.
9. **Keycloak-tokenlevensduur** voor access tokens met audience `zad-api` (relevant voor A-14) heb ik niet uit de code kunnen halen.
