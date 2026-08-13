# De deploymentpagina

De projectpagina toont zijn deployments als **tabel op het tabblad Overzicht**, met de
ArgoCD-status erin. Elk tabblad heeft een **eigen pad**. De tabbladen Deployments en
Metrics tonen er **een per pagina**, met zijn naam in de URL, in **een** blok.

## 1. De tabel op Overzicht

`/projects/<project>/details` had een blok "Deployment Status": een kaart per deployment,
die elk hun eigen ArgoCD-bevraging deden zodra je ze in beeld kreeg. Bij drie deployments
is dat een prima beeld; bij twintig scrol je langs twintig kaarten om te zien of er een
rood is, en kost het openen van de pagina twintig verzoeken.

Daar staat nu een tabel, en die **vervangt** de kaarten - ze staan er niet naast, want
twee weergaven van dezelfde lijst lopen uiteen.

| Kolom | Wat er staat |
|---|---|
| Naam | link naar `/projects/<project>/deployments/<naam>` |
| Cluster | onderscheidt anders identieke namen |
| Status | wat de diensten melden (slaapstand, uitgeschakeld) plus health en sync van ArgoCD |
| Laatste sync | wanneer ArgoCD deze deployment voor het laatst bijwerkte |
| Componenten | aantal |

### Zoeken en sorteren

Op de SERVER, via de URL - dus het werkt zonder JavaScript, is deelbaar als link, en de
telling boven de tabel kan niet uit de pas lopen met de rijen eronder. Met htmx wordt
alleen `#deployments-lijst` vervangen; zonder JavaScript herlaadt het formulier de pagina.

| Wat | Hoe |
|---|---|
| Zoeken | `?q=` - op deploymentnaam en cluster |
| Sorteren | `?dsort=` - `naam`, `naam-af`, `cluster`, `componenten` |

```
/projects/mijn-project/details?q=pr-&dsort=naam-af
```

Er wordt niet op status gesorteerd: die komt van een ander systeem, en dan zou dezelfde
URL morgen een andere volgorde geven.

## 2. De ArgoCD-status, gebundeld

Een statusoverzicht zonder status is geen overzicht - maar een bevraging per rij is precies
wat de kaarten al te duur maakte. `opi/services/argocd_overview.py` haalt daarom de LIJST
op: een enkele `GET /api/v1/applications`, waar de applicaties van dit project uit gepikt
worden. **Twintig rijen kosten een verzoek, niet twintig.**

Daarbovenop een korte cache in het geheugen van het proces:

- **`CACHE_TTL_SECONDS = 15`**. Kort, en dat is de afweging: een verouderde "Healthy" is
  erger dan geen status. Wat deze vervaltijd opvangt is de stoot - de pagina openen, F5,
  van tabblad wisselen en terug - en verder niets.
- Per project. Kent de bewaarde stand niet elke gevraagde deployment (er is er een
  bijgekomen), dan wordt er gewoon opnieuw opgehaald.

Duurt de bevraging langer dan `BEVRAGING_TIMEOUT_SECONDS = 5`, dan gaat de pagina zonder
statuskolom verder: deze bevraging staat op het renderpad, en de connector zelf wacht tot
dertig seconden.

Is ArgoCD niet verbonden, dan komt er niets terug en zegt de pagina dat in gewone taal
boven de tabel. Kent ArgoCD een deployment niet, dan staat er "Niet in ArgoCD": dat is iets
anders dan ongezond.

De **volledige diagnose** (de foutenlijst, de gebeurtenissen uit de namespace, de logknop)
blijft waar hij stond: in het deploymentpaneel, dat zichzelf laadt. Die vraagt meer van
ArgoCD en van kubectl, en er staat er precies een op de pagina.

Het **backupblok** staat hier helemaal los van: dat laadt lui vanwege de Kopia-verbindingen
en heeft zijn eigen, project-brede verzoek.

## 3. Elk tabblad een eigen pad

`?tab=deployments` is `/projects/<project>/deployments` geworden. Een querystring leest als
een filter op een pagina; een tabblad is een andere pagina over hetzelfde project.

| Tabblad | Pad |
|---|---|
| Overzicht | `/projects/<project>/details` |
| Componenten | `/projects/<project>/componenten` |
| Services | `/projects/<project>/services` |
| Deployments | `/projects/<project>/deployments/<deployment>` |
| Metrics | `/projects/<project>/metrics/<deployment>` |
| Taken | `/projects/<project>/taken` |

**De projectnaam staat voorop en het tabblad erachter.** Het project is waar je bent, het
tabblad is wat je erbinnen bekijkt. De vorige vorm had het tabblad voorop
(`/projects/deployments/<project>`); die adressen **verwijzen door** (302) naar de nieuwe,
inclusief de deployment en de querystring, zodat een gedeelde link niet stil een 404 wordt.

De paden staan **letterlijk** geregistreerd in `opi/web/router.py` en niet als
`/projects/{project_name}/{tab}`. Dat laatste zou ook de oude vorm opvangen -
`/projects/details/<naam>` zou een project met de naam "details" worden - en dan bepaalt de
volgorde van registreren welke route wint. Dat geldt ook voor de twee paden met een
deployment erachter. De nieuwe vorm staat vóór de oude geregistreerd: bij een project dat
toevallig `details` of `deployments` heet zijn beide te lezen, en dan wint het adres van
vandaag.

