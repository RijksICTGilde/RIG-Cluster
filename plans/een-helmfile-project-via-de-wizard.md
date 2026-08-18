# Een helmfile-project via de wizard: wat is er nodig, en hoe scheiden we het vriendelijk?

Er zijn projecten die niet uit componenten bestaan maar uit een helmfile: `mb-docs-helmfile` en `mb-grist-helmfile`, twee van de 46 in de sandboxrepository. Die zijn met de hand geschreven en zijn via het portaal **wel te bekijken maar niet te maken of te bewerken**.

Deze taak zoekt uit wat daarvoor nodig is, en levert een voorstel voor hoe je dat in de wizard scheidt zonder dat het een tweede, half parallelle wizard wordt.

## Wat er gemeten is

**De vorm loopt parallel aan componenten, maar het is niet hetzelfde.** In `projects/mb-docs-helmfile.yaml`:

- op projectniveau een `helmfile:`-lijst, de catalogus, met per item `name`, `url` (de git-repo van de helm-bron), `ref`, `path`, `helm-values`, `files` en een eigen `services`-lijst;
- op deploymentniveau opnieuw `helmfile:`, de verwijzing, met `reference`, `env-vars` en `helm-values`.

Dat is dezelfde catalogus-plus-verwijzing als bij componenten. **Het schema kent het al** (`helmfile-entry` en `helm-chart` in `opi/schemas/project_v2.json`), en `deployments[].helmfile` en `deployments[].helm-charts` staan er allebei in.

**De weergavekant kan het al, de schrijfkant niet.** `opi/web/router.py:1561` ontcijfert de helm-values van elk helmfile-item voor de projectpagina. Maar `opi/web/router_wizard.py` en de hele `opi/forms/`-laag noemen `helmfile` geen enkele keer. Bekijken werkt dus, aanmaken en bewerken bestaat niet.

**Er zijn twee verschillende dingen die allebei "helm" heten**: `helm-charts` en `helmfile`, elk met een eigen `$def` in het schema. Zoek uit wat het verschil is, waar ze allebei voor bedoeld zijn, en of ze allebei via de UI moeten kunnen. Als er maar één van in gebruik is, zeg dat dan, want dat scheelt de helft.

## Het eerste dat je moet meten, vóór alle ontwerp

**Wat gebeurt er als je zo'n project nu via het portaal bewerkt?**

De wizard en de bewerkdialogen bouwen projectgegevens op uit hun eigen formuliermodel, en dat model kent `helmfile` niet. De vraag is of een gewone bewerking (een teamlid toevoegen, een domein wijzigen) het `helmfile`-blok **stil weggooit**. Als dat zo is, is dat geen ontwerpvraag maar dataverlies dat er vandaag al is, en dan hoort dat als eerste gerepareerd te worden, los van de rest van deze taak.

Meet het op een kopie, niet op `mb-docs-helmfile` zelf.

## De ontwerpvraag

De opdrachtgever vraagt om een vriendelijke scheiding: een eigen helmfile-scherm, of misschien een eigen dienst omdat er veel bij komt kijken. Beantwoord dat met een voorstel, niet met een implementatie.

Drie dingen maken dit anders dan een gewoon component, en ze bepalen het antwoord:

**`helm-values` is een willekeurige geneste boom.** In `mb-docs-helmfile` is dat een blok van tientallen regels diep: autoscaling, pdb, securityContext, per-applicatie aan/uit. Daar valt geen formulier voor te maken, en dat moet je ook niet willen proberen. Een YAML-editor met validatie is waarschijnlijk het eerlijke antwoord, maar zeg wat dat betekent voor de belofte van de wizard, die juist bestaat om YAML te vermijden.

**`files` bevat hele bestanden, inclusief Go-templates.** Het item draagt een `helmfile.yaml.gotmpl` met `{{ toYaml .Values | nindent 8 }}` erin. Dat is code, geen configuratie.

**De helm-values op deploymentniveau zijn AGE-versleuteld, die op projectniveau niet.** Een bewerkscherm moet dus weten welk van de twee het bewerkt en wat er met het geheim gebeurt bij opslaan. Dit is precies waar eerder een realm-wachtwoord bij het opslaan verdween.

**En het helmfile-item draagt zijn eigen `services`-lijst** (`publish-on-web`, `keycloak`, `namespace-postgresql-database`, `minio-storage`, `redis`). Dat is een andere plek dan waar een component zijn diensten heeft. Zoek uit of dat dezelfde betekenis heeft en of de dienstenstap van de wizard daarop kan aansluiten, of dat het een eigen ding is.

## Waar de vraag "eigen dienst?" om draait

Er is inmiddels een uitgewerkt dienstensysteem met haken: een dienst declareert zijn eigen configuratie, formulier, uitleg, validatie en manifesten, en haakt in de UI en de generatie. De verleiding is groot om helmfile daar in te passen.

Onderzoek of dat past, en wees eerlijk als het niet past. Een dienst is iets dat je bij een component of deployment **aanzet**; helmfile is een andere manier om een deployment te vullen, náást componenten. Dat is een ander soort ding. Zie ook `features/futures/tabbladen-via-een-haak.md`, waar dezelfde spanning speelt: niet alles wat uitbreidbaar moet zijn is een dienst.

Kijk in `instructions/services.md` wat een dienst precies mag en moet, en zeg op grond daarvan of dit erin past, erbij hoort, of iets eigens is.

## Wat dit plan oplevert

Een voorstel dat de volgende vragen expliciet beantwoordt:

1. Gooit een bewerking via het portaal vandaag het helmfile-blok weg? (meting, en zo ja: is dat een aparte spoedreparatie)
2. Wat is het verschil tussen `helm-charts` en `helmfile`, en moeten ze allebei?
3. Wordt het een eigen scherm in de wizard, een eigen dienst, of iets anders; met de reden.
4. Hoe ga je om met `helm-values` en `files`, die geen formulier verdragen.
5. Wat er met de versleutelde deployment-values gebeurt bij bewerken en opslaan.
6. Hoe de `services`-lijst binnen een helmfile-item zich verhoudt tot de dienstenstap.
7. Een inschatting van de omvang, in stukken die los te bouwen zijn. Bekijken kan al; misschien is "bewerken zonder weggooien" een kleine eerste stap en "aanmaken via de wizard" een veel grotere.

**Geen implementatie in deze taak**, behalve wanneer punt 1 dataverlies blijkt: repareer dat dan wel, met een test, en meld het apart.

## Verifieerbaar

- De meting van punt 1, op een kopie, met wat er in het bestand overbleef.
- Het antwoord op punt 2 met vindplaats in het schema en in de code die het gebruikt.
- Een voorstel dat een ontwerper of bouwer kan lezen zonder eerst dit projectbestand te hoeven ontcijferen.
