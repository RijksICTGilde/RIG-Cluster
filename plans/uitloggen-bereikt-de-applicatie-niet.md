# Uitloggen bereikt de applicatie niet: een logout-pad per component

Keycloak doet bij uitloggen precies wat het moet, maar geen enkele applicatie hoort het ooit. Dat is op 31 augustus en 2 september 2026 op productie gemeten, en deze taak sluit die laatste schakel.

## Wat er gemeten is

Eén klik op uitloggen bij `docs.rijksapp.nl`, gevolgd door de sessielijst uit de admin-API (niet uit de cookie, zie de valkuil onderaan):

| sessie | voor | na |
|---|---|---|
| `mb-docs-helmfile-odcn-production` | c6aabaf5 | **weg** |
| `rig-platform` | 99968afc | **weg** |
| SSO Rijk (BZK) | blijft | blijft |

De propagatie binnen Keycloak werkt dus al, over realms heen, en de `always-clear-session-logout`-shim doet zijn werk (`Always-clear logout for user ... session '95168620...' via IdP 'sso-rijk'` in het Keycloak-log, exact de sessie uit de nulmeting). De BZK-sessie kunnen we niet wissen, en daarvoor is `forceAuthn: "true"` op de `sso-rijk` IdP de compensatie (`opi/configs/keycloak/bootstrap.yaml:57`): hun sessie mag blijven leven, maar de volgende login vraagt opnieuw om inloggegevens.

**Het gat zit in de applicatie.** Docs stuurt de browser naar het end-session endpoint en rekent erop dat die via `post_logout_redirect_uri` terugkomt om de eigen sessie te wissen. De shim laat de keten eindigen op de BZK-uitlogpagina, dus die terugreis komt nooit. De app houdt haar cookie, toont je als ingelogd, en raadpleegt Keycloak niet meer (vandaar ook geen SSO Rijk organisatiescherm). De tweede klik ruimt de app-sessie alsnog op en levert `LOGOUT_ERROR ... error="session_expired"` op, want de Keycloak-sessie was allang weg. Dat is geen storing maar de handtekening van die tweede klik.

Gemeten in een lokaal harnas (Keycloak 25.0.6, dezelfde shim-JAR v1.1.0): bij klik 1 eindigt de browser op de doodlopende pagina, bij klik 2 op de `post_logout_redirect_uri`. Het enige verschil is de terugreis.

**ZAD heeft er geen last van** omdat `opi/api/auth_routes.py:186` `request.session.clear()` doet vóór de redirect.

## Het ontbrekende veld

Uit de productiedatabase, de applicatieclients in de realms van docs, wies en operations-manager:

```
mb-docs-helmfile-production        frontchannel.logout.url (LEEG)   backchannel.logout.url (leeg)
wies-main                          (LEEG)                           (leeg)
rig-platform-operations-manager    (LEEG)                           (leeg)
... 17 clients, alle 17 leeg in beide kolommen
```

Terwijl docs er klaar voor is: `GET https://docs.rijksapp.nl/api/v1.0/logout/` geeft een 302 naar de eigen homepage en ruimt de sessie op. Dat is precies het contract van een front-channel logout URL. De knop zit er, niemand drukt erop.

Front-channel, niet backchannel: backchannel is een server-naar-server POST met een `logout_token` JWT die de app moet valideren, en zo'n endpoint heeft docs niet. Voor de applicatie waar je op dat moment uitlogt is front-channel ook de juiste weg, want de browser is er toch.

## Wat er gebouwd wordt

### 1. Een relatief logout-pad op het component

In het projectbestand, op het `keycloak`-dienstblok, opt-in. Laat je het weg, dan verandert er niets.

```yaml
services:
  keycloak:
    config:
      logout-path: /api/v1.0/logout/
```

**Relatief, niet absoluut.** OPI rekent het uit naar de volledige URL, net zoals het dat met alle andere Keycloak-paden doet. Dan hoeft niemand een hostname in zijn projectbestand te zetten, en loopt het niet scheef als een subdomein wijzigt.

