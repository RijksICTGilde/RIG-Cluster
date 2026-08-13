# ArgoCD token-cache

De ArgoCD-sessietoken wordt gedeeld binnen het proces in plaats van bij elke connector opnieuw
opgehaald. Dat scheelt op de projectdetailpagina ruim een factor vier.

## Het probleem

`create_argo_connector()` wordt op zestien plekken aangeroepen en levert elke keer een verse
`ArgoConnector`. De constructor deed vervolgens meteen een login - en wel een **blokkerende**
`requests.post` op de event loop.

Die login kost consequent ~700 ms. Dat ligt niet aan het netwerk (een round-trip naar ArgoCD is
0,07 s) maar aan ArgoCD zelf: het wachtwoord wordt met bcrypt geverifieerd, en bcrypt is met opzet
traag. Het token dat je ervoor terugkrijgt is een JWT die **24 uur** geldig is.

Gemeten op de sandbox-detailpagina: van de ~1,0 s totaal was 871 ms de ArgoCD-login. De
AGE-decrypts, de gebruikelijke verdachte, kostten samen 16 ms.

## De oplossing

Een cache op moduleniveau in `opi/connectors/argo.py`, met sleutel `(server, gebruiker)`:

- de constructor pakt een bestaand token op en logt alleen in als er nog geen is;
- `_ensure_authenticated()` doet hetzelfde voor de async kant;
- beide login-paden schrijven het token terug in de cache.

Twee locks, omdat er twee login-paden zijn: een `threading.Lock` voor de synchrone constructor en
een `asyncio.Lock` voor de async kant.

### Twee details die het verschil maken

**Double-check binnen de lock.** Als tien aanroepers tegelijk een lege cache aantreffen, nemen ze
allemaal de lock. Zonder hercontrole ná het verkrijgen ervan doen ze alle tien een login van 700 ms,
netjes achter elkaar - langzamer dan de oude situatie. Daarom kijkt elke aanroeper binnen de lock
opnieuw of iemand anders het token al heeft opgehaald, en neemt dat dan over.

**Compare-and-clear bij een 401.** Een request dat een 401 krijgt mag alleen het token wissen dát
het zelf gebruikte. Anders: request A gebruikt T1 en krijgt een 401, B heeft ondertussen al ververst
naar T2, en A gooit T2 weg - waarna iedereen opnieuw inlogt, met kans op een lus.
`_invalidate_token()` vergelijkt daarom eerst.

## Resultaat

| Detailpagina (`/projects/{project}/details`) | Vóór | Ná |
|---|---|---|
| Mediaan | 0,951 s | 0,217 s |
| Spreiding (min-max) | 0,711-1,196 s | 0,213-0,223 s |
| ArgoCD-logins per 7 requests | 7 | 0 |

De spreiding stort in omdat de variabele bcrypt-kosten verdwijnen.

## Tests

`tests/test_argo_token_cache.py` legt de drie gedragingen vast: een tweede connector hergebruikt het
token, gelijktijdige aanroepers produceren één login, en een verlopen 401 gooit een nieuwer token
niet weg.

## Wat dit niet oplost

Alleen de authenticatie. De resterende ~200 ms op de detailpagina zit in `kubectl get secret` voor
het Kopia-backupwachtwoord en de Kopia-repositorycheck zelf. Wil je verder, dan is de volgende stap
de statusopvraag van het renderpad halen (via HTMX nabestellen), of de ArgoCD `Application`-CR
rechtstreeks uit Kubernetes lezen (gemeten: 0,066 s, zonder auth).
