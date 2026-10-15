# Het echte client-IP achter de router

Status: ontwerp, niets gebouwd. Opgeschreven op 5 oktober 2026, gemeten op `350eb2adf` met uvicorn 0.40.0 (`operations-manager/python/uv.lock`). De verwachting bij het begin was dat dit een kleine codefix was. Dat is het niet: het vraagt een annotatie op de Ingress, een wijziging in het netwerkbeleid en daarna pas een opruiming in de code, en in die volgorde. Dit document bewaart de metingen zodat wie het oppakt niet opnieuw begint.

## Waarom dit er ligt

Een klant liep hier tegenaan en kon in zijn eigen applicatie een verzonnen client-IP doorgeven. Bij het natrekken bleek OPI hetzelfde gat te hebben. Vandaag kan elke pod in het cluster een verzonnen adres in het enige AUDIT-logregel van `opi/` laten schrijven dat een IP draagt, en kan elke externe aanroeper zijn eigen rate-limit-emmer kiezen.

Het is ook een voorwaarde voor ander werk. De `origin`-kolom uit BIO2 8.15.01 in `features/futures/gebeurtenissen-vastleggen-en-melden.md` kan niet gevuld worden zolang dit niet geregeld is, en dat document wijst het versmallen van het vertrouwen expliciet naar buiten de gebeurtenissencode.

## Wat er vandaag staat

De hele client-IP-oppervlakte van OPI is drie plekken:

| plek | wat er gebeurt |
|---|---|
| `opi/server.py:645` | `ProxyHeadersMiddleware(trusted_hosts=["*"])`, dus `always_trust`, dus de meest LINKSE waarde komt in `scope["client"]` |
| `opi/api/router.py:146` | `IPRateLimiter.get_client_ip` leest de header zelf met `.split(",")[0]` en komt pas bij `request.client` als de header ONTBREEKT |
| `opi/api/router.py:163` | de enige plek in heel `opi/` die `request.client` leest |

Twee aanroepers van `get_client_identifier` (`opi/web/router_self_service.py:68` en `opi/api/v2/router.py:626`) voeden de rate limiter, waar de spoofbaarheid is ingecalculeerd omdat er een fingerprint en een sessie bij komen. De derde aanroeper is `opi/api/v2/router.py:632`, en die schrijft de waarde in een AUDIT-logregel, waar zij als bewijs wordt gelezen. `sanitize_for_log` helpt daar niet: die weert loginjectie, niet spoofing.

Dezelfde middleware leidt ook het scheme af, en daar hangen twee dingen aan: HSTS in `opi/middleware/security_headers.py:72` en de `secure`-vlag op het CSRF-cookie in `opi/utils/csrf.py:112`. Wie `trusted_hosts` aanraakt raakt die twee dus mee, en dat is de reden dat dit niet als losse regel te wijzigen is.

## Wat de router werkelijk stuurt

Gemeten op 21 september 2026 in `rig-prd-test` met een probe die de ruwe headerregels dumpt. Bij `curl -H "X-Forwarded-For: 198.51.100.77"` ontvangt de pod:

```
x-forwarded-for: 198.51.100.77     <- door de client gestuurd, ongemoeid gelaten
x-forwarded-for: 147.181.15.230    <- door de router toegevoegd, de echte peer
```

De OpenShift-router staat op `forwardedHeaderPolicy: Append`, de default, en voegt zijn waarde toe als APARTE headerregel in plaats van met een komma. Dat is geen eigenaardigheid van ODCN: de router IS HAProxy, en `option forwardfor` zonder `if-none` doet precies dit. Hetzelfde geldt voor `forwarded`, `x-forwarded-proto` en `x-forwarded-host`. `x-real-ip` zet de router helemaal niet, dus die draagt nooit een echte waarde.

## Waarom uvicorn het bij ons per ongeluk goed doet

Twee regels is RFC-conform (RFC 9110 par. 5.3, RFC 7239 par. 4), maar bibliotheken lezen ze verschillend. Gemeten in onze eigen venv:

