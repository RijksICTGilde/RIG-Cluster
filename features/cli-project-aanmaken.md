# Een project aanmaken vanaf de CLI

Een project aanmaken kon alleen via het portaal, want dat vereist SSO in een browser. Een
agent of script kwam daar niet doorheen: de API kent maar één manier binnen te komen, een
sleutel die bij een project hoort, en bij het aanmaken bestaat dat project nog niet.

`POST /api/v2/projects` maakt een project aan met een SSO-token in plaats van een
projectsleutel, en geeft de projectnaam plus de nieuwe API-sleutel terug.

## Wat het doet

De aanroep maakt de **basis** van een project: identiteit (`name`, `display-name`,
`description`), het cluster, het `repositories`-blok en de eigen sleutels van het project
(AGE-keypaar en API-sleutel). Er worden **geen componenten en geen deployments**
aangemaakt, dus er komt niets op het cluster te draaien. Dat richt je daarna in met de
bestaande endpoints.

De aanroeper wordt als `admin` in de gebruikerslijst gezet.

Het projectbestand wordt opgebouwd door dezelfde bouwer als het zelfbedieningsportaal
(`generate_self_service_project_yaml`), zodat `main-repo` en de sleutels op één plek
worden samengesteld en de twee wegen niet uiteen kunnen lopen.

## Gebruik

```bash
curl -X POST "https://zad.rijksapps.nl/api/v2/projects" \
  -H "Authorization: Bearer $ZAD_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "mijn-project", "description": "Nog een test"}'
```

```json
{
  "status": "accepted",
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "task_type": "create_project",
  "poll_url": "/api/tasks/550e8400-e29b-41d4-a716-446655440000",
  "project_name": "mijn-project",
  "api_key": "Xk3mQ9vP2rT7wY1bN5cL8hJ4gF6dS0aZ"
}
```

| Veld | Verplicht | Betekenis |
|---|---|---|
| `name` | ja | Technische naam. Begint met een kleine letter, verder kleine letters, cijfers en koppeltekens, maximaal 20 tekens. |
| `description` | ja | Waar het project voor is. |
| `display_name` | nee | Naam zoals getoond in het portaal. Standaard gelijk aan `name`. |

De `api_key` staat **alleen in deze body** en nergens anders in leesbare vorm. Elke
volgende aanroep voor dit project gebruikt hem als `X-API-Key`. Hij komt bewust niet in
een URL terecht: een sleutel in een query string belandt in browsergeschiedenis,
proxy-logs en referrers.

Antwoordcodes: `202` geaccepteerd, `400` ongeldige projectnaam, `401` geen geldig token of
geen toegang tot het platform, `409` projectnaam bestaat al, `422` ongeldige body.

## De authenticatie

De API is hier **resource server en geen identity provider**. Er is geen inlogflow, geen
callback, geen sessie en geen redirect; het endpoint wil alleen een geldig token zien.

- `Authorization: Bearer <access token>` (RFC 6750). Niet in een query string, niet in een
  eigen header.
- Het token wordt geverifieerd tegen de **JWKS van de realm**: handtekening, uitgever,
  doelgroep en geldigheidsduur. De uitgever en de JWKS-url komen uit het discovery-document
  van de realm (`OIDC_DISCOVERY_URL`), dus ze worden niet zelf in elkaar gezet.
- Alleen asymmetrische algoritmes worden geaccepteerd; `alg: none` en een HMAC met de
  publieke sleutel worden geweigerd.
- De **identiteit** komt uit de claims, de **autorisatie** is van ons: het e-mailadres moet
  geverifieerd zijn en op dezelfde allowlist staan die de webkant gebruikt. Dat het token
  geldig is zegt wie iemand is, niet dat hij een project mag aanmaken.

De bestaande sessie-gebaseerde SSO voor de webkant blijft ongemoeid. Dit is een tweede
manier om een aanroeper te *herkennen*, naast de sleutel per project, geen tweede manier om
in te loggen.

## Hoe de CLI aan een token komt