**`?tab=` bestaat niet meer, ook niet als doorverwijzing.** Die vorm heeft nooit buiten
deze applicatie geleefd - de links erheen stonden in de eigen sjablonen en tests - en een
doorverwijzing die niemand gebruikt is een tweede adres dat onderhouden moet worden. Een
achtergebleven `?tab=` wordt genegeerd; je krijgt Overzicht.

De adressen komen uit een plek: `PROJECT_TABS` en `project_tab_url()` in
`opi/web/lotc_switch.py`.

## 4. Een deployment, een blok

Op het tabblad Deployments stond dezelfde deployment in twee blokken onder elkaar: een
statuskaart (naam, cluster, de rode melding, Uitgeschakeld/Synced, laatste sync) en
daaronder "Deployment: `<naam>`" met de acties, de componenten en de publieke links.

Ze zijn samengevoegd, met het tweede als uitgangspunt - dat draagt de acties en de inhoud.
Bovenin dat paneel staan nu het cluster, wat de diensten over deze deployment melden, en de
ArgoCD-statuskaart die zichzelf laadt.

## 5. Een deployment per pagina

De tabbladen Deployments en Metrics renderden **alle** deployments en verborgen er alles
behalve een met CSS. Dat is werk voor blokken die niemand ziet - elk verborgen blok draagt
zijn eigen lazy-laders - en de keuze ging verloren zodra je van tabblad wisselde.

De naam staat nu in het **pad**, en de server rendert die ene:

```
/projects/mijn-project/deployments/productie
/projects/mijn-project/metrics/productie
```

| Wat je opvraagt | Wat er gebeurt |
|---|---|
| `/projects/<p>/deployments/<naam>` | die deployment; de andere staan niet in de pagina |
| `/projects/<p>/deployments` | de server kiest de eerste op naam en **verwijst door**, zodat de URL daarna zegt welke |
| `/projects/<p>/deployments/<weg>` | een verwijderde deployment valt terug op de eerste, ook met een doorverwijzing |
| `/projects/<p>/deployments?deployment=<naam>` | de vorige vorm; verwijst door naar het pad, zodat een gedeelde link blijft werken |
| een project zonder deployments | het kale tabbladadres, met de melding "Nog geen deployments" |

Welke deployment open staat wordt op **een** plek bepaald: `kies_deployment()` in
`opi/web/lotc_switch.py`, met het pad voor de oude parameter. De route roept hem aan
voordat er iets ontsleuteld wordt, en `deployment_pagina_adres()` zegt waar de pagina hoort
te staan.

**De keuze reist mee tussen de tabbladen.** De tabbalk bouwt zijn adressen met
`project_tab_url(..., deployment=...)`, dus van Deployments naar Metrics blijft dezelfde
deployment staan - een gewone link, en niets dat de browser hoeft te onthouden. De andere
tabbladen krijgen de naam niet: daar heeft dat pad geen route.

**De kiezer wijst naar adressen.** De waarde van een optie is het adres van die deployment
op het tabblad waar je bent, en de optie die de pagina toont krijgt `selected`
(`bg/_deployment-selector.html.j2`). Kiezen is dus navigeren, en de terugknop werkt.

`static/js/deployment_switch.js` - tonen en verbergen, de keuze onthouden in
`sessionStorage`, herstellen uit de URL-hash - is hiermee **vervallen en verwijderd**.

## Klassen en id's

`deployment-<naam>`, `deployment-actions-<naam>`, `argocd-<naam>` en `data-deployment`
zeggen bij WELKE deployment een blok hoort; `deployment-card` bakent het paneel af (zie
`static/css/project-details.css`). `deployment-section`, `is-hidden` op deze blokken en
`#deployments-weergave` zijn weg: die hoorden bij het wisselen in de browser, en dat
bestaat niet meer.

## Toetsen

| Bestand | Wat het bewaakt |
|---|---|
| `tests/test_lotc_deploymentstabel.py` | zoeken, sorteren, de statusregel (RC-31/RC-35), en dat de tabel `columns` draagt |
| `tests/test_lotc_deploymentkiezer.py` | de kiezer staat op een plek, wijst met ADRESSEN naar de deployments, en beide tabbladen renderen er een |
| `tests/test_argocd_overview.py` | twintig rijen = een bevraging; de cache vervalt echt |
| `tests/test_lotc_tabbladen_url.py` | elk tabblad heeft een pad dat bij de projectpagina uitkomt, en de deployment reist mee in de adressen |
| `tests/e2e/test_lotc_deployments_tab.py` | de pagina toont er een, de doorverwijzingen kloppen, en de keuze blijft staan bij het wisselen van tabblad |
| `tests/e2e/test_lotc_deploymentstabel.py` | GEOMETRIE in de browser, plus screenshots |

Die laatste bestaat om een fout die groen was. NLDD maakt van een tabel een CSS-grid;
zonder het attribuut `columns` wordt `grid-template-columns: none` en valt elke cel op een
eigen regel. De vorige ronde leverde zo een tabel op die als lijst rendeerde, met alle
markup-asserties waar. Daarom meet de e2e de POSITIES (koppen naast elkaar, rijen eronder)
en schrijft hij `tests/e2e/screenshots/lotc/bg-deploymentstabel.png` weg om naar te kijken.
