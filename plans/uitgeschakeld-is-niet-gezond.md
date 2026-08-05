# Uitgeschakeld is niet gezond

Status: plan, 5 augustus 2026. Niet gebouwd. Aanleiding: een deployment die uit staat wordt overal als "Healthy" gerapporteerd. Dat is niet onhandig maar onwaar, en het staat op de plek waar iemand kijkt om te weten of alles goed gaat.

## Wat er gebeurt

Een component met `disabled: true` krijgt `replicas: 0`. ArgoCD noemt nul replicas gezond, want er is niets dat faalt. Die waarde gaat vervolgens ongefilterd door naar drie plekken:

| Waar | Wat je ziet |
|---|---|
| `templates/dashboard.html.j2:43` | De banner "Alle N projecten zijn gezond" |
| `templates/project-details/_argocd-deployment-card.html.j2` | De groene Healthy-badge, met op diezelfde kaart de rode badge "Uitgeschakeld" ernaast |
| `api/v2/router.py:172` `_collapse_argo_status` | Dezelfde status in de API |

Die tweede is het scherpst: de pagina zegt tegelijk "dit staat uit" en "dit is gezond", en dat is niet op te lossen door er beter naar te kijken.

Slapende deployments hebben exact hetzelfde. Ook nul replicas, ook Healthy.

## Waarom dit nu klein is

Op 5 augustus heeft RC-28 precies de haak opgeleverd die hiervoor nodig is. `opi/services/deployment_state.py` verzamelt feiten die diensten over een deployment melden:

- `DeploymentStateFact` met een `summary` en `expects_no_application_pods`
- `collect_deployment_state(project_data, deployment_name)` die ze ophaalt via de registry
- Sleep-mode als eerste bewoner: "deze deployment slaapt, nul pods is hier verwacht"

Slapend is dus al een feit dat het systeem kent. Uitgeschakeld is hetzelfde soort feit en hoort erbij, en dan voeden die feiten de drie weergaven.

Let op de vorm die daar bewust gekozen is, en houd die aan: een feit is **geen oordeel**. `expects_no_application_pods` zegt alleen dat afwezige pods hier de bedoeling zijn; het maakt een waargenomen storing niet gezond. Diezelfde scheiding geldt hier: "uitgeschakeld" vervangt de gezondheidsweergave, maar dooft geen echt probleem.

## Voorstel

1. **Uitgeschakeld als feit.** Wie meldt het? Het is geen dienst maar een veld op een component, dus dit hoort bij de systeemdienst die er al is (`deployment-health`) of bij een generieke bijdrage in `collect_deployment_state`. Kies één van de twee en leg vast waarom, want dit is de eerste bijdrage die niet van een gewone dienst komt.
2. **Drie toestanden in plaats van twee.** De oorspronkelijke notitie stelde `RUNNING`, `PARTIALLY_DISABLED` en `DISABLED` voor, en dat onderscheid is nodig: een deployment waarvan één van de vier componenten uit staat is iets anders dan een die helemaal uit staat. Bij helemaal uit toont de badge "Uitgeschakeld" in plaats van de health; bij gedeeltelijk blijft de health staan met "N van M componenten uitgeschakeld" ernaast.
3. **De banner mag niet meer liegen.** "Alle N projecten zijn gezond" moet uitgeschakelde en slapende deployments niet als gezond meetellen. Wat er dan wél staat is een tekstkeuze, geen technische: bepaal die expliciet in plaats van hem uit de code te laten volgen.
4. **De V2 API krijgt dezelfde waarheid.** `_collapse_argo_status` is de plek. Let op dat dit publiek gedrag is: een client die vandaag op `Healthy` filtert krijgt straks een andere uitkomst, en dat is de bedoeling, maar het hoort in de release-aantekening.

## Volgorde

1. Het feit toevoegen en aantonen dat het opgehaald wordt, zonder dat er iets aan de weergave verandert. Verifiëren: een deployment met een uitgeschakeld component meldt dat, een gewone niet.
2. De deployment-kaart, want daar staat de tegenspraak letterlijk op één regel.
3. Het dashboard en de banner.
4. De V2 API als laatste, met de aantekening erbij.

## Waar op te letten

**Een echte storing mag niet verdwijnen.** Als "uitgeschakeld" de health-badge vervangt, moet een component dat uit staat én kapot is nog steeds zichtbaar zijn. Dit is dezelfde eis die RC-28 al stelde en die daar getest is (`test_a_sleeping_state_does_not_excuse_an_observed_problem`); neem het equivalent hier op.

**Slapend en uitgeschakeld zijn niet hetzelfde.** Slapend gaat vanzelf over als er verkeer komt; uitgeschakeld blijft tot iemand het aanzet. Toon dus niet één grijze "niet actief" voor allebei, want dan weet je niet of je iets moet doen.

**Nul replicas is niet altijd uitgeschakeld.** Een deployment kan ook nul replicas hebben doordat iets misging. Leid de toestand af uit het projectbestand, zoals RC-28 dat doet, en niet uit wat het cluster laat zien.
