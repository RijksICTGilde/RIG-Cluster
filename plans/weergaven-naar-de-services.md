# Weergaven naar de services, net als config en editables

Status: plan, 4 augustus 2026. Niet gebouwd. Aanleiding: de UI wordt binnenkort omgezet, en dan wil je dat een dienst zijn eigen blokken meebrengt in plaats van dat de algemene pagina van elke dienst moet weten.

## Waar we staan, gemeten

RC-5 heeft configuratie en editables naar de diensten gebracht en daar een haak voor gemaakt. Voor weergaven is die haak er ook (`Service.detail_page_sections`), maar de verhuizing is bij twee van de vijftien diensten blijven steken.

- **Verhuisd**: `keycloak` en `invite`. Die leveren hun eigen sjabloon, met de rolcontrole in de dienst zelf.
- **Niet verhuisd**: de rest. Er staan 28 sjablonen in `opi/templates/project-details/`, en daarvan dragen er acht dienstspecifieke kennis terwijl ze in de algemene laag zitten.

Het scherpste voorbeeld staat in `project-details.html.j2` zelf:

```jinja
{% if "attachments" in (project.services | ... ) %}
{% include "project-details/section-attachments.html.j2" %}
{% endif %}
```

De hoofdpagina kent hier een dienst bij naam en controleert of hij gebruikt wordt. Dat is precies de kennis die in de dienst hoort.

## Inventaris: wat moet waarheen

| Sjabloon | Regels | Hoort bij | Wat het nodig heeft |
|---|---|---|---|
| `section-backups.html.j2` + `_backup-snapshots.html.j2` + `_backup-snapshots-one.html.j2` | 97 + 2 delen | backup | Sectie per deployment, plus een route voor het lui nalezen (staat nu op `hx-get /projects/details/{{ project.name }}/backups`) |
| `section-attachments.html.j2` | 37 | attachments | Projectsectie; de `{% if %}` in de hoofdpagina vervalt dan |
| `_db-console-modal.html.j2` | 87 | postgresql-database | Modal, plus de bijbehorende routes onder `/projects/{p}/db-console` |
| `_job-modal.html.j2` | 99 | postgresql-database (jobs draaien tegen de database) | Modal, plus routes onder `/projects/{p}/jobs` |
| `section-deployment-actions.html.j2` | 62 | sleep-mode + postgresql-database | Deels al gedaan: `collect_deployment_actions` levert de knoppen. De rest van het sjabloon noemt die diensten nog bij naam |
| `section-metrics.html.j2` | 44 | metrics-scraper | Sectie per deployment |
| `section-env-vars.html.j2` | 141 | generiek, maar toont per dienst geleverde variabelen | Blijft algemeen; wel voeden vanuit `VariableDefinition` in plaats van uit een lijst in het sjabloon |
| `section-tasks.html.j2` | | generiek, noemt backup bij naam | De backup-specifieke tak naar de dienst |

De overige twintig sjablonen zijn echt algemeen (kop, team, componenten, repositories, danger zone, ArgoCD-status) en blijven waar ze zijn.

## Wat er aan haken bij moet

`detail_page_sections(project_data, user_role)` dekt alleen het projectniveau. Wat ontbreekt:

1. **Een sectie per deployment.** Backups, metrics en deployment-acties horen bij één deployment, niet bij het project. Dat vraagt een tweede haak die per deployment gevraagd wordt, met dezelfde vorm als de bestaande zodat er niets nieuws te leren valt.
2. **Modals en fragmenten die een dienst bezit.** De database-console en de jobs zijn geen sectie maar een modal met eigen routes. Een dienst moet die kunnen meebrengen, inclusief de endpoints, zoals `services_router` dat al doet voor configuratie.
3. **Lui nageladen fragmenten.** Backups worden met `hx-get` opgehaald, en die route staat nu in de algemene router. Een dienst die een sectie levert moet ook zijn eigen fragmentroute kunnen leveren, anders blijft de helft achter.

## Volgorde

Doe dit vóór de UI-omzetting, anders zet je dezelfde blokken twee keer om.

1. De haak voor secties per deployment, met `metrics-scraper` als eerste bewoner: klein sjabloon, één dienst, geen routes.
2. `attachments`, want dat haalt meteen de `{% if %}` uit de hoofdpagina en bewijst dat een dienst zijn eigen zichtbaarheid bepaalt.
3. `backup`, inclusief de fragmentroute; dat is de zwaarste en bewijst punt 3 van de haken.
4. De twee modals van `postgresql-database`, die bewijzen punt 2.
5. Als laatste `section-deployment-actions` en `section-tasks` opschonen: de dienstnamen eruit nu de blokken elders staan.

## Waar op te letten

**Een blok dat in de algemene pagina staat faalt stil.** Toen RC-5 de keycloak-config verplaatste, stopte de keycloak-sectie met renderen omdat het sjabloon nog naar de oude locatie keek. Niemand merkte dat, want een lege sectie ziet eruit als een project zonder Keycloak. Zodra de dienst het blok bezit, hoort dat te falen waar het thuishoort. Neem per verhuizing een test op die vastlegt dát de sectie verschijnt voor een project dat de dienst gebruikt, en niet voor een project zonder.

**De rolcontrole hoort mee te verhuizen.** `keycloak` doet dat goed: `detail_page_sections` geeft niets terug voor iemand die geen admin of owner is, dus het sjabloon hoeft daar niets van te weten. Bij de blokken die nu in de algemene pagina staan zit die controle verspreid, en dat is het moment om ze op één plek te krijgen.

**Niet elk sjabloon hoort naar een dienst.** `section-env-vars` toont variabelen die door diensten geleverd worden, maar het blok zelf is algemeen. Daar is de winst dat het gevoed wordt uit `VariableDefinition`, dat al de "single source of truth" is, in plaats van uit een lijst in het sjabloon.