| leesweg | uitkomst bij twee regels |
|---|---|
| `dict(scope["headers"])`, wat `proxy_headers.py:35` doet | de LAATSTE regel, dus de router-waarde |
| `request.headers.get(...)` (Starlette) | de EERSTE regel, dus de waarde van de aanvaller |
| `request.headers.getlist(...)` | de volledige keten, dit is wat je nodig hebt |
| CGI-omgevingsvariabele | de laatste, en dit plette de eerste meting |

Daar zitten twee aparte dingen in. Het eerste is een echte bug: `dict()` gooit een dubbele headerregel weg, terwijl RFC 9110 par. 5.3 samenvoegen wel toestaat maar eist dat het "without changing the semantics" gebeurt. Het tweede is geen bug maar een footgun: met `trusted_hosts=["*"]` zet uvicorn `always_trust` (`proxy_headers.py:71`) en geeft `get_trusted_client_host` letterlijk `x_forwarded_for_hosts[0]` terug (`:132-133`), dus de meest linkse waarde van de regel die hij nog over heeft. Dat `"*"` leest als "zet aan achter een proxy" en in werkelijkheid betekent "laat de aanroeper zijn eigen adres kiezen".

Dat OPI toch het goede adres in `scope["client"]` krijgt, komt doordat die bug en die footgun elkaar bij HAProxy opheffen: `dict()` had de regel van de client al weggegooid, waarna links en rechts hetzelfde zijn. Gemeten verschil:

```
uvicorn "*" op twee regels (OpenShift Append) -> 147.181.15.230   correct
uvicorn "*" op een komma-lijst                -> 203.0.113.9      gespoofd
```

Zet er een komma-appendende proxy voor en dezelfde config overhandigt het door de aanvaller gekozen adres. De meest linkse waarde komt bij ons dus alleen niet boven doordat de header in twee regels aankomt, en dat is geen eigenschap waar je op wil bouwen.

## Er is geen standaard, en "de rechtse waarde" is geen regel

`X-Forwarded-For` is niet-geauthenticeerde tekst die iedereen op het pad erbij mag zetten. RFC 7239 zegt dat ook: de waarden zijn te vervalsen en de ontvanger moet zijn eigen topologie kennen. Het enige onbetwistbare gegeven in een verzoek is het TCP-peer-adres.

De vuistregel "de laatste in de rij is het echte IP" klopt bij HAProxy maar is geen HTTP-regel. GCP's Application Load Balancer appendt TWEE waarden, `CLIENT_IP, FORWARDING_RULE_IP`, dus daar is de laatste het adres van de load balancer en de client de op een na laatste. En belangrijker voor ons: de rechtse waarde is alleen iets waard als je WEET dat een vertrouwde proxy hem heeft toegevoegd. Het netwerkbeleid van OPI laat poort 8000 binnen met `from: []` (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/network-policy.yaml:12-16`), dus elke pod in het cluster komt rechtstreeks binnen. Daar heeft niemand iets toegevoegd en is de rechtse waarde net zo verzonnen als de linkse.

Elke stack lost dit op dezelfde manier op, namelijk door de peer te vertrouwen en een vast aantal hops van rechts te tellen. Envoy zet dat als knop neer (`xff_num_trusted_hops`, de N+1-e van rechts, default 0). Dat is de de-facto standaard: geen header waar het echte adres in staat, maar een getal of een lijst die jij moet aanleveren.

## Wat wies doet, en wat daarvan overdraagbaar is

Wies heeft dit opgelost met een hopteller in config: `settings.TRUSTED_PROXY_HOPS` (`wies/rijksauth/request_meta.py:57`), default 0, in prod 1 (`.env.prod.example:38`), en dan `parts[-hops]`. Het is dus net zo config-afhankelijk als een CIDR-lijst, alleen met een ander soort config.

Drie dingen daaruit zijn hier wel de moeite:

1. **Drie velden in plaats van een.** `ip` is de afgeleide waarde, `forwarded_for` de ruwe keten als bewijs (afgekapt op 512), en `remote_addr` het peer-adres met in de docstring de zin die het ontwerp draagt: "the only non-spoofable element and the tell-tale for traffic bypassing the ingress". Dat laatste veld is de bypass-detector die wij niet hebben. Dit is ook het antwoord op de zorg dat je anders "alles moet loggen": de ruwe keten hoort in een apart, afgekapt veld en nooit in een opgemaakte logregel.
2. **Poort strippen.** `_valid_ip` haalt een achterliggende poort eraf, dus `1.2.3.4:56789` en `[::1]:443` leveren nog een bruikbaar adres. Azure's Application Gateway emit dat formaat.
3. **Default die niets vertrouwt.** 0 hops in dev en test, zodat een aangeleverde header daar nooit wordt geeerd.

Wat NIET overdraagbaar is: wies heeft het dubbele-regel-probleem nooit gehad, en niet omdat ze het hebben opgelost. Gunicorn voegt herhaalde headerregels samen met een komma (`gunicorn/http/wsgi.py:201-203`, met de comment "do not change lightly, this is a common source of security problems"), want WSGI's environ is een dict met een key per headernaam en die samenvoeging is daar verplicht. ASGI heeft de ruwe lijst bewaard, wat correcter is, en heeft het probleem daarmee in elke bibliotheek gelegd. De normalisatie is in onze stack dus eigen werk.

## De oplossing zit in drie lagen, en de app is de laatste

**Laag 1, de router.** `haproxy.router.openshift.io/set-forwarded-headers: "replace"` op onze Ingress. De router gooit dan de header van de client weg en alleen zijn eigen waarde overleeft. Geen hopteller, geen CIDR-lijst, geen app-config, en "de enige waarde" is dan triviaal ook de rechtse. Dit is een per-route annotatie, en onze Ingress draagt al drie `haproxy.router.openshift.io/*`-annotaties (`ip_whitelist`, `timeout`, `hsts_header`), dus het mechanisme werkt daar aantoonbaar. De kennis over welke proxy te vertrouwen is verdwenen uit de app en staat in de route-definitie in git, naast de app, waar hij meeleest in een PR.

**Laag 2, het netwerk.** `replace` draait niet op het directe pod-pad, want daar komt de router er niet aan te pas. Versmal daarom het inkomende netwerkbeleid tot de ingress-namespace, zodat de router de enige ingang is. Dan is de header onvoorwaardelijk betrouwbaar en staat de vertrouwensgrens in de NetworkPolicy, wat de plek is waar hij werkelijk ligt. Lukt dat niet omdat er een in-cluster aanroeper blijkt te zijn, dan is de terugval een peer-controle in de app, met de ingress-adressen in config, en dan komt de config-afhankelijkheid alsnog terug.

**Laag 3, de code.** Met laag 1 en 2 op hun plek blijft er weinig over: alle `x-forwarded-for`-regels samenvoegen, splitten op komma, strippen, poort eraf, de rechtse waarde nemen, valideren als IP, en terugvallen op het peer-adres als er niets bruikbaars staat. Dat in een eigen middleware die `scope["client"]` zet, en dan is `request.client.host` de ene deur waar iedereen door leest. Geen nieuwe accessor, geen call site die iets nieuws moet leren, en de volgende ontwikkelaar die naief `request.client.host` gebruikt heeft het vanaf dan automatisch goed.

Waarom we hiervoor `ProxyHeadersMiddleware` niet houden: hij gooit de dubbele regel weg (de bug hierboven) en zijn `"*"`-modus geeft de linkse waarde. Met echte CIDR's zou hij het wel goed doen, maar dan hebben we de config-afhankelijkheid die laag 1 juist weghaalt.

## Plan

