# Van gedeeld naar een eigen databasecluster

Status: plan, 1 september 2026. Aanleiding: er is een project dat een eigen database wil. Beide eindsituaties bestaan al en draaien in productie, maar er is geen weg van de ene naar de andere. Wie vandaag `scope: shared` in `scope: project` verandert, krijgt een leeg toegewijd cluster en een deployment die daarnaar wijst. De data blijft achter op de gedeelde instantie, waar niets hem nog opvraagt. Dat is geen migratie maar dataverlies met een omweg.

Dit plan hoort bij [postgresql-schemas-en-scope.md](postgresql-schemas-en-scope.md) en [rc17-postgres-scope-en-schemas.md](rc17-postgres-scope-en-schemas.md), die het `scope`-veld zelf ontwerpen en de verhuizing van de losse `namespace-postgresql-database`-service beschrijven. Die twee gaan over *hoe je opschrijft waar je database staat*. Dit plan gaat over *hoe je verhuist zonder je data kwijt te raken*, en dat staat in geen van beide.

Het raakt ook [de-connectielimiet-wordt-instelbaar.md](de-connectielimiet-wordt-instelbaar.md), en dieper dan alleen "de waarde moet mee". Op een gedeelde instantie is het aantal verbindingen het enige wat een project voor zichzelf kan zetten; geheugen en volumegrootte zijn eigenschappen van de server die iedereen deelt. Op een toegewijd cluster kan dat alle drie, en `max_connections` wordt dan een eigenschap van dat cluster in plaats van een rem per rol. Een overstap verandert dus niet alleen waar de data staat, maar ook **welke knoppen er bestaan**.

**Scope.** Eén richting: gedeeld naar toegewijd. De omgekeerde weg staat expliciet buiten dit plan; zie "Waar op te letten". Het plan verandert niets aan hoe `scope` gemodelleerd is en niets aan de bestaande provisioning van beide modi. Het voegt een verhuisweg toe.

## Wat er nu is, gemeten

### Beide modi werken al, en er is één beslispunt

`opi/services/postgres_scope.py` is de enige plek waar "waar staat de database van dit project" wordt beantwoord. `postgres_scope()` geeft `"shared"`, `"project"` of `None`, en `project_uses_dedicated_postgres()` is de afgeleide die op acht plekken wordt geraadpleegd: `database_manager`, `delete_project_manager`, `project_manager`, `project_validation`, `backup_router`, `task_handlers_backup`, `project_file_handler` en `schema_migration`.

Dat is gunstig. Er is één waarheid over de plaatsing, dus een migratie hoeft niet acht takken te leren kennen; hij hoeft dat ene antwoord op het juiste moment te laten kantelen.

In productie draaien twee toegewijde clusters, allebei in een `<project>-infrastructure`-namespace:

```
rig-prd-algor-odc-infrastructure/algor-odc-db
rig-prd-mb-docs-helmfile-infrastructure/mb-docs-helmfile-db
```

Plus de gedeelde `rig-prd-operations/rig-db`, waarop 80 databases staan, inclusief die van Keycloak, Forgejo en de mailrelay.

### Wat een deployment van zijn database weet

`opi/services/catalog/postgresql_database/variables.py` levert onder meer `DATABASE_SERVER_HOST`, `DATABASE_SERVER_PORT`, `DATABASE_DB`, `DATABASE_SCHEMA`, `DATABASE_SERVER_USER`, `DATABASE_PASSWORD`, de `_RO`-varianten daarvan, en een reeks `APP_DATABASE_*`-aliassen.

Die waarden komen via secrets in de pods terecht. Een overstap wijzigt in elk geval de host, en afhankelijk van de naamgeving ook de databasenaam en de gebruiker. Elke component die de database gebruikt moet daarna herstarten. Dat is de eigenlijke downtime van deze operatie, niet het kopiëren.

### De generatie zit in de databasenaam

Gemeten op productie: `mpfb_8wh_pr_250` bezit `mpfb_8wh_pr_250`, `mpfb_8wh_pr_250_v1` en `mpfb_8wh_pr_250_v2`. De generatie leeft in de naam van de database, niet in het schema, en `database_generation_service_type()` bepaalt onder welke service-ingang die generatie op de deploymentlaag staat.

Eén deployment kan dus meerdere databases hebben. Een verhuizing die er één meeneemt is niet af.

### De backup weet van welke soort hij kwam

In `opi/core/task_handlers_backup.py` staat:

```python
uses_namespace_db = project_uses_dedicated_postgres(project_data)
source_type = "namespace" if uses_namespace_db else "shared"
```

Een overstap verandert dus de afstamming van de backups. Wat er onder `shared` ligt blijft daar liggen en wordt niet meer aangevuld. Dat is geen bijeffect om later te ontdekken, dat is een besluit dat in de migratie hoort.

## Het model

### Er verhuist ook configuratie, niet alleen data