**Normaliseren, niet weigeren.** Alle vier deze invoeren moeten hetzelfde opleveren:

```yaml
logout-path: /api/v1.0/logout/
logout-path: api/v1.0/logout/
logout-path: /logout
logout-path: logout
```

Een ontbrekende of dubbele slash is geen gebruikersfout om een foutmelding over te geven, dat is iets om op te lossen. Normaliseer naar precies één leidende slash en laat een afsluitende slash staan zoals hij staat (docs heeft die nodig: `/api/v1.0/logout/` is een ander endpoint dan `/api/v1.0/logout`). Leg de gevallen vast in een parametrische test, inclusief lege invoer en alleen `/`.

Wat wél geweigerd wordt is een absolute URL (`https://...`), want dan verliest de gebruiker de reden dat dit veld relatief is. Dan hoort er een foutmelding die zegt wat er moet staan, niet alleen dat het fout is.

**Hergebruik de bestaande sanitizer.** `_sanitize_path_value` in `opi/handlers/project_file_handler.py:174` doet dit al voor component-paden, met `_SAFE_PATH_PATTERN` tegen injectie via nginx-configsnippets. Diezelfde bescherming hoort hier te gelden, want dit pad wordt een URL die Keycloak zelf ophaalt. Hij is nu module-privaat; moet hij daarvoor verhuizen naar `opi/utils/`, benoem dat in de PR in plaats van een tweede exemplaar ernaast te zetten. Kijk ook naar `PathValidator` in `opi/forms/editables/validators.py:335`: die weigert nu een pad zonder leidende slash, en dat is precies het gedrag dat hier niet gewenst is.

Raakpunten: `opi/services/catalog/keycloak/config_model.py` (typering en normalisatie), `editables.py` en `visualizers.py` (het veld in de portal), en `keycloak.v1.0.json` meeschrijven. Zie `instructions/services.md` voor de haken.

### 2. Het pad wordt een clientattribuut

`frontchannel.logout.url` is een attribuut **per client**, en dat valt één-op-één samen met het model hier: één client per applicatie (`client_id = f"{project_name}-{deployment_name}"`, `opi/connectors/keycloak.py:684`).

In `create_deployment_client` (`:661`) is de host al bekend; daar wordt nu al `https://{host}/*` als redirect-URI gebouwd. Op datzelfde punt het logout-pad eraan plakken en als attribuut zetten. `frontchannelLogout: True` staat daar al hard op `:735`, dus dat klopt dan ineens ook in plaats van een vlag te zijn die niets kan.

Bij meerdere ingress-hosts moet er één gekozen worden, want Keycloak accepteert één front-channel URL per client. Neem de eerste: de app ruimt zijn sessie serverside op, dus welke van zijn eigen hostnames je belt maakt niet uit.

**Let op de 409-tak.** `create_deployment_client` trekt bij een bestaande client alleen `redirectUris`, `webOrigins` en `post.logout.redirect.uris` gelijk (`:766` en verder). Zonder uitbreiding krijgt geen enkele bestaande client dit attribuut ooit, en dat is precies de fout die hieronder bij punt 5 als losse drift terugkomt.

### 3. De fan-out moet daadwerkelijk afgevuurd worden

Dit is het open stuk, en het is de reden dat punt 1 en 2 alleen niet genoeg zijn.

Front-channel logout wordt gerenderd in `AuthenticationManager.finishBrowserLogout`. Onze shim roept `backchannelLogout` aan en stuurt daarna zelf een redirect, dus `finishBrowserLogout` draait nooit en er wordt niemand gebeld.

**Een prototype dat dit naïef omdraait is gemeten en is slechter.** `finishBrowserLogout` in plaats van `backchannelLogout` in de shim: de app-realms overleven dan juist (de backchannel-notificatie valt weg), BZK wordt niet meer bezocht, en het Keycloak-log geeft `LOGOUT_ERROR ... error="logout_failed", reason="There was an error during the local logout"` op beide app-realms. Dat prototype is bewust niet uitgerold. Zie `browserLogoutAllClients` in Keycloak 25.0.6: die doet beide mechanismen, maar slaat client-sessies over die al op `LOGGING_OUT` staan, en dáár botst het.

