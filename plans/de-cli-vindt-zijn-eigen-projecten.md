# De CLI vindt zijn eigen projecten

Status: plan, 8 augustus 2026. Niet gebouwd. Aanleiding: je kunt sinds RC-51 een project aanmaken via de API met een SSO-token, maar je kunt niet opvragen welke projecten je hebt. Een CLI die opnieuw opstart weet dus niet waar hij is.

## Wat er nu is, gemeten

Onder `/api/v2/projects` bestaat precies één operatie:

```
POST /api/v2/projects        aanmaken, met een SSO-token (RC-51)
```

Er is geen `GET`. Alles wat daarna komt hangt aan `/projects/{project_name}/...` en vraagt de **projectsleutel**, die je alleen hebt als je al weet om welk project het gaat. Dat is een kip-en-ei: zonder lijst weet je niet welke projecten er zijn, en zonder projectnaam kun je geen sleutel gebruiken.

De autorisatievraag is al beantwoord in de code: `is_user_authorized_for_project(project_name, user_email)` en `get_user_role_for_project(...)` bestaan in `opi/services/project_authorization.py`. Een lijst is dus "loop de projecten langs en houd over waar deze gebruiker bij mag".

## Wat het moet worden

```
GET /api/v2/projects
Authorization: Bearer <SSO-token>
```

Per project: **naam, omschrijving en API-sleutel**, zodat de CLI zijn context kan zetten en meteen verder kan zonder tweede aanroep.

Langs dezelfde weg als het aanmaken: een SSO-token, geen projectsleutel. Dat kan ook niet anders, want de projectsleutel is per project en dit is juist de vraag welke er zijn.

## Eén afweging die vooraf gemaakt moet worden

**Een lijst mét sleutels is een grotere buit dan een lijst zonder.** Vandaag geeft één gestolen projectsleutel toegang tot één project. Als deze route sleutels teruggeeft, levert één gestolen SSO-token in één aanroep alle sleutels op waar die gebruiker bij mag.

Daar staat tegenover dat dat token al genoeg is om projecten aan te maken, en dat de CLI de sleutel toch nodig heeft zodra hij van context wisselt.

Twee vormen, en de keuze hoort bewust gemaakt te worden:

| | |
|---|---|
| **A. Lijst mét sleutels** | Eén aanroep, de CLI kan meteen verder. Wat de vraag was. |
| **B. Lijst zonder sleutels, plus `GET /projects/{naam}/key`** | Wie alleen wil weten wat er is, krijgt geen geheimen. De sleutel haal je op als je hem nodig hebt. |

**Besloten op 8 augustus: A, de lijst draagt de sleutels.**

De reden is sterker dan mijn oorspronkelijke bezwaar. De sleutel staat **al** op de projectdetailpagina, achter dezelfde autorisatie (`section-config.html.j2` toont `config.api-key` in een `c-secret-field`). Wie deze lijst mag opvragen, kan die sleutel vandaag al zien door de pagina te openen. Een lijst met sleutels voegt dus geen nieuwe blootstelling toe; het is dezelfde informatie via een andere deur, aan dezelfde mensen.

Wat overeind blijft en in de uitvoering hoort: de OpenAPI-omschrijving moet **zeggen** dat er een geheim in het antwoord zit, zodat een aanroeper dat weet voordat hij het antwoord ergens logt.

### Correctie na de securityreview, 8 augustus: A, maar achter de rolpoort van de UI

De onderbouwing hierboven klopt niet zoals ze er staat. Nagemeten dekt "dezelfde deur, dezelfde mensen" alleen `admin` en `owner`: `section-config.html.j2:2` zet het hele secrets-blok, inclusief `config['api-key']`, achter `{% if user_role in ["admin", "owner"] %}`, terwijl het filter van deze route lidmaatschap is (`is_user_authorized_for_project`), niet rol. Een `developer` zou de sleutel dus via deze lijst krijgen en via de UI niet -- dat is wél een nieuwe blootstelling.

En het weegt zwaarder dan zichtbaarheid, want de projectsleutel kent zelf geen rollen: elke `@validate_api_token`-route accepteert hem zonder rolcontrole (deployment verwijderen, component toevoegen, image wisselen, klonen), terwijl de webkant diezelfde mutaties voor een `developer` met 403 weigert (`require_project_edit_access`). Een `developer` met de sleutel is dus een verticale rechtenverhoging, en een langlevende: de sleutel overleeft het intrekken van zijn rol.

Uitgevoerd is daarom de variant die de UI exact spiegelt: `api_key` is gevuld voor `admin` en `owner`, en `null` voor een `developer`, achter dezelfde constante (`PROJECT_EDIT_ROLES`) die de webkant gebruikt. Vorm A blijft dus overeind voor wie mag handelen -- één aanroep, meteen verder -- zonder de rolgrens op te heffen. Wie de sleutelkant voor een `developer` alsnog wil, moet vorm B nemen (aparte sleutelroute achter dezelfde rolpoort) of het rolmodel zelf veranderen; geen van beide hoort in deze route.

## Voorstel

1. **`GET /api/v2/projects`**, met hetzelfde tokenpad als de `POST`. Geen nieuwe manier van herkennen, alleen een tweede route die hem gebruikt.
2. **Alleen wat deze gebruiker mag zien.** Gebruik de bestaande autorisatiefuncties; een project waar je niet bij mag hoort niet in de lijst, ook niet als naam.
3. **De rol erbij.** Als het toch per gebruiker is, is "wat mag ik hier" bruikbare informatie voor een CLI die straks acties aanbiedt.
4. **De sleutel volgens de gekozen vorm**, met de afweging opgeschreven.
5. **Een test dat een projectsleutel deze route NIET opent**, zoals RC-51 die ook heeft. De twee wegen horen niet te kruisen.

## Volgorde

1. De route met naam, omschrijving en rol, zonder sleutels. Dat deel is onomstreden en meteen bruikbaar.
2. De sleutelkant, in de gekozen vorm.
3. De CLI-kant: context zetten uit deze lijst.

## Waar op te letten

**Wat je hier teruggeeft, geef je aan iedereen met dat token.** Een omschrijving is onschuldig, een sleutel niet. Als de lijst sleutels draagt, hoort dat expliciet in de OpenAPI-omschrijving te staan, zodat een aanroeper weet dat hij een geheim in handen krijgt.

**Geen lijst van alles voor een beheerder.** De verleiding is een `?all=true` voor wie admin is. Dat is een tweede autorisatiemodel in dezelfde route; als dat nodig is, is het een eigen besluit en een eigen route.

**Dit hoort bij `zad-cli`.** Punt 6 van de TODO verwijst al naar wat daar op ons wacht; deze route is precies wat de CLI nodig heeft om zijn context te kunnen zetten.