Dit is het deel dat makkelijk over het hoofd wordt gezien. Bij een gedeelde database declareert de service zelf wat er geldt, en terecht: aan geheugen of volumegrootte valt per project niets te draaien, want die zijn van de server. Bij een toegewijd cluster verschuift een deel van die beslissingen naar de projectconfiguratie, want daar zijn het wél eigenschappen van iets dat het project bezit.

Een overstap voegt die twee dus samen. Wat de service tot dan toe zelf bepaalde wordt deels overgenomen door wat er in het projectdocument staat, en dat betekent drie dingen:

- er moet een **samenvoegregel** zijn die zegt wat wint, net als de voorrangsvolgorde in het andere plan;
- wat uit het projectdocument komt is **gebruikersinvoer** en moet dus gevalideerd worden, niet alleen op vorm maar ook op verdedigbaarheid: een project dat zichzelf 64 GiB toekent hoort te worden tegengehouden;
- de **connectielimiet verandert van soort**. Op de gedeelde instantie is hij een rem per rol tegen een gedeeld plafond; op een eigen cluster is hij een keuze binnen je eigen `max_connections`. Dezelfde waarde betekent aan de twee kanten niet hetzelfde.

Dit is een wijziging in het servicemodel en niet alleen in deze migratie, en daarom staat het in een eigen plan: [een-service-declareert-zijn-speelruimte.md](een-service-declareert-zijn-speelruimte.md). **Dat gaat hieraan vooraf.** Zonder een plek waar een service zijn grenzen declareert en iets wat de projectinvoer daartegen toetst, bedenkt deze migratie dat mechanisme zelf, vanuit één geval.

### Een overstap is een taak met een omkeerpunt

Geen veldwijziging. De volgorde die dat afdwingt:

```
voorvertoning  ->  doelcluster erbij  ->  data kopiëren  ->  omschakelen  ->  (later) opruimen
                                                              ^
                                                        omkeerpunt: hierna
                                                        draait alles op het
                                                        nieuwe cluster, maar
                                                        het oude staat er nog
```

De kern is dat `scope` pas kantelt in de omschakelstap, niet ervoor. Tot dat moment bestaan beide kanten naast elkaar en kost terugvallen niets. Na de omschakeling blijft de oude data staan tot iemand expliciet zegt dat hij weg mag, en dat is een aparte handeling met een eigen bevestiging.

## Wat er moet gebeuren

Vijf stappen. De eerste is read-only en kan meteen; de laatste kan weken later.

### 1. Een voorvertoning die zegt wat er gaat gebeuren

Read-only. Voor het gekozen project: welke databases er zijn inclusief alle generaties, hoe groot ze zijn, welke rollen erbij horen (read-write en `_ro`), welke connectielimieten die rollen nu hebben, welke schema's erin zitten, en welke componenten van welke deployments erop aangesloten zijn en dus zullen herstarten.

Dit is de stap die de operatie voorspelbaar maakt. Wie hem overslaat ontdekt de tweede en derde generatie pas als er iets ontbreekt.

### 2. Het doelcluster erbij, zonder om te schakelen

Provisioneer het toegewijde CNPG-cluster met de `DedicatedPostgresFields` die het project opgeeft (`storage`, `resources`, `instances`), maar laat `scope` op `shared` staan. Beide bestaan dan naast elkaar en het project draait ongestoord door.

Hiervoor is een manier nodig om een doelcluster te provisioneren zonder dat de variabelen al meeverhuizen. Dat is de enige echt nieuwe capaciteit in dit plan.

### 3. Data, rollen en rechten kopiëren

Per database, inclusief elke generatie. Mee moeten: de schema's, de rollen met hun wachtwoorden, de grants, de `search_path`, en de connectielimiet van de rol zoals die op dat moment geldt.

Herhaalbaar maken. Je wilt dit een keer of drie kunnen draaien voordat je omschakelt, zodat de omschakeling zelf alleen nog een kleine delta is.

### 4. De omschakeling

De enige stap met impact: een read-only venster op de bron, de laatste delta overzetten, `scope` op `project` zetten, de secrets herschrijven, en de componenten herstarten in de volgorde die hun afhankelijkheden voorschrijven.

Daarna verifiëren dat er niets meer verbindt met de gedeelde instantie voor dit project. `pg_stat_activity` op `rig-db` is de eenvoudigste toets: staat de oude rol daar nog met sessies, dan is er een component vergeten.

### 5. Opruimen, als aparte handeling

De oude databases en rollen op de gedeelde instantie blijven staan tot iemand ze expliciet weggooit. Met bevestiging, en met een rapport van wat er weg gaat.

Zolang deze stap niet gedaan is, is terugvallen goedkoop: `scope` terug, secrets terug, herstarten.

## De toets