Een CLI die je uitdeelt kan geen `client_secret` bewaren, dus de vertrouwelijke client van
het portaal is hier niet bruikbaar. De CLI gebruikt **authorization code met PKCE en een
loopback-redirect** (RFC 8252), net als `gcloud auth login`, `aws sso login` en
`gh auth login --web`:

1. De CLI luistert op `http://127.0.0.1:<vrije poort>` en verzint een `state`-nonce plus een
   PKCE-verifier.
2. Hij opent de browser naar de authorisatie-url van de realm, met die loopback als
   `redirect_uri`.
3. De gebruiker logt in met SSO, in zijn eigen browser.
4. Keycloak stuurt de code naar de loopback; de CLI wisselt hem met de PKCE-verifier om voor
   een token.

Wat het platform daarvoor levert is de publieke client `zad-cli` in de realm, aangemaakt door
de Keycloak-bootstrap (`opi/configs/keycloak/sso-only.yaml` en `sso-support.yaml`):

- `publicClient: true`, geen secret;
- `pkce.code.challenge.method: S256`, dus PKCE verplicht;
- redirect-uri's `http://127.0.0.1/*` en `http://localhost/*`. Keycloak negeert de poort bij
  een loopback-adres, dus er hoeft geen poort vastgelegd te worden;
- een audience-mapper die `zad-api` in de `aud` van het **access token** zet.

Die laatste is makkelijk stil verkeerd te zetten. De UI bewaart het `id_token`, maar deze API
verifieert het **access token**, en zonder de juiste `audience` faalt de verificatie of --
erger -- zou een token geaccepteerd worden dat voor iets anders bedoeld was. Er is een test
die precies dat afdwingt.

## Configuratie

| Instelling | Standaard | Waarvoor |
|---|---|---|
| `CLI_CLIENT_ID` | `zad-cli` | De client-id van de publieke CLI-client in de realm |
| `CLI_TOKEN_AUDIENCE` | `zad-api` | De `aud` die een access token moet dragen |
| `OIDC_DISCOVERY_URL` | - | De realm waartegen tokens worden geverifieerd (bestond al) |
| `ALLOWED_EMAILS` | - | Wie het platform mag gebruiken (bestond al) |

## Waarom er niets wordt uitgerold

Het nieuwe project verklaart geen deployments. `process_project` beschouwt "geen deployments
voor dit cluster" als een mislukking, dus een project dat prima is aangemaakt zou als
mislukte taak eindigen. De taak draagt daarom `rollout=false`: het bestand wordt geschreven
en gecommit, en het cluster komt in beeld zodra er een deployment bij komt. Dat is hetzelfde
mechanisme dat de andere endpoints gebruiken, beschreven in
`features/opslaan-zonder-verwerken.md`.

## Wat er niet in zit

**Verwijderen.** Aanmaken met een gebruikerstoken is één besluit; verwijderen met datzelfde
token is een tweede, en het hoort niet mee te liften omdat het toevallig in dezelfde route
zou passen.

## Afhankelijkheden

- Keycloak-realm met de `zad-cli`-client (komt uit de bootstrap-templates)
- `authlib` voor de JWKS-verificatie
- Het bestaande taaksysteem (`create_project`) en de projectstore

## Tests

- `tests/test_user_token_auth.py` -- tokenverificatie: geldig, verkeerde ondertekenaar,
  verkeerde uitgever, verkeerde doelgroep, verlopen, `alg: none`, HS256-met-publieke-sleutel,
  en de autorisatiestap los daarvan.
- `tests/test_create_project_api.py` -- het endpoint: geweigerd zonder token, geweigerd met
  een projectsleutel, geweigerd buiten de allowlist, en bij succes de teruggegeven sleutel
  (die echt de opgeslagen sleutel is), het repositories-blok, de admin, geen deployments, en
  `rollout=false`.
- `tests/test_task_handlers_progress.py` -- de taak schrijft het bestand en stopt bij
  `rollout=false`, en rolt wel uit als de vlag ontbreekt.
