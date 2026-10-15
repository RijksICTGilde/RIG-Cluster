# Keycloak logout-pad per component

## Wat het is

Een opt-in veld op het `keycloak`-dienstblok waarmee een project zegt welk pad op zijn
eigen applicatie de sessie van die applicatie beëindigt. OPI zet dat pad als
`frontchannel.logout.url` op de Keycloak-client van elke deployment, zodat Keycloak de
applicatie kan bellen als iemand uitlogt.

Zonder dit veld verandert er niets: de client houdt een lege `frontchannel.logout.url`.

## Hoe je het gebruikt

In het projectbestand, of via het scherm "Keycloak configuratie" onder "Uitloggen":

```yaml
services:
  keycloak:
    config:
      logout-path: /api/v1.0/logout/
```

Het pad is **relatief**. OPI rekent het uit naar `https://<eerste-ingress-host><pad>`,
net als bij de andere Keycloak-paden, zodat niemand een hostname in zijn projectbestand
hoeft te zetten en niets scheef loopt als een subdomein wijzigt. Een absolute URL wordt
daarom geweigerd.

Deze vier schrijfwijzen leveren allemaal hetzelfde op:

| in het projectbestand | wat de client krijgt |
|---|---|
| `/api/v1.0/logout/` | `/api/v1.0/logout/` |
| `api/v1.0/logout/` | `/api/v1.0/logout/` |
| `/logout` | `/logout` |
| `logout` | `/logout` |

Een ontbrekende of dubbele leidende slash is geen gebruikersfout om een foutmelding over
te geven, dus die wordt rechtgezet. Een **afsluitende** slash blijft staan zoals hij
staat: `/api/v1.0/logout/` is een ander endpoint dan `/api/v1.0/logout`, en docs heeft de
eerste nodig.

## Wat het endpoint moet doen

Hetzelfde als wat de applicatie bij een eigen uitlogknop doet: de sessie van die gebruiker
wissen. Keycloak haalt de URL op in de browser van de gebruiker, dus het is een gewone
GET; een redirect erna is toegestaan. `docs` heeft dit al:
`GET https://docs.rijksapp.nl/api/v1.0/logout/` geeft een 302 naar de eigen homepage en
ruimt de sessie op.

Front-channel, niet backchannel: backchannel is een server-naar-server POST met een
`logout_token` JWT die de applicatie moet valideren, en zo'n endpoint hebben deze
applicaties niet. Voor de applicatie waar je op dat moment uitlogt is front-channel ook
de juiste weg, want de browser is er toch.

## Beperking die vandaag geldt

**Binnen de federatie naar SSO Rijk wordt de URL niet aangeroepen, en dat is structureel.**
De `always-clear-session-logout`-shim op `rig-platform` wist de sessie en stuurt de browser
naar de uitlogpagina van BZK; het projectrealm hoort de uitlog daarna als
server-naar-server POST, en in zo'n verzoek zit geen browser om iframes in te renderen.
Twee manieren om de browser er alsnog langs te sturen zijn gemeten en vallen beide af. Het
mechanisme en die metingen staan in
`features/keycloak-always-clear-session-logout.md`, sectie "De fan-out binnen de federatie:
waarom hij er niet is". Reken er dus niet op dat dit later vanzelf gaat werken.

Buiten de federatie (inloggen op het projectrealm zelf) belt Keycloak de URL wel; dat is de
groene meting in het harnas (`tests/e2e/test_keycloak_logout_harness.py`).

Wie dit veld aanzet moet ook weten wat de vlag eronder kost. `frontchannel.logout.url`
werkt alleen op een client waarvan Keycloaks schakelaar "Front channel logout" aan staat,
en OPI zet die aan op beide clients van elke deployment: `<client>` en `<client>-public`.
Diezelfde vlag laat binnen de federatie, gemeten in het harnas, de sessie van het
projectrealm overleven: de POST loopt vast op een 501 en Keycloak draait het verwijderen van
de sessie terug. Een van de twee clients met de vlag aan is daarvoor genoeg, zolang de
gebruiker er een sessie op heeft, dus hem uitzetten is niet een schakelaar maar twee. Dat
geldt ook zonder `logout-path`, want de vlag staat er los van. De vork tussen die twee staat
in het feature-document hierboven.

De robuuste weg blijft daarom: de applicatie wist haar eigen sessie **voordat** ze de
browser naar het end-session endpoint stuurt. Zie
`instructions/uitloggen-in-een-applicatie.md`.

## Configuratie

| Sleutel | Laag | Default | Betekenis |
|---|---|---|---|
| `logout-path` | `services/keycloak/config` | afwezig | Relatief pad op de applicatie dat haar sessie beëindigt |

- Model: `KeycloakConfig.logout_path` in `opi/services/catalog/keycloak/config_model.py`
- Normalisatie: `normalize_logout_path` in `opi/utils/url_paths.py`, een lezer voor elke
  plek die het veld aanraakt: het configmodel, `KeycloakManager._get_keycloak_service_config`,
  de formuliervalidator `LogoutPathValidator` en de formulierconverter `LogoutPathConverter`
- Formulier: `KEYCLOAK_LOGOUT_PATH` in `opi/services/catalog/keycloak/visualizers.py`,
  in het veldblok "Uitloggen"
- Client: `KeycloakConnector.create_deployment_client` zet
  `frontchannel.logout.url = https://<eerste-host><pad>`

Bij meerdere ingress-hosts wordt de eerste gekozen, want Keycloak accepteert één
front-channel URL per client.

## Veiligheid

Het pad gaat door dezelfde charset-controle als de `match`/`rewrite`-paden van een
component (`SAFE_PATH_PATTERN`: `/`, letters, cijfers en `-._~`). Dat schild stond er tegen
nginx-snippet-injectie in `ingress.yaml.jinja`, en het geldt hier net zo goed, want dit pad
wordt een URL die Keycloak zelf ophaalt.

## Bestaande clients

Een client die al bestond krijgt het attribuut bij de eerstvolgende verwerking van het
project: de 409-tak van `create_deployment_client` trekt nu elk attribuut gelijk dat die
methode bezit, in plaats van alleen `post.logout.redirect.uris`.

Haal je `logout-path` weer weg, dan verdwijnt de URL ook van de client:
`frontchannel.logout.url` staat altijd in de verwachte attributen, leeg als er geen pad is.
Een lege waarde betekent voor Keycloak hetzelfde als geen waarde. Een bestaande client die
het attribuut nooit had wordt daardoor een keer bijgewerkt om het leeg te schrijven.

Hetzelfde geldt een laag hoger voor de platform-clients in `rig-platform`:
`_ensure_idp_and_platform_client_configuration` neemt hun logout-attributen mee in de
reconcile. Zeven clients van voor 15-05-2026 hadden daardoor nog geen
`backchannel.logout.url`, want die reconcile trok alleen `redirectUris` gelijk.

## Afhankelijkheden

- De `keycloak`-dienst (en daarmee `publish-on-web`, voor de ingress-host)
- Geen Keycloak-versie-eis: `frontchannel.logout.url` is een standaard clientattribuut

## Gerelateerd

- `instructions/uitloggen-in-een-applicatie.md` - het contract voor de applicatiekant
- `features/keycloak-always-clear-session-logout.md` - de shim en waarom de keten eindigt