- de voorvertoning noemt **alle** generaties van een database, niet alleen de huidige: te controleren tegen `mpfb_8wh_pr_250` met zijn drie;
- na stap 2 draait het project nog volledig op de gedeelde instantie en is er niets aan zijn secrets veranderd;
- na stap 3 bestaat elk schema, elke rol en elke grant aan beide kanten, en `SELECT rolconnlimit FROM pg_roles` geeft aan beide kanten hetzelfde getal;
- na stap 4 staat er in `pg_stat_activity` op `rig-db` geen enkele sessie meer van een rol van dit project;
- na stap 4 werkt de applicatie zonder dat er iets in de applicatiecode is gewijzigd: dat is de test of `search_path` en de variabelen kloppen;
- een backup die na stap 4 draait gebruikt `source_type = "namespace"` en slaagt;
- terugvallen vóór stap 5 herstelt de oude situatie zonder dataverlies, en dat is een keer echt geoefend en niet alleen opgeschreven;
- stap 5 verwijdert niets zonder bevestiging, en het rapport klopt met wat er daadwerkelijk verdwijnt.

## Waar op te letten

**Het gevaar zit in de afwezigheid, niet in de code.** Vandaag is `scope` gewoon een veld dat je kunt omzetten. Er is geen enkele controle die zegt "let op, hier staat data". Zolang dit plan niet gebouwd is, is dat een valstrik voor de eerste die het probeert. Overweeg als losse, kleine ingreep alvast een blokkade op het omzetten van `scope` bij een project dat databases heeft, met een duidelijke melding. Dat is een middag werk en het voorkomt precies het ongeluk waar dit plan voor bestaat.

**De doelnamespace is de projectnamespace.** Besloten op 1 september, en dat bevestigt besluit 10.6 uit de RC-17-notitie: *"alles gaat naar de projectnamespace, inclusief de drie bestaande"*. Eén plaatsing betekent dat de netwerkpolicy-uitzondering kan vervallen en de verwijderweg eenvoudiger wordt. Migreer dus nooit naar `<project>-infrastructure`, ook niet tijdelijk, want dan doe je de operatie twee keer.

**De connectielimiet moet meeverhuizen, en opnieuw worden gewogen.** Kopieer je alleen data en rollen zonder die waarde, dan komt een project dat juist vanwege verbindingsdruk verhuisde uit op de standaard van 20, en heeft de hele operatie zijn doel gemist. Maar overnemen alleen is ook te weinig: op een eigen cluster met een eigen `max_connections` mag die 60 waarschijnlijk hoger, en is de reden om hem laag te houden verdwenen. Neem de oude waarde over als ondergrens en laat het project hem daarna zelf zetten binnen zijn eigen plafond.

**Backups zijn geen bijzaak.** Na de omschakeling groeit de `shared`-afstamming niet meer en begint er een nieuwe onder `namespace`. Bepaal expliciet hoelang de oude bewaard blijft en of hij nog terugzetbaar moet zijn. Er is eerder een geval geweest waarin backups onder een andere identiteit stonden dan de opruiming verwachtte, waardoor er niets verliep; dezelfde klasse fout ligt hier op de loer.

**Geen omgekeerde weg.** Toegewijd terug naar gedeeld staat buiten dit plan. Bouw het niet "voor de zekerheid": het verdubbelt de testmatrix voor iets waar nog niemand om heeft gevraagd, en de terugvalweg vóór stap 5 dekt het enige realistische scenario af.

**Alles wat ik hier een naam geef is een voorstel.** Er staan in dit plan geen bestaande identifiers voor de nieuwe capaciteit, alleen beschrijvingen. Kies de namen bij het bouwen en laat mijn formuleringen niet doorlekken naar velden of endpoints.

## Een tweede afnemer van dezelfde machinerie

Nu de doelnamespace vaststaat op de projectnamespace, staan de bestaande toegewijde clusters op de verkeerde plek. Gemeten op 1 september draaien er twee, allebei in een `-infrastructure`-namespace:

```
rig-prd-algor-odc-infrastructure/algor-odc-db
rig-prd-mb-docs-helmfile-infrastructure/mb-docs-helmfile-db
```

De RC-17-notitie van 2 augustus noemde er nog drie (`algor-odc`, `mb-grist-helmfile`, `mb-docs-helmfile`). Gemeten is `rig-prd-mb-grist-helmfile-infrastructure` inmiddels volledig leeg: geen cluster, geen pods. Die namespace kan waarschijnlijk gewoon weg, en dat is een opruimactie en geen migratie.

Die twee moeten verhuizen, en dat is **niet dezelfde operatie** als dit plan beschrijft: het is toegewijd naar toegewijd in een andere namespace, niet gedeeld naar toegewijd. Maar de stappen zijn dezelfde vijf: voorvertoning, doel erbij, kopiëren, omschakelen, opruimen. De machinerie uit dit plan is dus precies wat die verhuizing nodig heeft.

Neem die variant niet mee in deze taak, maar bouw de vijf stappen wel zo dat de bron uitwisselbaar is. Dan is de tweede afnemer een kleine toevoeging in plaats van een tweede implementatie.

## Wat hierna nodig is

De losse `namespace-postgresql-database`-service kan weg zodra zijn projecten op `scope: project` staan (RC-17, besluit 10.1). Deze verhuisweg is precies wat dat mogelijk maakt, want zonder migratie kun je die projecten niet omzetten. Dat opruimen is een vervolgtaak, geen onderdeel hiervan.
