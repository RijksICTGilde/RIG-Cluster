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

**Dit veld doet op dit moment niets.** De banner rendert alleen op de sign-in-pagina van oauth2-proxy, en die pagina wordt niet meer getoond: een sessieloze aanvraag gaat rechtstreeks naar Keycloak. Zie `Sessie en cookies` hieronder voor het waarom. De vlag gaat nog wel mee naar de sidecar, zodat de plumbing intact blijft tot besloten is of het veld verdwijnt of dat de tekst naar het Keycloak-inlogthema verhuist.

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

De muur houdt alles tegen, en lost waar het kan zelf op. Het inlogscherm hoort alleen te verschijnen wanneer er geen sessie te maken valt: omdat die niet bestaat, of omdat hij verlopen is en ook niet stil te vernieuwen.

| soort aanvraag | antwoord |
|---|---|
| navigatie, Keycloak-sessie leeft nog | 302 naar Keycloak en direct terug; de gebruiker ziet geen scherm |
| navigatie, geen Keycloak-sessie | 302 naar Keycloak, dat zijn eigen inlogscherm toont |
| pad onder `/api/` | 401 zonder body |
| statisch bestand (op extensie) | 401 zonder body |

Dat het inlogscherm van Keycloak komt en niet van de muur is de bedoeling: Keycloak draait het `nl-design-system`-thema en kent de identity providers. De muur heeft geen manier om vooraf te weten of een stille herlogin gaat lukken, want alleen Keycloak weet of er nog een SSO-sessie is. Daarom stuurt de muur altijd door en laat hij Keycloak beslissen.

Een applicatie achter de muur hoeft dus zelf niets te detecteren. Dat was eerder wel nodig: de muur antwoordde elke sessieloze aanvraag met een 403 en de inlogpagina als HTML, ook een `fetch()` en ook een stylesheet. Een browser kan daar niets mee, want er is geen navigatie om te volgen en HTML is geen CSS.

De twee `--api-route`-regels horen bij `--skip-provider-button`. Zonder de eerste krijgt een `fetch()` een 302 naar Keycloak, die cross-origin afketst op CORS en bij de applicatie aankomt als netwerkfout zonder statuscode. Zonder de tweede start elk sessieloos verzoek om een stylesheet of een icoon een eigen OAuth-flow, wat Keycloak belast en de browser niets oplevert. Die tweede regel matcht op extensie, omdat de paden per applicatie verschillen.

Let op dat de eerste regel alleen paden onder `/api/` dekt. Een applicatie die zijn XHR elders heeft staan krijgt daar een redirect in plaats van een 401, en kan `/oauth2/auth` gebruiken om te toetsen of de sessie weg is (401 zonder sessie, 202 met).

`--cookie-csrf-per-request=true` hoort er ook bij: zodra meerdere aanvragen tegelijk een inlogpoging kunnen starten, bijvoorbeeld uit twee tabbladen, deelt oauth2-proxy anders één CSRF-cookie en overschrijft de tweede flow die van de eerste.

## Dependencies

- `keycloak` service must be configured at the project level
- `publish-on-web` should be enabled for the component (otherwise there's no ingress to protect)
