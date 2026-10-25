# Platformbeheer via de API

## Wat het is

De acht beheerroutes van OPI lopen op het persoonlijke token van een platformbeheerder, niet
op een gedeelde sleutel. Een beheerhandeling via de API is een handeling van een persoon, en
het actor-spoor noemt die persoon: in de git-trailer en in `async_tasks.created_by` staat het
e-mailadres, waar eerder `admin-api-key` stond.

Tot RC-377 schermde een gedeelde sleutel (`ADMIN_API_KEY`) deze routes af. Die sleutel
bestaat niet meer. Hij droeg geen rol en geen naam, stond op productie niet gezet (zodat de
routes daar 501 gaven) en was niet aan een mens te koppelen.

## Wie

Een gebruiker die platformbeheerder is: zijn e-mailadres staat in `ADMIN_EMAILS` en
`UserService.is_platform_admin` bevestigt dat. Dat is dezelfde grens als de
`/admin/...`-pagina's van het portaal gebruiken.

## Met welk token

Hetzelfde SSO-bearertoken dat zad-cli gebruikt, in de `Authorization`-header:

```bash
curl -X GET "https://zad.rijksapps.nl/api/v2/admin/orphans/report" \
  -H "Authorization: Bearer $ZAD_TOKEN"
```

De verificatie zit in `opi/api/user_token_auth.py`: signatuur tegen de JWKS van de realm,
daarna issuer, audience en geldigheid, daarna de identiteit uit de claims. Pas daarna wordt
beslist of die identiteit dit mag.

## De antwoorden

| Situatie | Antwoord |
|---|---|
| Geen `Authorization: Bearer`-header | `401` |
| Alleen een `X-API-Key` (projectsleutel of een andere gedeelde sleutel) | `401`, want er is geen token |
| Een geldig token van een gebruiker die geen platformbeheerder is | `403` |
| Een geldig token van een platformbeheerder | de route zelf |

Een projectsleutel is hier geen zwakkere vorm van dit credential maar een ander credential:
hij legitimeert een project, geen persoon.

## De routes

`action` is de waarde die een weigering in het problem-detail noemt; een leesroute heeft er
geen, want hij vraagt geen OTP.

| Route | Wat hij doet | OTP | `action` |
|---|---|---|---|
| `GET /api/v2/admin/marked-for-deletion` | de resources die voor verwijdering gemarkeerd staan | nee | |
| `GET /api/v2/admin/orphans/report` | het wezenrapport, zonder mutaties | nee | |
| `POST /api/v2/admin/cleanup/trigger` | de opruiming van verlopen markeringen starten | ja | `opruiming-starten` |
| `POST /api/v2/admin/reconciliation/trigger` | een volledige reconciliatie starten | ja | `reconciliatie-starten` |
| `DELETE /api/v2/admin/marked-for-deletion/{mark_id}` | een markering intrekken | ja | `markering-intrekken` |
| `POST /api/v2/admin/orphans/confirm` | bevestigde wezen markeren | ja | `wezen-bevestigen` |
| `POST /api/v2/admin/projects/:reconcile` | de projects-repo nu inlezen | ja | `projecten-herinlezen` |
| `POST /api/v2/projects/{project_name}/:rotate-project-key` | de projectsleutel vervangen (`features/projectsleutel-vervangen.md`) | ja | `projectsleutel-vervangen` |

## Een verse OTP bij een wijziging

Een beheerroute die iets VERANDERT vraagt naast het token een verse code uit de
authenticator van de beheerder, in `X-ZAD-OTP`. Dat is dezelfde step-up als bij een
handeling op productie (RC-332), met dezelfde header, hetzelfde problem-detail en dezelfde
`cause`-waarden; zie [otp-op-de-zad-gebruiker.md](otp-op-de-zad-gebruiker.md) voor de velden.
Het verschil is alleen de aanleiding: platformbeheer weegt geen omgeving, want een
beheerhandeling raakt het hele cluster.

```bash
curl -X POST "https://zad.rijksapps.nl/api/v2/admin/projects/:reconcile" \
  -H "Authorization: Bearer $ZAD_TOKEN" \
  -H "X-ZAD-OTP: 123456"
```

Er is geen venster: elk verzoek draagt zijn eigen code, en een hergebruikte code faalt.

Zonder code is het antwoord `403` met `application/problem+json`, `type:
urn:zad:step-up-required` en `cause: otp-ontbreekt`. Een client hoort daaraan te zien dat hij
om een code moet vragen en de aanroep te herhalen. `action` zegt welke handeling het was,
`reason` waarom ze een step-up vraagt, `remedy` wat er moet gebeuren.

De twee leesroutes vragen alleen het token: een OTP erop zou een handeling verzinnen die er
niet is.

## Ook op dit token: de back-upstand en de federatie (RC-386)

`MASTER_API_KEY` bestond hiernaast als tweede gedeelde sleutel. Die is weg, en zijn
leesroutes vragen nu hetzelfde token, zonder step-up:

| Route | Wat het is |
|---|---|
| `GET /api/v1/backup/status` | de back-uplock van het cluster, een per cluster en dus zonder project |
| `GET /api/federation/peers` | de ingerichte peers, zonder hun secrets |
| `GET /api/federation/health` | een health-check per peer |

De twee federatieroutes geven **404** als de federatie op deze instantie uit staat (geen
`FEDERATION_ROLE=master` met peers). Dat er hier niets is ingericht is geen serverfout.

## Wat niet op dit token loopt

- **De `/admin/...`-pagina's van het portaal.** Die lopen op de sessie, niet op een token.

## Afhankelijkheden

- `opi/api/user_token_auth.py`: `validate_platform_admin_token` en de tokenverificatie
- `opi/services/user_service.py`: `is_platform_admin`, gevuld uit `ADMIN_EMAILS`
- `opi/api/route_herkenning.py`: de markering `platformbeheerder-token`
- `opi/core/step_up.py` en `opi/api/otp_step_up.py`: de step-up en het problem-detail (RC-332)
- `opi/services/step_up_conditions.py`: `platformbeheer_wijzigt`, de conditie van de beheerroutes
- `tests/test_platformbeheer_via_de_api.py`: de guard op de routes en op de oude sleutel
- `tests/test_platformbeheer_step_up.py`: per route de 403, de geslaagde aanroep en de hergebruikte code
- `tests/test_step_up_routes.py`: de lijst van routes die een step-up dragen, en hun OpenAPI
