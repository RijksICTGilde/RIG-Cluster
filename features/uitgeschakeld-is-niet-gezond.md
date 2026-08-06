# Uitgeschakeld is niet gezond

## Wat het is

Een component met `disabled: true` wordt gerenderd met `replicas: 0`. ArgoCD noemt nul
replicas gezond -- er is immers niets dat faalt -- en die waarde ging ongefilterd door naar
de drie plekken waar iemand kijkt om te weten of alles goed gaat:

| Waar | Wat je zag |
|---|---|
| De dashboardbanner | "Alle N projecten zijn gezond", inclusief een project dat niets draait |
| De deploymentkaart | De groene Healthy-badge naast de rode badge "Uitgeschakeld", op dezelfde regel |
| De V2 API | `"status": "Healthy"` voor een deployment die uit staat |

Dit onderdeel maakt van "uitgeschakeld" een **feit** dat het platform zelf meldt, en voedt
daarmee die drie weergaven.

## Drie toestanden, geen twee

Een deployment waarvan één van de vier componenten uit staat is iets anders dan een die
helemaal uit staat: in het eerste geval bedient er nog iets verkeer, in het tweede niet.

| Toestand | Betekenis |
|---|---|
| `RUNNING` | Niets uitgeschakeld (ook: een deployment zonder componenten) |
| `PARTIALLY_DISABLED` | Een deel uit, de rest draait |
| `DISABLED` | Alles uit |

```python
from opi.services.catalog.deployment_health.disabled import deployment_disabled_state

state = deployment_disabled_state(project_data, deployment_name)
state.is_disabled              # alles uit
state.is_partially_disabled    # een deel uit
state.disabled_count, state.total_count
```

Gelezen uit het **projectbestand**, nooit uit het cluster: nul replicas in het cluster kan
ook betekenen dat er iets misging, en juist dat onderscheid is de hele bedoeling. De
voorrang is dezelfde die de manifestgeneratie gebruikt -- een inline vlag op de
deployment-component wint, anders beslist de componentdefinitie.

## Wie meldt het

De systeemdienst `deployment-health` (RC-28), via `HookPoint.DEPLOYMENT_STATE` -- dezelfde
haak waarop sleep-mode meldt dat een deployment slaapt.

`disabled: true` is een veld op een component, dus geen gewone dienst bezit het. Daarom
had een generieke bijdrage in `collect_deployment_state` gekund. Dat is bewust niet
gedaan: de haak is het contract ("wie iets weet over deze deployment, zegt het"), en een
tweede pad ernaast betekent twee manieren om een feit toe te voegen en twee plekken om er
een te zoeken. `deployment-health` spreekt al voor het platform zelf, dus het feit van het
platform hoort daar.

Het blijft een **feit, geen oordeel** -- de RC-28-vorm:

- helemaal uit → `expects_no_application_pods=True`. Dat verklaart alleen *afwezige* pods;
  een component dat uit staat én kapot is, blijft kapot melden.
- gedeeltelijk uit → `expects_no_application_pods=False`. De rest hoort te draaien, dus
  ontbrekende pods blijven zichtbaar als probleem.

## Wat je ziet

### De deploymentkaart

- **Alles uit**: de badge "Uitgeschakeld" komt in plaats van de health-badge -- maar
  uitsluitend in plaats van `Healthy`. Is de health `Degraded`, `Progressing` of
  `Unknown`, dan staat die er gewoon naast: iets uitschakelen mag nooit een manier zijn om
  een storing te laten verdwijnen.
- **Gedeeltelijk uit**: de health-badge blijft staan, met "N van M componenten
  uitgeschakeld" ernaast.
- Staat de deployment op een ander cluster (geen badge-rij), dan draagt de chip in de kop
  de mededeling.

Sinds RC-35 komen die woorden niet meer uit een eigen module maar uit het feit zelf
(`DeploymentStateFact.badge`), en leidt de kaart ze generiek af -- dezelfde weg waarlangs
de slaapstand nu ook een badge krijgt. De regels hierboven zijn ongewijzigd; ze zijn alleen
niet langer met de hand op "uitgeschakeld" geschreven. Zie
`features/deployment-state-and-health.md` voor de badge-regels en wat er gebeurt als twee
diensten tegelijk iets melden.

### De dashboardbanner

"Alle N projecten zijn gezond" blijft ongewijzigd zolang er niets uit staat of geparkeerd
is. Zodra dat wel zo is, laat de banner het woord "alle" vallen:

```
2 van de 3 projecten zijn gezond
  1 project heeft een uitgeschakelde deployment
  1 project heeft een deployment die tijdelijk niet actief is
```

De zin wordt gekozen in `_dashboard_health_banner` in `opi/web/router.py`, niet afgeleid in
de template: het is een tekstkeuze, geen renderdetail.

Uitgeschakeld en tijdelijk-niet-actief worden apart geteld en apart benoemd. Slapend gaat
vanzelf over bij het eerste bezoek; uitgeschakeld blijft tot iemand het aanzet. Eén grijze
"niet actief" voor allebei laat een lezer niet zien of er iets te doen valt.

In de projecthealth rangschikken `Disabled` en `Inactive` net boven `Healthy`: een project
met een uitgeschakelde deployment mag niet meetellen als gezond, maar hoort ook niet boven
een deployment te staan die echt degraded is.

### De V2 API

`DeploymentStatus` heeft een waarde `Disabled`. Die vervangt uitsluitend `Healthy`; elke
andere uitkomst (`Degraded`, `OutOfSync`, `Progressing`, `Missing`, `Suspended`,
`Unknown`) is iets dat ArgoCD echt waarnam en blijft staan.

**Let op -- dit is een gedragsverandering in een publieke API.** Een client die vandaag op
`status == "Healthy"` filtert, krijgt een uitgeschakelde deployment niet meer terug. Dat is
de bedoeling, maar het vraagt een aanpassing aan de kant van zo'n client.

Slapende deployments krijgen (nog) geen eigen API-status; die toestand is via de
deployment-state-feiten beschikbaar en staat op de deploymentweergave.

## Configuratie

Geen. De toestand volgt uit `disabled` in het projectbestand.

## Afhankelijkheden

- `opi/services/catalog/deployment_health/disabled.py` -- de drie toestanden, bij de dienst die ze meldt
- `opi/services/catalog/deployment_health/` -- meldt het feit
- `opi/services/deployment_state.py` -- de collector (RC-28)
- `opi/templates/project-details/_argocd-deployment-card.html.j2`
- `opi/web/router.py` -- `_deployment_inactivity`, `_derive_project_health`, `_dashboard_health_banner`
- `opi/api/v2/router.py` -- `_collapse_argo_status`

## Zie ook

- `features/deployment-state-and-health.md` - het haakpunt en de asymmetrie eronder
- `features/sleep-mode.md` - de andere reden dat een deployment niets draait
- `features/oom-kill-watcher.md` - wat componenten automatisch uitschakelt
