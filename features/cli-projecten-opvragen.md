# Je projecten opvragen vanaf de CLI

Sinds `POST /api/v2/projects` kun je een project aanmaken met een SSO-token
(`features/cli-project-aanmaken.md`), maar je kon niet opvragen wélke projecten je hebt.
Een CLI die opnieuw opstart wist daardoor niet waar hij was: onder `/api/v2/projects`
bestond alleen `POST`, en alles daaronder (`/projects/{naam}/...`) vraagt de
**projectsleutel**, die per project is en die je pas kunt gebruiken als je de projectnaam
al weet. Zonder lijst geen naam, zonder naam geen sleutel.

`GET /api/v2/projects` doorbreekt dat: met hetzelfde SSO-token krijg je de projecten waar
je bij mag, en van de projecten die je beheert ook de sleutel waarmee je verder kunt.

## Gebruik

```bash
curl "https://zad.rijksapps.nl/api/v2/projects" \
  -H "Authorization: Bearer $ZAD_TOKEN"
```

```json
{
  "projects": [
    {
      "name": "mijn-project",
      "description": "Nog een test",
      "role": "admin",
      "api_key": "Xk3mQ9vP2rT7wY1bN5cL8hJ4gF6dS0aZ"
    },
    {
      "name": "project-van-het-team",
      "description": "Waar ik in meewerk",
      "role": "developer",
      "api_key": null
    }
  ]
}
```

| Veld | Betekenis |
|---|---|
| `name` | De technische projectnaam, waarmee je de andere endpoints aanroept. |
| `description` | Waar het project voor is. |
| `role` | De rol van de aanroeper in dit project: `admin` of `developer`. |
| `api_key` | **Geheim.** De projectsleutel, voor de `X-API-Key`-header op elke volgende aanroep. Alleen gevuld bij de rollen `admin` en `owner`; `null` voor een `developer`. |

Het antwoord kan een geheim dragen en gaat daarom met `Cache-Control: no-store` de deur uit.

De lijst is gesorteerd op naam. Antwoordcodes: `200` met de lijst (leeg als je nergens bij
hoort), `401` geen geldig token of geen toegang tot het platform.

## De authenticatie

Dezelfde weg als het aanmaken: `Authorization: Bearer <access token>`, geverifieerd tegen
de JWKS van de realm, met de identiteit uit het geverifieerde `email`-claim. De details
staan in `features/cli-project-aanmaken.md` en zijn hier niet opnieuw bedacht -- deze route
gebruikt dezelfde decorator (`validate_user_token`).

**Een projectsleutel opent deze route niet.** Dat is geen omissie maar het punt: een
projectsleutel beantwoordt "mag ik dit project aanraken", en dit is juist de vraag welke
projecten er zijn. De twee wegen kruisen elkaar niet, en er is een test die dat vasthoudt.

## Wie welk project ziet

Het filter is `is_user_authorized_for_project(project, email)` -- dezelfde functie waar de
webkant op leunt, met het e-mailadres uit de tokenclaims. Er is dus geen tweede
autorisatieregel bijgekomen. Een project waar je niet bij mag ontbreekt **volledig** uit
het antwoord, ook als naam; je leert er niet uit dat het bestaat.

**Een platformbeheerder ziet elk project, met elke sleutel.** Dat volgt uit diezelfde
functie, die een beheerder overal toegang geeft. Het is een bewuste uitkomst en consistent
met de UI, waar een beheerder elke projectpagina kan openen en de sleutel daar kan lezen.
Het is géén aparte "toon alles"-modus: er is geen `?all=true` en die hoort er ook niet
ongemerkt bij te komen. Wie zoiets wil, neemt daar een eigen besluit over.

## Wie de sleutel krijgt, en waarom niet iedereen

Overwogen zijn twee vormen:

| | |
|---|---|
| **A. Lijst mét sleutels** | Eén aanroep, de CLI kan meteen verder. |
| **B. Lijst zonder sleutels, plus een aparte `GET .../key`** | Wie alleen wil weten wat er is, krijgt geen geheimen. |

Het is **A** geworden, maar dan achter dezelfde rolpoort als de UI: `api_key` is gevuld voor
de rollen `admin` en `owner` en `null` voor een `developer`.

Die rolpoort is het punt. De oorspronkelijke onderbouwing van A was "de sleutel staat al op
de projectdetailpagina, dus dit is dezelfde informatie via een andere deur, aan dezelfde
mensen". Nagemeten klopt dat alleen voor beheerders: `section-config.html.j2` zet het hele
blok Configuratie & Secrets, inclusief `config['api-key']`, achter
`{% if user_role in ["admin", "owner"] %}`. Een `developer` ziet die sleutel via de UI dus
níét.

En het gaat verder dan zichtbaarheid: **de projectsleutel kent zelf geen rollen.** Elke
route achter `@validate_api_token` accepteert hem zonder rolcontrole -- een deployment
verwijderen, een component toevoegen, een image wisselen, een database of bucket klonen.
De webkant zet daar wél een rolpoort voor (`require_project_edit_access` weigert elke
bewerking met 403 als de rol geen `admin` of `owner` is). Een lijst die de sleutel aan een
`developer` geeft, zou hem via de API laten doen wat de UI hem verbiedt -- en langlevend
ook, want de sleutel blijft geldig als zijn rol wordt ingetrokken. Daarom is de sleutel in
deze route achter exact dezelfde poort gezet, letterlijk dezelfde constante
(`PROJECT_EDIT_ROLES` in `opi/services/project_authorization.py`) die de webkant gebruikt.

Voor wie de sleutel wél krijgt blijft de bundeling staan: één gestolen SSO-token levert in
één aanroep alle sleutels op van de projecten die die persoon beheert, waar één gestolen
projectsleutel één project opent. Daarom staat in de OpenAPI-omschrijving van het endpoint
en bij het veld zelf expliciet dat er een geheim in het antwoord zit, zodat een aanroeper
dat weet vóórdat hij het antwoord ergens logt.

## Verse gegevens

De route draait eerst `store.reconcile()`, net als het aanmaken. Zonder dat mist een CLI een
project dat zojuist door een ander cluster of buiten ZAD om is gemaakt. Reconcile vraagt de
remote eerst om zijn branch-tip (`ls-remote`) en slaat het dure deel over als er niets is
veranderd.

## Afhankelijkheden

- `opi/api/user_token_auth.py` -- tokenverificatie en de allowlist (uit het aanmaak-endpoint)
- `opi/services/project_authorization.py` -- `is_user_authorized_for_project` en
  `get_user_role_for_project`
- De projectstore, die de sleutel al ontsleuteld in de cache heeft staan

## Tests

`tests/test_list_projects_api.py`:

- de deur: geweigerd zonder token, geweigerd met een **projectsleutel**, geweigerd met een
  kapot token, geweigerd buiten de allowlist;
- het filter: alleen de projecten van deze gebruiker, de naam van andermans project lekt
  niet, en iemand zonder projecten krijgt een lege lijst;
- de inhoud: naam, omschrijving en rol, plus dat er gereconcilieerd wordt en dat het
  antwoord `Cache-Control: no-store` draagt;
- de sleutel: een `admin` en een `owner` krijgen hem, een `developer` krijgt zijn project
  wél en de sleutel `null` -- en die sleutel komt dan ook nergens anders in het antwoord voor;
- de beheerder: ziet elk project, met elke sleutel, overal als `admin`;
- de documentatie: de omschrijving noemt het geheim, de beheerder én dat een `developer`
  geen sleutel krijgt, en het veld `api_key` is als geheim gemarkeerd.
