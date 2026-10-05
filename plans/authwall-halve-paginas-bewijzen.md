# De halve pagina's achter de authorization wall: bewijzen in plaats van beweren

**Status**: uitgevoerd in RC-240 (PR #213). De opstelling staat in `features/authorization-wall-meetopstelling.md`, de uitkomst in `features/authorization-wall.md`.
**Code bestaat al**: `claude/authwall-error-page`, commits `e51b5ad25` en `a10bdadfc`. Dit plan vraagt geen herbouw.
**Wat ontbrak**: tests die toetsen wat er werkelijk fout gaat en dat de fix dat wegneemt.

Twee punten hieronder zijn door die metingen weerlegd en blijven alleen staan als het contract dat ze opdroeg:

- **F2 heeft een andere oorzaak dan paragraaf 3 beschrijft.** Zonder `--cookie-refresh` vernieuwt oauth2-proxy helemaal niet (`refresh:disabled` in zijn eigen log) en de klif zit op `--cookie-expire`, niet op de vijf minuten van het access token. Keycloak roteert het refresh token niet (`revokeRefreshToken` op de default `false`, OPI zet er niets), dus de stormloop kan niet ontstaan en de parallelliteit is niet dragend. De vlag blijft overeind met een andere reden: hij laat de sessie meeschuiven.
- **De poort van F3 staat op WEERLEGD.** Splitsen gebeurt, al bij 80 realmrollen, maar een gesplitste sessie werkt. Conform punt 3 betekent dat: geen fix erbij, en het geval blijft open.

---

## 1. Waarom dit plan bestaat

De wijziging is gebouwd en de suite is groen, maar de tests toetsen tekenreeksen in een gegenereerd manifest:

```python
assert "--cookie-refresh=3m" in args
assert "--api-route=^/api/" in args
```

Die slagen ook als de fix niet werkt. Ze leggen vast dat een vlag in het sjabloon staat, niet dat een browser een werkende pagina krijgt. De enige echte verificatie tot nu toe is een loganalyse achteraf op `rig-prd-mpfm-w3h`, en die is niet herhaalbaar en niet geautomatiseerd. Daarmee staat er te veel op aanname, en een deel van het oorspronkelijke probleem is nog onverklaard.

Dit plan draait dat om: eerst een opstelling die de storing reproduceert, dan de fix ertegen aan.

## 2. Wat er al gebouwd is, en waarom

Zes vlaggen op de oauth2-proxy-sidecar in `manifests/sidecar-authorization-wall.yaml.jinja`:

| vlag | waarom |
|---|---|
| `--api-route=^/api/` | een `fetch()` hoort een 401 te krijgen, geen HTML-pagina |
| `--api-route=\.(css\|js\|...)$` | hetzelfde voor statische bestanden, op extensie omdat de paden per applicatie verschillen |
| `--cookie-refresh=3m` | vernieuwen vóór de grens; zonder deze vlag vernieuwt de proxy pas nadat de sessie verlopen is |
| `--cookie-expire=10h` | het cookie mag niet langer geldig beweren te zijn dan de Keycloak-sessie (projectrealms: 10 uur) |
| `--cookie-samesite=lax` | expliciet in plaats van de browser laten kiezen; strict kan niet, want de callback is een top-level cross-site redirect |
| `--cookie-csrf-per-request=true` | twee tabbladen die beide een inlogpoging starten overschrijven anders elkaars CSRF-cookie |

Buiten scope, bewust, en vastgelegd in `features/futures/authorization-wall-rolcontrole-in-de-proxy.md`: het tussenscherm van de muur (`--skip-provider-button` blijft `false`, want de banner van vijf projecten hangt eraan) en rolcontrole in de proxy zelf.

## 3. De drie storingen die bewezen moeten worden

Dit is het contract. Elke storing krijgt een test die hem reproduceert **zonder** de fix en aantoont dat hij verdwijnt **met** de fix. Een test die alleen de goede kant toetst is niet genoeg: dan weet je niet of je de storing ooit had.

### F1. Een stylesheet krijgt HTML terug

Wat er fout gaat: een sessieloze aanvraag voor `/bediening.css` wordt beantwoord met `403` en `Content-Type: text/html`, met de inlogpagina als body. De browser kan dat niet als CSS gebruiken, en met `X-Content-Type-Options: nosniff` wordt het geblokkeerd. Gevolg: een pagina zonder opmaak en consolefouten die niets over de oorzaak zeggen.

Dit is de storing waar de gebruiker "browsers die poep lieten zien" over schreef, en in de productielogs staan er 163 van, met 2313 tot 2315 bytes HTML per stuk.

Te toetsen:

- zonder `--api-route`: status 403, `Content-Type: text/html`, body bevat `<!DOCTYPE html>`
- met `--api-route`: status 401, body leeg of kort, `Content-Type` niet `text/html`
- een navigatie naar `/` blijft in beide gevallen de inlogkaart geven, want die hoort niet te veranderen

### F2. De vijfminutenklif en de refresh-stormloop

Wat er fout gaat: oauth2-proxy neemt de levensduur van het Keycloak access token over als sessie-expiry, en dat is vijf minuten. Zonder `--cookie-refresh` vernieuwt hij pas nadat de sessie verlopen is. Op dat moment komen alle aanvragen van één paginalading tegelijk aan op een dode sessie en bieden ze allemaal hetzelfde refresh token aan. Keycloak roteert dat token, dus er wint één en de rest krijgt een 403.

In de productielogs: 32 van de 47 geauthenticeerde page loads verloren hun stylesheet en scripts.

Te toetsen, met een access-token-levensduur van tien seconden zodat de test seconden duurt in plaats van minuten:

- inloggen, wachten tot voorbij de expiry, dan vijf aanvragen parallel vuren (document plus vier subresources, zoals een echte pagina)
- zonder `--cookie-refresh`: minstens één aanvraag faalt
- met `--cookie-refresh` korter dan de tokenlevensduur: alle vijf slagen, en er is geen nieuwe interactieve aanmelding nodig

De parallelliteit is de kern van deze test. Vijf aanvragen na elkaar vuren reproduceert de storing niet, want dan vernieuwt de eerste en profiteren de rest daarvan.

### F3. Het onverklaarde geval

Wat er fout gaat: binnen één geauthenticeerde paginalading haalt het document de sessie wel en halen de subresources die niet, soms negen seconden na een verse login en dus ruim binnen de vijf minuten. `--cookie-refresh` verklaart dit niet. oauth2-proxy logt geen enkele `Error loading cookied session`, dus het cookie wordt op die aanvragen niet meegestuurd en komt niet kapot aan.

Uitgesloten met bewijs uit de productielogs: browsercache (`Cache-Control: no-store` op zowel HTML als CSS), een kapotte router-pod (alle zes router-IP's leveren zowel 200 als 403), `crossorigin` op de tags (de HTML gebruikt kale relatieve verwijzingen) en een domeinfilter (`--email-domain=*`).

Hypothese: het sessiecookie wordt gesplitst in `_oauth2_proxy_0`, `_1`, `_2`, omdat de hele tokenset in het cookie zit en dat bij een gebruiker met veel claims niet in één cookie past. Dat `robbert.uittenbroek` nooit omvalt en `robbert.bos` en `kees.keulemans` wel, past daarbij.

**Dit is de belangrijkste winst van de opstelling**: hiermee is de hypothese te toetsen zonder dat er iemand in productie met devtools hoeft te zitten. Te toetsen:

- een gebruiker met weinig rollen en een gebruiker met zoveel realmrollen dat het token boven de 4 kB komt
- inloggen, de `Set-Cookie`-headers tellen: splitst oauth2-proxy in meerdere cookies
- daarna een paginalading aan aanvragen vuren en kijken of er aanvragen zonder geldige sessie aankomen

**Dit is een poort, geen stap.** Bevestigt de meting de hypothese, dan hoort er een fix bij en de kandidaten staan in `plans/authorization-wall-vervolg-sessieherstel.md` (`--session-cookie-minimal`, een Redis session store, of de claims in het token terugbrengen), elk met zijn eigen prijs. Weerlegt de meting hem, dan is de oorzaak nog onbekend en moet dit plan niet doen alsof het probleem opgelost is. In beide gevallen hoort de uitkomst in de PR-tekst, want de belofte "de halve pagina's zijn weg" is zonder F3 niet hard te maken.

## 4. De opstelling

Twee containers, geen sandbox en geen cluster nodig. Er is precedent: in de futures-doc staat een opstelling die op 25 augustus met echte containers is nagemeten, en die is het startpunt in plaats van iets nieuws.

- `quay.io/keycloak/keycloak:25.0.6`, dezelfde versie als productie, in dev-mode. De realm opbouwen met OPI's eigen connectorcode (`create_deployment_client` en wat daarbij hoort), niet met een handgeschreven import, zodat de test toetst wat OPI werkelijk aanmaakt.
- `quay.io/oauth2-proxy/oauth2-proxy:v7.7.1`, hetzelfde image als in het sjabloon, met `--upstream=static://200`.

Sluit aan op het huispatroon uit `tests/conftest.py` voor `zad-test-postgres`: een container met een vaste naam die blijft staan, plus Taskfile-doelen om hem te stoppen en te verversen. Dat ontwerp is daar beredeneerd ("wat een mens kan noemen, kan hij opruimen") en een tweede patroon ernaast zou een dubbeling zijn.

Twee dingen die uren kosten als je ze niet weet, beide uit de futures-doc:

- De proxy draait in een container en de testclient op de host, dus de issuer moet voor allebei dezelfde naam hebben. Zet `frontendUrl` als realm-attribuut, anders mint Keycloak tokens met de ene hostnaam terwijl de proxy de andere verwacht en krijg je `id token issued by a different provider`.
- De vlaggen uit het sjabloon moeten echt uit het sjabloon komen. Een test die de vlaggen opnieuw opschrijft toetst zijn eigen kopie, en dan lopen sjabloon en test uit elkaar. Render `sidecar-authorization-wall.yaml.jinja` en haal de args daaruit.

**Voorwaarde**: werkende Docker. Op de machine waar dit plan is geschreven hing Docker (`docker info` liep in een timeout en `docker inspect zad-test-postgres` ook), waardoor de bestaande Postgres-tests een ERROR gaven. Dat is omgevingsruis, geen testfout, maar het blokkeert deze opstelling wel. Zie `reference_docker_desktop_orphan_backend` voor de bekende oorzaak.

## 5. Wat er niet in deze tests hoort

Geen Playwright en geen echte browser. De storing zit in statuscodes en content-types, en die zijn met een HTTP-client te toetsen. Een browser erbij halen maakt de test traag en vals-rood zonder iets toe te voegen. De bestaande `tests/e2e/` met Playwright is er voor wizardstromen, niet hiervoor.

Geen test tegen het sandboxcluster. Deze opstelling heeft er niets aan nodig, en de sandbox is een gedeelde resource met een claim-mechanisme (`orch sandbox claim`).

## 6. Verificatie

Af als dit alle vier waar is:

- F1 heeft een test die zonder de fix rood is en met de fix groen, en die de `Content-Type` toetst en niet alleen de statuscode.
- F2 heeft een test met parallelle aanvragen over de expiry-grens, met een controlemeting zonder `--cookie-refresh` die faalt.
- F3 heeft een meting met een uitkomst, en die uitkomst staat in de PR-tekst. Bevestigd betekent een fix erbij; weerlegd betekent dat het open blijft en dat dit expliciet gezegd wordt.
- De tests halen hun vlaggen uit het gerenderde sjabloon en niet uit een eigen kopie.
