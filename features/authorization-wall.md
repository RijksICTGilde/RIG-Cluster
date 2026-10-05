# Authorization Wall

## What it is

The `authorization-wall` is a service that puts an authentication proxy in front of a web application. It is designed for static websites or applications that have no backend capable of handling OIDC authentication themselves. When enabled, unauthenticated users are redirected to Keycloak for login before they can access the application.

## How it works

An [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) sidecar container is added to the application's deployment. All incoming traffic is routed through the proxy (port 4180) instead of directly to the application. The proxy handles the complete OIDC flow with Keycloak:

```
Browser → ingress → service:4180 → oauth2-proxy (sidecar) → app (localhost:app_port)
```

1. User visits the application URL
2. oauth2-proxy checks for a valid session cookie
3. If no session: redirects to Keycloak login page
4. After login: Keycloak redirects back to `/oauth2/callback`
5. oauth2-proxy sets a session cookie and forwards the request to the application

## How to use it

### Prerequisites

The project must have `keycloak` configured in its services (the authorization wall reuses the project's Keycloak client credentials).

### Project YAML

1. Add `authorization-wall` to the project-level `services:` section
2. Add `authorization-wall` to the component's `uses-services` list

```yaml
services:
  - keycloak:
      config:
        template: sso-support
  - authorization-wall

components:
  - name: my-static-site
    ports:
      inbound: [8080]
    uses-services:
      - publish-on-web
      - authorization-wall
```

### Banner (optional sign-in page)

By default, the authorization wall redirects directly to Keycloak without showing an intermediate page. To show a sign-in page with a custom message before the Keycloak redirect, add a `banner` in the project-level `services:` config:

```yaml
services:
  - authorization-wall:
      config:
        banner: "Welcome to our application. Please log in with your SSO account."
```

De regel hierboven klopt niet: de wall stuurt een sessieloze navigatie **niet** rechtstreeks naar Keycloak door. `--skip-provider-button` staat op `false`, dus er komt altijd eerst een inlogkaart met een knop. Dat is ook de enige plek waar de banner rendert, en vijf projecten gebruiken dat veld. Of dat tussenscherm weg moet is een open afweging; zie `features/futures/authorization-wall-rolcontrole-in-de-proxy.md`.

### What gets generated

When `authorization-wall` is enabled for a component:

- **Deployment**: An `authorization-wall` sidecar container is added alongside the `app` container
- **Service**: Traffic is routed to port 4180 (oauth2-proxy) instead of the application port
- **Cookie secret**: A SOPS-encrypted Kubernetes Secret with a random cookie encryption key

No ingress changes are needed - the ingress routes to the service as normal.

## Configuration

The authorization wall is configured automatically from the project's existing Keycloak service configuration:

| Setting | Source |
|---------|--------|
| OIDC Issuer URL | Keycloak realm discovery URL |
| Client ID | Project's Keycloak client ID |
| Client Secret | Project's Keycloak client secret (from envFrom) |
| Cookie Secret | Random 32-byte key, generated on the first deploy and kept on later ones (to rotate it, see `sops-skip-unchanged-reencryption.md`) |
| Redirect URL | `https://<hostname>/oauth2/callback` |

## Sessie en cookies

Er lopen drie klokken door elkaar heen, en ze horen op elkaar afgestemd te zijn.

| klok | waarde | wie zet hem |
|---|---|---|
| Keycloak access token | 5 minuten | Keycloak default (Access Token Lifespan) |
| Keycloak SSO-sessie | 30 min idle, 10 uur max | Keycloak default; alleen de platform-realm krijgt andere waarden uit `bootstrap.yaml` |
| oauth2-proxy cookie | 10 uur | `--cookie-expire` in de sidecar-template |

oauth2-proxy neemt de levensduur van het access token over als expiry van zijn sessie. Die sessie leeft dus vijf minuten en wordt daarna met het refresh token verlengd, zonder dat de browser er iets van merkt. Daarom staat `--cookie-refresh=3m` in de template: zonder die vlag vernieuwt de proxy pas *nadat* de sessie verlopen is, en dan komen alle requests van één pagina tegelijk op een dode sessie aan en bieden ze allemaal hetzelfde refresh token aan. Keycloak roteert dat token, dus er wint één en de rest krijgt een 403. In de praktijk haalde het document het dan wel en vielen de stylesheet en de scripts om, met een kale pagina als resultaat.

`--cookie-expire` staat op 10 uur en niet op de oauth2-proxy-default van 168 uur, omdat een cookie niet langer geldig hoort te beweren te zijn dan de Keycloak-sessie waarop het leunt. Projectrealms krijgen geen `ssoSession*`-waarden uit een blueprint, dus daar geldt Keycloak's eigen 10 uur.

`--cookie-samesite=lax` staat er expliciet omdat de browser anders zelf kiest. Strict kan niet: Keycloak komt met een top-level redirect terug op `/oauth2/callback`, en strict houdt het cookie daar tegen.

### Wat een sessieloze aanvraag terugkrijgt

| soort aanvraag | antwoord |
|---|---|
| navigatie | 403 met de inlogkaart en een knop |
| pad onder `/api/` | 401 zonder body |
| statisch bestand (op extensie) | 401 zonder body |

Die laatste twee regels zijn de kern. Eerder kreeg **elke** sessieloze aanvraag een 403 met de inlogpagina als HTML, ook een `fetch()` en ook een stylesheet. Een browser kan daar niets mee: er is geen navigatie om te volgen, en `text/html` met `nosniff` is geen CSS. Het resultaat was een halve pagina zonder opmaak, met fouten in de console die niets zeggen over de werkelijke oorzaak. De twee `--api-route`-regels maken daar een kale 401 van, zodat een mislukte subresource als mislukte subresource aankomt. De tweede matcht op extensie omdat de paden per applicatie verschillen.

Let op dat de eerste regel alleen paden onder `/api/` dekt. Een applicatie die zijn XHR elders heeft staan krijgt daar nog de HTML-pagina, en kan `/oauth2/auth` gebruiken om te toetsen of de sessie weg is (401 zonder sessie, 202 met).

`--cookie-csrf-per-request=true` dekt een losse valkuil: zonder die vlag deelt oauth2-proxy één CSRF-cookie, dat een tweede gestarte inlogpoging overschrijft, waarna de callback van de eerste afketst op een 500. Twee tabbladen die binnen het kwartier beide op Inloggen drukken is genoeg.

Wat hiermee **niet** is opgelost: de muur toont nog altijd een tussenscherm bij een sessie die stil te herstellen was, en controleert zelf geen rol. Beide staan in `features/futures/authorization-wall-rolcontrole-in-de-proxy.md`.

## Dependencies

- `keycloak` service must be configured at the project level
- `publish-on-web` should be enabled for the component (otherwise there's no ingress to protect)