1. **Meten in prod, read-only**: wie opent er werkelijk verbindingen naar OPI:8000 naast de router. Een grep in git vindt geen ServiceMonitor of scrape naar die poort, maar afwezigheid in git is geen bewijs. Verify: een lijst peers, waarmee de keuze tussen laag 2 en de terugval een feit wordt in plaats van een aanname.
2. **`set-forwarded-headers: "replace"`** op de drie Ingress-objecten in de prod-overlay (`ingress.yaml`, `ingress-router.yaml`, `ingress-rijksapp.yaml`). Verify: een verzoek met een verzonnen `X-Forwarded-For` via de router levert op de pod nog precies EEN XFF-regel, met het echte adres. Dat is ook de toets die aantoont dat we geen hopteller nodig hebben.
3. **Netwerkbeleid versmallen** tot de ingress-namespace, als stap 1 dat toelaat. Verify: een `curl` van een willekeurige pod naar OPI:8000 loopt dood, en de portal doet het nog.
4. **De middleware** in `opi/middleware/`, met de leeslogica uit laag 3 en zonder config. Verify: unit-tests op twee regels, op een komma-lijst, op de gemengde vorm, op een waarde met een poort, en op een verzoek zonder header.
5. **`get_client_ip` platleggen** tot `request.client.host if request.client else "unknown"`. Verify: `tests/test_rate_limiter.py:278-299` keert om. Die tests pinnen vandaag precies het spoofbare gedrag vast, en ze gebruiken een dict als headers, dus ze moeten over een echte `Headers`-raw gaan.
6. **Het derde veld van wies overnemen** op de AUDIT-regel in `opi/api/v2/router.py:632`: naast het afgeleide IP ook het peer-adres, zodat een verzoek dat de router omzeilt zichzelf verraadt.
7. **De sandbox apart toetsen.** Daar staat nginx-ingress, en die negeert met `use-forwarded-headers: false` (de default) de inkomende header al en vult hem zelf. Verify: dezelfde toets als stap 2, zodat we weten dat de code in beide omgevingen klopt en niet per ongeluk.
8. **Optioneel, los van de uitrol**: het `dict()`-gedrag bij uvicorn melden, met de meting uit dit document.

## Open beslissingen

- **Laag 2 of de terugval.** Stap 1 beslist dit. Lukt het versmallen niet, dan hebben we alsnog de ingress-adressen in config nodig, en die staan nergens in deze repo: een grep op `forwarded` in `infrastructure/` en `bootstrap/` geeft twee treffers, allebei van Keycloak. Dat is dan een vraag aan wie de ingress beheert.
- **Wat `replace` nog meer raakt.** Hij gooit ook de `forwarded`-, `x-forwarded-proto`- en `x-forwarded-host`-regels van de client weg. Voor ons is dat winst, maar het is een gedragswijziging op de route die ook andere diensten op diezelfde Ingress raakt, dus hij hoort in een eigen PR met een eigen toets.
- **De rate-limit-emmers tijdens de overgang.** Zolang laag 1 en 2 niet beide staan, en we wel al op het peer-adres terugvallen, zit al het externe verkeer in een emmer en sluit de inloglimiet iedereen tegelijk uit. De stappen moeten dus in de gegeven volgorde, en stap 5 niet voor stap 2.
- **`x-forwarded-host` laten we staan.** De CSRF-controle leunt op de `Host`-header en die passeert ongewijzigd. Geen werk tenzij er een reden opduikt.

## Doodlopend, voor de volledigheid

Het schoolboekantwoord is PROXY protocol: de proxy geeft het peer-adres buiten HTTP om door, voor de eerste HTTP-byte, en een HTTP-client kan er niet bij. Maar uvicorn spreekt het niet en de OpenShift-router stuurt het niet naar pods. Daarmee valt het af.

## Verwijzingen

- `features/futures/gebeurtenissen-vastleggen-en-melden.md`, de `origin`-regel bij BIO2 8.15.01, en `features/futures/gebeurtenissen-plan-van-aanpak.md` fase 4, die dit werk al bij naam noemt
- OpenShift route-annotaties: https://docs.okd.io/4.20/networking/ingress_load_balancing/routes/nw-configuring-routes.html en de PR waarin het is toegevoegd, https://github.com/openshift/router/pull/134
- GCP's formaat met twee waarden: https://docs.cloud.google.com/load-balancing/docs/https
- ingress-nginx `use-forwarded-headers`: https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/configmap/
- OWASP over spoofbare client-IP-headers: https://community.owasp.org/pages/attacks/ip_spoofing_via_http_headers
- wies: `wies/rijksauth/request_meta.py` en `config/settings/base.py:157`
