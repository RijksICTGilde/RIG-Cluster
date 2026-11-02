# Security Headers (internet.nl compliance)

## What it does

Adds HTTP security headers to all ZAD responses for internet.nl compliance. Works on both NGINX (sandbox) and HAProxy (production) because headers are set at the application level via FastAPI middleware, not just ingress annotations.

## Headers set

| Header | Value | Purpose |
|--------|-------|---------|
| Strict-Transport-Security | `max-age=31536000; includeSubDomains; preload` | Force HTTPS for 1 year |
| X-Content-Type-Options | `nosniff` | Prevent MIME type sniffing |
| X-Frame-Options | `DENY` | Prevent clickjacking |
| Referrer-Policy | `strict-origin-when-cross-origin` | Limit referrer leakage |
| Permissions-Policy | `geolocation=(), microphone=(), camera=()` | Disable unused browser APIs |
| Content-Security-Policy | See below | Restrict resource loading |
| Content-Security-Policy-Report-Only | See below | Measure the stricter policy we want |

### Content-Security-Policy

```
default-src 'self';
script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline';
img-src 'self' data: <keycloak-origin>;
font-src 'self';
connect-src 'self' <keycloak-origin>;
form-action 'self' <keycloak-origin>;
frame-src 'self' <prometheus-origin>;
frame-ancestors 'none';
base-uri 'self'
```

- `unsafe-inline` voor scripts: zie "Op weg naar een nonce-beleid" hieronder
- `unsafe-inline` voor stijlen: de componenten renderen style-attributen
- Keycloak origin: dynamically derived from `KEYCLOAK_URL` setting

Er staat GEEN externe host meer in. Chart.js, chartjs-plugin-annotation, mermaid,
Swagger-UI en ReDoc komen uit `static/` in plaats van cdn.jsdelivr.net; wat er precies
staat, in welke versie en onder welke licentie noemt `static/HERKOMST.md`. Een host in
`script-src` laat ELKE bibliotheek van die host toe, dus zodra `'unsafe-inline'` ooit
vervalt zou zo'n toestemming een opening blijven.

## Op weg naar een nonce-beleid

Naast het beleid hierboven gaat een `Content-Security-Policy-Report-Only` mee met het
beleid dat we WILLEN:

```
script-src 'self' 'nonce-<per verzoek>' 'strict-dynamic';
object-src 'none'
```

`object-src 'none'` staat alleen in het doelbeleid: het portaal heeft geen plug-ins, en
`<object>` is onder `'strict-dynamic'` de weg terug naar scriptuitvoering. Verder is dat
beleid woord voor woord gelijk aan het afgedwongen beleid. De middleware
maakt per verzoek een nonce (`secrets.token_urlsafe(16)`), zet hem op `request.state`, en
elk eigen `<script>` in de sjablonen draagt hem via `nonce="{{ csp_nonce }}"`. De
sjablonen komen eraan langs een context_processor (`opi/core/templates_lotc.py`) en, voor
een fragment dat geen `TemplateResponse` is, langs `render_fragment`.

Het AFGEDWONGEN beleid draagt die nonce met opzet niet: zodra er een nonce in `script-src`
staat negeert de browser `'unsafe-inline'`, en dan valt elke inline event-handler weg.
Report-only breekt niets en levert wel het getal.

### Wat er nog in de weg staat

Gemeten op 21 oktober 2026, met `tests/e2e/test_csp_report_only.py`.

| wat | aantal | waar het zit |
|---|---|---|
| schendingen bij het laden | 3 per pagina | de drie scripts van de componentbibliotheek (`/static/lotc/nldd/dist/nldd.js`, `nldd/lotc-nldd.js`, `forms/forms.js`) |
| inline event-handlers in de DOM | 10 tot 37 per pagina | `lord-of-the-components` zet `@click` om in een `onclick`-attribuut (`python/src/lord_of_the_components/runtime.py`) |
| `/docs` en `/redoc` | 0 en 1 schending, 0 handlers | eigen sjablonen (`api-docs-swagger.html.j2`, `api-docs-redoc.html.j2`) met een nonce op elk script. Die ene op `/redoc` is geen eigen script: de redoc-bundel laadt zijn logo van `cdn.redoc.ly`, en het afgedwongen beleid blokkeert dat al |