Niet begrepen en eerst uit te zoeken: waarom faalt óók het realm dat niet in de browserketen zit. Dat is een ordeningsvraag, niet een configuratievraag.

**Poort.** Blijkt de fan-out niet werkend te krijgen zonder de shim te verzwakken, dan stopt deze taak na punt 1, 2, 4 en 5 en wordt punt 3 een eigen taak. Lever dan de meting die het uitwijst. Punt 1 en 2 zijn op zichzelf al waardevol (het veld bestaat, de URL staat op de client), en punt 4 lost docs op zonder punt 3.

### 4. Het contract vastleggen

In `instructions/`: een applicatie op dit platform wist haar eigen sessie **vóór** ze naar het end-session endpoint stuurt, want de keten eindigt bij BZK en komt niet terug. Met `opi/api/auth_routes.py:186` als voorbeeld, en de shim als reden erbij zodat niemand die later "repareert".

Dit is de robuuste weg en hij werkt vandaag al: voor docs is het één regel in hun code. Punt 1 tot 3 zijn er voor applicaties die dit niet zelf kunnen.

Werk ook `features/keycloak-always-clear-session-logout.md` bij. Die zegt nu dat de shim voor "federated IdPs" is, en vertelt niet dat hij de terugreis onmogelijk maakt. Dat is precies de kennis die hier ontbrak.

### 5. De drift repareren

Zeven platform-clients in `rig-platform` hebben geen `backchannel.logout.url`: `bouwm-75c`, `bouwm-nr1`, `mb-docs-odcn`, `mb-grist-helmfile`, `mink1-3bz`, `test-xoa`, `testw-03o`. Die dateren van vóór 15-05-2026, toen de sleutel aan de blauwdruk werd toegevoegd (95949146), en niets repareert dat: `_ensure_idp_and_platform_client_configuration` (`opi/manager/keycloak_manager.py:1738`) trekt alleen `redirectUris` gelijk, nooit attributen.

Dus: attributen meenemen in die reconcile. Dan trekken de zeven bij op hun eerstvolgende verwerking, en krijgt het nieuwe veld uit punt 2 ook een leverweg naar bestaande clients.

## Succescriteria

1. `logout-path` accepteert `logout`, `/logout`, `api/v1.0/logout/` en `/api/v1.0/logout/` en levert in alle gevallen hetzelfde genormaliseerde pad met één leidende slash. Een absolute URL wordt geweigerd met een melding die zegt wat er wél moet staan. Verify: parametrische unit test op het config-model, inclusief lege invoer en alleen `/`.
2. Een component mét `logout-path` levert een client op met `frontchannel.logout.url = https://<host><pad>`, zonder dubbele slash op de naad. Verify: unit test op `create_deployment_client`, plus een e2e in de sandbox.
3. Een bestaande client krijgt het attribuut bij een volgende verwerking. Verify: test op de 409-tak met een client die het attribuut mist.
4. Keycloak roept die URL daadwerkelijk aan bij uitloggen. Verify: harnas-test met een stub-webserver die registreert welke paden binnenkomen (zie onder).
5. De zeven platform-clients zonder backchannel-URL trekken bij. Verify: de query onderaan geeft nul lege rijen.
6. `uv run ruff check . --fix`, `uv run ruff format .`, `uv run pyright` schoon.

## Het harnas

Bouw dit opnieuw op; het vorige stond in een scratchpad die is opgeruimd. Het is de reden dat we drie verkeerde oplossingen níét hebben uitgerold, dus het hoort deze keer in de repo, bij de e2e-tests, met een marker zodat het niet in de gewone run meedraait.

Opzet: een Keycloak 25.0.6 in docker met de shim-JAR eruit (`keycloak-migration/custom-mapper`, bouwt met `mvn -B clean package -DskipTests`), en drie wegwerprealms via de admin-API:

```
app   (speelt docs)  --OIDC--\
                              >--  plat (speelt rig-platform)  --SAML of OIDC-->  up (speelt BZK)
app2  (speelt wies)  --OIDC--/
```

Op `plat` wijst de IdP zijn logout naar de shim met een `downstreamLogoutUrl` die doodloopt op een stub, precies zoals productie naar BZK. Een stub-webserver registreert welke URL's de browser aanroept; dát is de meting voor succescriterium 4. Playwright loopt de inlogketen af (let op: de IdP-knop heeft voorrang boven het lokale inlogformulier, en de eerste federatie vraagt om profielvelden).

Verifieer dat het harnas eerst **rood** gaat zonder de wijziging. Een groene meting zonder een rode ervoor zegt niets.

## Valkuilen, duur geleerd

- **Een blijvende `KEYCLOAK_SESSION`-cookie bewijst niet dat de sessie leeft.** Alleen `finishBrowserLogout` wist die cookie, en die draait per ontwerp niet. Hierdoor heb ik een halve dag een niet-bestaand probleem onderzocht. Meet sessies via `/admin/realms/<realm>/users/<id>/sessions`.
- **Keycloak logt geslaagde logouts op DEBUG.** Stilte in het log betekent niet dat er niets gebeurd is. De faalhandtekeningen zijn `KC-SERVICES0057 Logout for client ... failed` (POST mislukt), `LogoutToken verification ... failed` (token afgewezen), `logout_failed` (botsing), `session_expired` (geweigerd vóór alles). Grep op de exacte tekst: "failed to logout" matcht niets.
- **Twee clients met bijna dezelfde naam.** `<project>-<cluster>-platform` in realm `rig-platform` is het app-realm als afnemer van het platform; `<project>-<deployment>` in het app-realm is de applicatie zelf. Die zijn we in het onderzoek herhaaldelijk verward.
- **De `-public` clients staan op `frontchannelLogout: true` met een lege URL**, wat ook backchannel blokkeert (`backchannelLogoutClientSession` stapt op `isFrontchannelLogout()` meteen uit). Ze worden nauwelijks gebruikt, dus geen onderdeel van deze taak, maar raak ze niet aan zonder te meten.
- **Niet aan `forceAuthn` komen.** Zolang de BZK-sessie niet te wissen is, is die vlag de enige afdwingbare vorm van "uitloggen betekent uitloggen". Hij kost wel het onthouden van de organisatiekeuze op het SSO Rijk-scherm (`prompt=login` propageert en tilt de vlag op; omlaag kan niet). Dat is een bewuste ruil, geen bug.

## Niet in deze taak

Echte single sign-out over meerdere applicaties. Dat vraagt backchannel-endpoints in de applicaties zelf (een eigenschap van de app, niet te configureren door ons) plus een oplossing voor de `logout_failed`-botsing tussen realms. Punt 3 hierboven is de voorwaarde die eerst moet staan.

## Handige commando's

De platform-clients en hun logout-attributen:

```sql
select c.client_id, c.frontchannel_logout,
       max(case when a.name='frontchannel.logout.url' then a.value end) as fc_url,
       max(case when a.name='backchannel.logout.url' then 'gezet' end) as bc_url
from client c
join realm r on r.id = c.realm_id
left join client_attributes a on a.client_id = c.id
where r.name = 'rig-platform' and c.client_id like '%-platform'
group by c.client_id, c.frontchannel_logout
order by c.client_id;
```

Draaien met `kubectl --context odcn-rig-production exec -n rig-prd-operations rig-db-1 -- psql -U postgres -d keycloak -c "..."`. Een admintoken voor de API komt uit `operations-manager-env-secrets`, sleutel `KEYCLOAK_ADMIN_PASSWORD`, gebruiker `admin` op realm `master` via `admin-cli`. Het wachtwoord in `keycloak-admin-credentials` werkt niet meer.

Keycloak-logs: `kubectl --context odcn-rig-production logs -n rig-prd-operations deployment/keycloak`. Ouder dan een paar uur via `scripts/grafana_loki_logs.py`.
