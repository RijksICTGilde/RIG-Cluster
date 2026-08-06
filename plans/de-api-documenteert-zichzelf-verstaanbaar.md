# De API documenteert zichzelf verstaanbaar

Status: plan, 7 augustus 2026. Niet gebouwd. Aanleiding: de API-documentatie toont diensten zo vaak dat het op duplicaten lijkt, endpoints staan door elkaar in plaats van bij hun dienst, en de meeste vertellen niet wat ze doen.

## Wat er nu is, gemeten

```
101 operaties in de spec
244 regels die de documentatie daarvan toont
 53 operaties met een dubbele tag
 34 van de 39 dienst-operaties zonder omschrijving
```

**De 244 tegen 101 is de kern.** Swagger UI groepeert op tag en toont een operatie onder *elke* tag die hij draagt. Een dienst-endpoint draagt er vier:

```
tags = ['v2', 'v2', 'services', 'attachments']
```

Dus dezelfde upload staat onder "v2" (twee keer, want de tag staat er dubbel in), onder "services", en onder "attachments". Dat is niet een gevoel van rommeligheid maar letterlijk hetzelfde endpoint dat vier plekken op de pagina inneemt. Bij 53 operaties staat een tag dubbel; dat alleen al is een fout die niets oplevert.

**Vier gegenereerde modelnamen versterken het.** De uploadroutes declareren geen eigen body-model, dus FastAPI verzint er een:

```
Body_create_attachments_component_api_v2_projects__project_name__services_attachments_component__component_name__attachments_post
Body_update_attachments_component_api_v2_..._attachment_id__put
Body_create_attachments_project_api_v2_..._attachments_post
Body_update_attachments_project_api_v2_..._attachments__attachment_id__put
```

Vier namen van rond de honderd tekens die alleen verschillen in niveau en werkwoord, tussen `AttachmentUse` en `AttachmentsConfig` in de schemalijst. Ze lezen als duplicaten omdat ze er zo uitzien.

**En 34 van de 39 dienst-operaties hebben alleen een summary, geen omschrijving.** Wat een endpoint doet met het projectbestand, wat er daarna gebeurt, en wanneer je hem wel of niet gebruikt staat er niet.

## De "of"-conditie die ontbreekt

`FieldCombination` (RC-38) drukt een implicatie uit: *als* `provide-as=file`, *dan* is `path` verplicht. Twee daarvan zijn in gebruik, allebei bij attachments. De opzet klopt: het is documentatie met een verwijzing en geen tweede implementatie, want `enforced_by` wijst naar het model dat de regel echt afdwingt en een test lost dat pad op, zodat de verwijzing niet kan verrotten naar een bewering over een controle die niet meer bestaat.

Wat er niet is, is de **disjunctie**: "geef A of B mee". Bij een bijlage op een component is dat een echt geval: je verwijst naar een bestaand id, of je levert zelf inhoud. Met implicaties is dat alleen van twee kanten op te schrijven, en dan bewaakt niets dat het er precies één is. Een client ontdekt de regel nu pas bij een 422.

In OpenAPI heet dit `oneOf` of `anyOf`; in Pydantic is het een model-validator of een discriminated union. Het kan dus, en het hoort op dezelfde plek te landen als de implicatie.

## Voorstel

1. **Eén groep per dienst, en verder geen.** Een operatie draagt de tag van zijn dienst, niet ook nog de laag eronder. De dubbele `v2` verdwijnt sowieso; die is nooit iets waard geweest. Verifieer met een test dat het aantal getoonde regels gelijk is aan het aantal operaties, want dat is precies wat nu niet klopt (244 tegen 101) en het is een getal dat stil terugloopt.

2. **Versie hoort in het pad, niet in een groep.** `/api/v2/...` zegt het al. Een tag "v2" met 106 leden groepeert niets; hij zet alles bij elkaar. Als een lezer versies uit elkaar wil houden, doet het pad dat.

3. **Geef de uploadroutes een eigen body-model met een leesbare naam.** Cosmetisch voor de spec, geen gedragsverandering, en het haalt vier regels weg die als duplicaten lezen.

4. **Elke dienst-operatie krijgt een omschrijving** die zegt wat er met het projectbestand gebeurt en wat er daarna gebeurt (rolt dit uit, of alleen opslaan). Met een test die faalt op een operatie zonder omschrijving, zoals RC-38 die al voor configvelden heeft.

5. **Voeg de disjunctie toe naast de implicatie**, met dezelfde eis: hij verwijst naar de code die hem afdwingt en een test lost die verwijzing op. Laat hem doorwerken in de spec (`oneOf`/`anyOf`), zodat een client de regel leest in plaats van hem te ontdekken.

## Volgorde

1. De tags. Verifiëren: het aantal getoonde regels zakt van 244 naar 101, met de test die dat vasthoudt.
2. De body-modellen van de uploads.
3. De omschrijvingen, met hun test. Eerst de test schrijven en zien welke operaties hij noemt; dat is meteen de werklijst.
4. De disjunctie, met de component-bijlage als eerste bewoner (verwijzing of inhoud, niet allebei en niet geen van beide).

## Waar op te letten

**Een tag is een groep, geen etiket.** De verleiding is om alles wat waar is als tag toe te voegen. Elke extra tag is een extra kopie op de pagina; dat is de hele oorzaak van dit plan.

**Een omschrijving is geen herhaling van de naam.** "Upload an attachment" staat al in de summary. De omschrijving hoort te vertellen wat de aanroeper moet weten en niet kan raden: waar het terechtkomt, of het een uitrol veroorzaakt, en wat er gebeurt als het id al bestaat.

**De disjunctie mag geen tweede waarheid worden.** `FieldCombination` is bewust documentatie met een verwijzing. Een disjunctie die zelf gaat valideren, zou de regel op twee plekken zetten en dat is precies wat de bestaande opzet vermijdt.

**Dit raakt geen gedrag, op punt 5 na.** Punten 1 tot en met 4 veranderen alleen wat de spec toont. Houd ze uit elkaar in de commits, zodat een terugval in gedrag niet tussen de documentatie verstopt zit.