Die eerste twee zijn hetzelfde probleem: ze komen uit de componentbibliotheek en niet uit deze
codebase. De drie scripts laadt `<c-page>` zonder nonce, en onder `'strict-dynamic'` wordt
`'self'` genegeerd, dus ze vallen weg. Een inline handler kan helemaal geen nonce dragen.

De inline handlers staan NIET in de schendingen: een `onclick` wordt pas gecompileerd als
er geklikt wordt, dus een paginabezoek meldt hem niet. Daarom telt de test ze apart; zonder
dat getal lijkt het beleid dichter bij afdwingbaar dan het is.

`/docs` en `/redoc` vielen eerst buiten de dekking: `get_swagger_ui_html` en
`get_redoc_html` (FastAPI) bouwen hun scripts in Python en kennen geen nonce. Daarom
rendert `opi/server.py` die twee pagina's nu uit eigen sjablonen, met dezelfde bestanden
uit `static/` en dezelfde parameters, en staan ze mee in de meting.

Afdwingen vraagt dus nog een ding: van `lord-of-the-components` event-binding zonder
inline handlers en een nonce op zijn eigen scripts. `style-src 'unsafe-inline'` blijft
voorlopig hoe dan ook staan: de componenten renderen style-attributen, en een attribuut
kan geen nonce dragen.

### Een fragment draagt de nonce van zijn eigen verzoek

Een nonce geldt EEN antwoord. Een htmx-fragment is een eigen verzoek, dus het inline
script in zo'n fragment draagt een andere nonce dan het document waarin het terechtkomt,
en de browser toetst tegen het beleid van dat document. Dat is geen probleem, en dat is
gemeten: htmx plaatst een ingeswapt script met `createElement('script')` en niet via de
parser, en `'strict-dynamic'` laat precies zo'n script toe, ongeacht zijn nonce. De meting
staat in `tests/e2e/test_csp_report_only.py`
(`test_een_ingeswapt_fragmentscript_komt_er_niet_bij`): het tabblad Metingen swapt een
fragment in dat Chart.js zelf bijlaadt, en levert geen schending op.

Valt `'strict-dynamic'` ooit uit het doelbeleid, dan vervalt dit en moet een fragment de
nonce van het document meekrijgen.

## security.txt

Served at `/.well-known/security.txt` with `Content-Type: text/plain` per RFC 9116.

## How it works

- `SecurityHeadersMiddleware` in `opi/middleware/security_headers.py` runs on every response
- Uses `setdefault` so it won't override headers already set by NGINX or the application
- HSTS is only set when `request.url.scheme == "https"` (safe for local dev)
- HAProxy HSTS is also set via `haproxy.router.openshift.io/hsts_header` annotation on production ingresses

## Files

- `opi/middleware/security_headers.py`: the middleware
- `opi/server.py`: middleware registration + security.txt route
- `operations-manager/python/static/HERKOMST.md`: pakket, versie en licentie van elke meegeleverde bibliotheek
- `bootstrap/.../odcn-production/ingress-rijksapp.yaml`: HAProxy HSTS annotation
- `bootstrap/.../odcn-production/ingress.yaml`: HAProxy HSTS annotation
- `tests/test_security_headers.py`: de headers, de nonce per verzoek en het verschil tussen de twee beleidsregels
- `tests/test_geen_externe_scripthost.py`: drift guard op de sjabloonbron
- `tests/e2e/test_eigen_bibliotheken.py`: de pagina's die Chart.js, Swagger-UI en ReDoc gebruiken werken zonder CDN
- `opi/templates_lotc/api-docs-swagger.html.j2`, `api-docs-redoc.html.j2`: `/docs` en `/redoc` met een nonce
- `tests/e2e/test_csp_report_only.py`: de meting hierboven, met de beginstand

## Not covered

- **IPv6 reachability**: ODC-Noord infrastructure
- **CAA DNS records**: manual action in TransIP
- **User application headers**: on production (HAProxy), user apps must set their own security headers; the ingress can only set HSTS
- **Het afgedwongen beleid zonder `'unsafe-inline'`**: zie "Wat er nog in de weg staat": dat vraagt eerst een wijziging in `lord-of-the-components`
