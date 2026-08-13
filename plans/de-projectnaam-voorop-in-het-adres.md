# De projectnaam voorop in het adres

Status: plan, 13 augustus 2026. Klein en scherp begrensd.

## Wat er nu is

RC-92 gaf de tabbladen en de deployment een eigen adres, maar in deze vorm:

```
/projects/deployments/tfc-nfv            /projects/metrics/tfc-nfv/pr-test
```

Het tabblad staat vóór de projectnaam. Gevraagd is het omgekeerde, want het project is waar je bent en het tabblad is wat je erbinnen bekijkt:

```
/projects/tfc-nfv/deployments            /projects/tfc-nfv/metrics/pr-test
/projects/tfc-nfv/deployments/pr-test
```

## Wat er moet gebeuren

Eén plek bouwt die adressen: `project_tab_url()` in `opi/web/lotc_switch.py:448`. De tabbalk, de kruimels, de kerncijfers en de router lezen daar allemaal uit, dus de wijziging zelf is klein.

De routes zijn het echte werk: zes tabbladen, met en zonder deployment (`opi/web/router.py:1254` en verder).

**Let op de valkuil die er al staat.** In het commentaar bij die routes staat waarom de paden LETTERLIJK zijn opgeschreven en niet als `/projects/{tab}/{project_name}`: dat laatste vangt ook `/projects/<naam>/tasks` op, en dan bepaalt de vololgorde van registreren welke route wint. Bij het omdraaien geldt hetzelfde in spiegelbeeld: `/projects/{project_name}/{tab}` zou ook `/projects/details/tfc-nfv` opvangen, met `project_name="details"`.

Schrijf de nieuwe paden dus net zo letterlijk op: `/projects/{project_name}/deployments`, `/projects/{project_name}/deployments/{deployment_name}`, en zo voor de zes. Dan is er geen wildcard die iets anders kan opeten.

**Beslis wat de oude adressen doen.** Ze staan sinds vandaag in de sandbox en kunnen gedeeld zijn. Doorverwijzen is de vriendelijke keuze; laat ze in elk geval niet stil een 404 worden.

## De toets

- `/projects/tfc-nfv/deployments/pr-test` en `/projects/tfc-nfv/metrics/pr-test` tonen die deployment;
- de tabbalk, de kruimels en de kerncijfers wijzen naar de nieuwe vorm, want ze komen uit dezelfde functie;
- wisselen van tabblad houdt de deployment vast, net als nu;
- geen enkel bestaand pad wordt stil een 404;
- `/projects/details/<naam>` werkt nog steeds en wordt niet als project "details" gelezen.

## Waar op te letten

**Niet en passant de tabbladen hernoemen.** Dit gaat over de volgorde van de segmenten, niet over hoe ze heten.

**De e2e-tests dragen deze paden.** Ze staan in `tests/e2e/` en in de vastgelegde schermafbeeldingen; die lopen mee.
