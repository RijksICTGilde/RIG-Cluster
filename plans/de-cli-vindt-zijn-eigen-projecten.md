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

**Advies: B**, omdat het het gewone geval (wat heb ik?) scheidt van het gevoelige geval (geef me de sleutel), en omdat het de blast radius van een gelekt token kleiner houdt zonder de CLI iets te kosten: die doet één extra aanroep op het moment dat hij van project wisselt. Maar A is verdedigbaar en was de vraag; leg de keuze vast met de reden, zoals bij `rollout=false` en `confirm_in_use`.

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
