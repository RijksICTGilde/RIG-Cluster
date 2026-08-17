# Eén toestand, één badge

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: een slapende deployment toont "Healthy" op de kaart én "slaapstand" in een apart blok eronder. Dat is te onduidelijk, en de reden is dat er twee mechanismen naast elkaar staan.

## Twee wegen voor hetzelfde soort ding

RC-28 bouwde de haak: een dienst meldt een **feit** over een deployment (`DeploymentStateFact` met een samenvatting en `expects_no_application_pods`), `collect_deployment_state` haalt ze op, en `section-deployment-state.html.j2` rendert ze in een blok onder de kaart. Sleep-mode is de bewoner.

RC-31 bouwde daarna `opi/services/disabled_state.py`: een eigen module die alleen over uitgeschakelde componenten gaat, buiten die haak om, met een eigen weergave die de Healthy-badge op de kaart **vervangt**.

Zo staan er nu twee mechanismen voor "deze deployment is in een bijzondere toestand", met twee verschillende weergaven:

| Toestand | Waar het vandaan komt | Wat je ziet |
|---|---|---|
| Uitgeschakeld | `disabled_state.py`, hardgecodeerd | De badge wordt "Uitgeschakeld" |
| Slapend | een feit van sleep-mode, via de haak | De badge blijft "Healthy", met een apart blok eronder |

Voor een gebruiker is dat verwarrend op precies de manier die je zou verwachten: de kaart zegt dat alles gezond is, en eronder staat dat de deployment slaapt. Allebei waar, samen onbruikbaar.

## Wat er moet gebeuren

Uitgeschakeld wordt een feit als elk ander, en de badge komt uit de feiten.

1. **`disabled_state.py` draagt bij aan de haak** in plaats van ernaast te bestaan. Wie meldt het? Uitgeschakeld is geen dienst maar een veld op een component, dus dit hoort bij de systeemdienst die er al is (`deployment-health`) of bij een generieke bijdrage in `collect_deployment_state`; RC-31 stond voor dezelfde keuze en heeft hem toen ontweken door een eigen module te maken.
2. **Een feit moet kunnen zeggen dat het de gezondheidsuitspraak vervangt.** Vandaag draagt `DeploymentStateFact` een samenvatting en `expects_no_application_pods`, en dat is niet genoeg om er een badge van te maken: er is een korte badge-tekst nodig ("Slaapstand", "Uitgeschakeld"). Voeg dat toe en houd het bij die twee velden; dit is niet de plek om een dienst een kleur of een icoon te laten kiezen.
3. **De kaart leidt de badge af uit de feiten.** Vervangt een feit de uitspraak, dan staat dat op de badge; anders blijft de gezondheid staan. De kaart kent dan geen enkele dienst bij naam, net zoals hij dat sinds vandaag ook niet meer doet voor de logs-knop.
4. **Het aparte blok blijft, maar alleen voor wat de badge niet zegt.** Een badge heeft ruimte voor één woord; "slaapt sinds gisteren, wordt gewekt bij verkeer" hoort in het blok. Bepaal expliciet wat waar staat, anders staat straks alles twee keer.

## Volgorde

1. Het veld op `DeploymentStateFact` en sleep-mode dat laten vullen, zonder dat de weergave verandert. Verifiëren: een slapende deployment levert een feit met een badge-tekst.
2. De kaart de badge uit de feiten laten afleiden, met sleep-mode als eerste zichtbare geval. Dan is de klacht opgelost.
3. `disabled_state.py` omzetten naar een bijdrage aan dezelfde haak en de module opheffen. Verifiëren: de vier bestaande tests van RC-31 blijven groen, want het gedrag mag niet veranderen.
4. Nakijken wat er in het blok overblijft.

## Waar op te letten

**Een echte storing mag niet verdwijnen.** RC-31 heeft dat goed: "Uitgeschakeld" vervangt alleen de groene Healthy die nul replicas oplevert, en Degraded, Progressing of Unknown houden hun badge. Die regel moet mee naar de nieuwe vorm en er staan al twee tests op (`test_a_real_failure_survives_being_switched_off`, `test_a_real_failure_outranks_being_switched_off`).

**Slapend en uitgeschakeld blijven verschillend.** Slapend gaat vanzelf over zodra er verkeer komt, uitgeschakeld blijft tot iemand het aanzet. Eén gedeelde badge "niet actief" zou verbergen of je iets moet doen.

**Gedeeltelijk uitgeschakeld is een derde geval.** Draait er nog iets, dan blijft de gezondheidsbadge staan met een chip "N van M componenten uitgeschakeld" ernaast. Dat werkt vandaag en mag niet sneuvelen in de omzetting.

**Twee diensten kunnen tegelijk iets melden.** Een deployment die slaapt én een uitgeschakeld component heeft, levert twee feiten die allebei de badge willen vervangen. Beslis wat er dan gebeurt in plaats van het aan de volgorde van de registry over te laten.
