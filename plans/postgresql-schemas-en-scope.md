# Meerdere schema's en een scope-keuze voor de PostgreSQL-service

Status: ontwerpnotitie, 2 augustus 2026. Niet gebouwd. **Alle zes de openstaande beslissingen zijn genomen op 2 augustus; zie sectie 10. Waar de tekst hierboven nog een voorstel doet, wint sectie 10.** Aanleiding: een project kan vandaag precies één schema hebben, en de toegewijde database is een aparte service met een eigen namespace terwijl hij feitelijk gewoon een andere plaatsingskeuze is.

Doel van dit document: een plan dat gebouwd kan worden, met de open beslissingen expliciet, zonder het bestaande mechanisme voor generaties, klonen en backups te slopen.

Alle paden zijn relatief aan `operations-manager/python/` tenzij ze met `instructions/`, `features/`, `plans/` of `opi/schemas/` beginnen.

---

## 1. Wat er nu staat, gemeten

Drie namen komen uit hetzelfde paar `(project, deployment)`, in `utils/naming.py`:

| Wat | Functie | Vorm |
|---|---|---|
| Database | `generate_database_name` (regel 539) | `{project}_{deployment}`, met `_v{generation}` bij een kloon |
| Schema | `generate_database_schema` (regel 520) | `{project}_{deployment}`, **zonder** generatie |
| Gebruiker | `generate_database_username` (regel 501) | `{project}_{deployment}` |

Eén database per deployment, één schema erin, één gebruiker. Vier dingen die daar omheen al bestaan en die het ontwerp hieronder dragen:

**Er is al een tweede rol met per-schema-rechten.** `_ensure_readonly_role` (`manager/database_manager.py:320`) maakt `{user}_ro`, doet `grant_readonly_on_schema(database, schema, user)` en zet een eigen `search_path`. Het mechanisme "meerdere rollen, elk met eigen grants op een schema" hoeft dus niet bedacht te worden, alleen gegeneraliseerd.

**De generatie zit in de databasenaam, niet in de schemanaam.** Een kloon maakt `proj_dep_v2` en alles wat daarin staat gaat mee. Dat is precies waarom extra schema's binnen dezelfde database het versiebeheer niet raken.

**Klonen is schema-scoped.** `pg_dump -n` in `connectors/postgres.py`, met een expliciete voorziening dat `-n` geen `CREATE EXTENSION` meeneemt en extensies daarom vooraf worden aangemaakt (regel 1262 en verder). Meer schema's betekent hier dus meer werk, want de kloon kent nu één schemanaam.

**De applicatie hoeft de schemanaam niet te kennen.** `set_role_search_path` (`database_manager.py:242`) zet het schema op de rol, dus een verbinding landt vanzelf in het goede schema. `DATABASE_SCHEMA` is een gemak, geen noodzaak. Dat is de belangrijkste vaststelling van dit document en sectie 4 leunt erop.

Wat er tegenwerkt: **het credential is per deployment, niet per component.** `build_secret_files` in `catalog/postgresql_database/__init__.py` maakt één `DatabaseSecret` voor de hele deployment en elk component krijgt hem. "Wie mag waarbij" vraagt dus per component een eigen rol en een eigen secret, en dat is de grootste ingreep in dit hele plan, groter dan de schema's zelf.

## 2. Scope als veld op de service, niet als aparte service

Vandaag zijn `postgresql-database` en `namespace-postgresql-database` twee services. Ze verschillen niet in wat ze de applicatie leveren maar in waar de database draait: gedeeld op een clusterinstantie, of een eigen CNPG-cluster in `rig-{project}-infrastructure` (`core/cluster_config.py:619`).

Dat is een plaatsingskeuze, geen andere dienst. Voorstel: één service `postgresql-database` met een veld `scope`:

| Waarde | Betekenis | Status |
|---|---|---|
| `shared` | Een database op de gedeelde clusterinstantie | Default; dit is wat `postgresql-database` nu doet |
| `project` | Eén eigen CNPG-cluster per project, gedeeld door alle deployments | Nu `namespace-postgresql-database` |
| `deployment` | Een eigen CNPG-cluster per deployment | Nieuw, niet in de eerste ronde |

`shared` is de default, dus een bestaand project dat niets zet verandert niet van gedrag. Dat is de hele reden om het zo te modelleren: de migratie is een verplaatsing van de bestaande `namespace-postgresql-database`-selectie naar `scope: project`, niet een herbouw.

Wat dit oplost: de manifests en de werking bestaan al voor beide gevallen, maar ze hangen nu aan twee servicetypen, twee configmodellen en twee plekken in de manifestpijplijn. Met een scope-veld staat de keuze op één plek en is `deployment` er later bij te zetten zonder een derde service.

Wat dit kost: `namespace-postgresql-database` heeft een eigen configmodel met `instances`, `storage`, `image`, `registry`, `postInitSQL` en `privileges`. Die velden horen alleen bij `project` en `deployment`, niet bij `shared`. Het samengevoegde model moet dus per scope andere velden accepteren, en dat is precies waar een Pydantic-discriminated union voor is. Zet `scope` als discriminator; dan faalt `storage: 10Gi` op `scope: shared` met een begrijpelijke fout in plaats van stil genegeerd te worden.

**BESLIST (10.1):** het servicetype verdwijnt uiteindelijk, maar niet in de eerste ronde. `scope` komt erbij en `namespace-postgresql-database` blijft gewoon werken; het opruimen is een latere stap zodra de drie projecten die hem gebruiken (`algor-odc`, `mb-grist-helmfile`, `mb-docs-helmfile`) gemigreerd op schijf staan. Dat is dezelfde volgorde als bij `domains` en `invites`, en het hangt aan dezelfde blokkade: zolang het projectschema niet per versie gevalideerd wordt, kan de oude vorm er niet uit.

## 3. Meerdere schema's

Het bestaande schema blijft bestaan onder zijn huidige naam en wordt het default schema. Extra schema's krijgen `{project}_{deployment}_{postfix}`.

**Naamgeving.** Een zelfgekozen korte postfix leest in `psql` en in logs veel beter dan een systeem-increment, dus dat is de voorkeur. De uniciteit binnen een deployment dwing je af met een validator plus een enforcer, in de vorm die `AttachmentIdValidator` en `UniqueInviteKeyEnforcer` al hebben. Houd de postfix kort en streng: kleine letters, cijfers, onderstrepingsteken, beginnend met een letter, en kort genoeg dat de volledige naam onder de 63 tekens blijft die `_truncate_if_needed` nu afdwingt. Let op dat afkappen bij meerdere schema's gevaarlijker is dan bij één: twee lange postfixen kunnen na afkappen dezelfde naam opleveren. Valideer daarom op de volledige naam, niet op de postfix alleen.

**Versies en backups blijven werken zolang de schema's binnen dezelfde database blijven.** De generatie zit in de databasenaam, dus een kloon van `proj_dep` naar `proj_dep_v2` neemt alle schema's mee. Dat is de invariant die dit plan bewaakt: extra schema's mogen nooit in een eigen database landen.

**Wat wel moet meebewegen:** de kloonweg gebruikt `pg_dump -n <schema>` met precies één schemanaam. Dat wordt een lijst, en de extensie-voorbereiding (`connectors/postgres.py:1262`) moet per schema draaien. Dat is de plek waar dit werk echt zit; de rest is naamgeving.

## 4. Variabelen, het lastigste deel

Vandaag levert de service negen variabelen, elk met `APP_`-aliassen: host, port, user, password, user_ro, password_ro, `DATABASE_DB`, `DATABASE_SCHEMA` en de connectiestring `DATABASE_SERVER_FULL`.

Met meerdere schema's is de vraag: wat betekent `DATABASE_SCHEMA` dan nog? Drie mogelijkheden, en de keuze is niet vrij omdat bestaande applicaties er al op leunen.

**Het uitgangspunt dat de vraag kleiner maakt:** een applicatie hoeft de schemanaam niet te kennen. De rol krijgt een `search_path`, dus een gewone query landt in het juiste schema zonder dat de applicatie iets weet. `DATABASE_SCHEMA` bestaat voor het geval een applicatie hem tóch nodig heeft, bijvoorbeeld voor migratietooling die het schema expliciet moet noemen.

Daaruit volgt het voorstel:

- **`DATABASE_SCHEMA` blijft wijzen naar het default schema**, precies zoals nu. Geen bestaande applicatie breekt.
- **De `search_path` van de rol wordt de lijst van schema's die dat component mag**, met het default schema voorop. Een applicatie die niets doet werkt dus door; een applicatie die een tweede schema wil gebruiken kan `SET search_path` of een gekwalificeerde naam gebruiken.
- **Per extra schema komt er een variabele bij, afgeleid van de postfix**: `DATABASE_SCHEMA_{POSTFIX}` in hoofdletters, met de `APP_`-alias ernaast. Dat is voorspelbaar zonder dat de afnemer de gegenereerde naam hoeft te raden, wat precies jouw zorg is.

**De aap uit de mouw:** die naam is afgeleid van door de gebruiker gekozen invoer, dus hij moet een geldige env-variabelenaam opleveren. Dat vraagt dezelfde strengheid als in sectie 3, plus een botsingscontrole tegen de negen bestaande namen en hun aliassen. Een postfix `db` zou anders `DATABASE_SCHEMA_DB` opleveren, wat verwarrend dicht bij `DATABASE_DB` ligt.

**BESLIST (10.2):** nee. Per extra schema komt er precies één variabele, `DATABASE_SCHEMA_{POSTFIX}`, plus de `APP_`-alias. Wie een verbinding op dat schema wil, bouwt hem uit de onderdelen die er al zijn of gebruikt `SET search_path`. Technisch was het triviaal geweest, want `DatabaseSecret` zet de `search_path` al in de query-parameters van de connectiestring (`utils/secrets.py:172`), maar het aantal variabelen in de pod-omgeving weegt zwaarder.

**BESLIST (10.3):** `DATABASE_SCHEMA` wijst naar het primaire schema van dát component, oftewel het eerste in zijn lijst, en de `search_path` begint daar ook. Voor een component dat niets instelt is dat het default schema, dus niets breekt. De variabele betekent daarmee overal hetzelfde: waar dit component standaard in werkt. Dat is niet onvoorspelbaar zoals hier eerst stond, maar volledig bepaald.

## 5. Toegang per component

Dit is de grootste ingreep en hij staat los van de rest; overweeg hem als eigen ronde.

Vandaag krijgt elk component in een deployment hetzelfde credential. Om te voorkomen dat componenten per ongeluk in elkaars schema komen, is een rol per component nodig: `{user}_{component}`, met grants op precies de schema's die dat component in zijn config noemt, en een `search_path` in dezelfde volgorde. Het patroon staat er al in `_ensure_readonly_role`; dit is dezelfde vorm met andere grants.

Wat er wel echt verandert: `build_secret_files` maakt nu één secret per deployment. Dat wordt er één per component, en de manifestgeneratie moet de juiste secret aan de juiste pod koppelen. Reken erop dat dit meer dan alleen de databasecode raakt.

**Terugvalgedrag:** een component dat niets opgeeft krijgt wat het nu krijgt, namelijk toegang tot het default schema. Anders breekt elk bestaand project.

**BESLIST (10.4):** één rol per component, geen leesrecht-variant. De bestaande deployment-brede `_ro`-rol blijft, en die krijgt SELECT op álle schema's van de deployment; anders wordt de database-console blind voor precies de schema's die je toevoegt. Dat is geen keuze maar een gevolg, want `db_console_manager` verbindt als die rol. **Eis die daaruit volgt:** de console moet alle schema's van een deployment kunnen ontsluiten, dus daar komt een schemakeuze in. Vandaag toont hij er één.

## 6. De UI

Beheer van meerdere schema's is een lijst, dus dezelfde vorm als de `additional-clients` van keycloak en de mounts van de storage-services: een `Sequence` in de serviceconfigsectie, met per item de postfix en een omschrijving.

Wat de UI expliciet moet doen, want anders wordt het een voetangel:

- **Tonen wat de volledige naam wordt.** De gebruiker kiest een postfix, maar de database ziet `{project}_{deployment}_{postfix}`. Toon die naam onder het veld zodra hij getypt is, net zoals de wizard dat bij subdomeinen doet.
- **Tonen welke variabele eruit volgt.** Als de postfix `rapportage` `DATABASE_SCHEMA_RAPPORTAGE` oplevert, hoort dat op het scherm te staan, niet in de documentatie. Dat is precies het punt dat een afnemer anders moet raden.
- **Verwijderen is gevaarlijk.** Een schema verwijderen uit de config betekent data weggooien. Dat hoort niet stil te gebeuren bij een gewone opslag. Voorstel: verwijderen uit de lijst haalt het schema níet weg maar markeert het, in de geest van de bestaande `marked_for_deletion`-weg, met een aparte bevestiging.
- **Toegang per component** hoort bij het component, niet bij de service: een meerkeuzeveld op het component met de schema's van dit project. Dat is een options-provider die uit de serviceconfig leest, precies zoals de invite-service er een krijgt voor realm-rollen.

**BESLIST (10.5):** strikt project-breed. De lijst staat in de projectconfig en elke deployment krijgt dezelfde schema's, elk in zijn eigen database. Daarmee zijn de variabelen overal gelijk en blijft applicatieconfiguratie één op één overdraagbaar tussen acceptatie en productie. Een deployment kan dus geen eigen schema toevoegen.

## 7. De namespace van de toegewijde database

Losstaand van de schema's, en het hoort bij scope uit sectie 2.

De toegewijde database staat nu in `rig-{project}-infrastructure`, naast de projectnamespace. Dat is verdedigbaar als je hem als infrastructuur ziet, maar hij is per project, dus je hebt twee namespaces met dezelfde levensduur en dezelfde eigenaar. In de projectnamespace zetten kan, en ArgoCD heeft daar geen moeite mee.

Drie dingen om te meten voordat dat verhuist, want ze bepalen of het echt eenvoudiger wordt:

1. **De netwerkpolicies.** De tenant-baseline staat nu expliciet verkeer toe naar de infrastructuurnamespace. Verhuizen mag die uitzondering opruimen, maar dat moet dan ook echt gebeuren, anders houd je een gat.
2. **De resourcequota.** Een database in de projectnamespace telt mee in het quotum van het project. Dat is een zichtbare gedragswijziging voor gebruikers en geen implementatiedetail.
3. **De verwijderweg.** Ruimt die vandaag de infrastructuurnamespace als geheel op? Zo ja, dan verdwijnt die vangnetfunctie bij een verhuizing en moet het opruimen expliciet worden.

**BESLIST (10.6):** alles gaat naar de projectnamespace, inclusief de drie bestaande. Eén plaatsing, dus de netwerkpolicy-uitzondering kan weg en de verwijderweg wordt eenvoudiger. Maar dat betekent een datamigratie op drie productiedatabases (`algor-odc`, `mb-grist-helmfile`, `mb-docs-helmfile`) met een CNPG-cluster en zijn PVC's, dus **doe dat als eigen traject met een terugvalplan, niet als onderdeel van dit plan**. De drie controles hierboven blijven staan en gaan daaraan vooraf.

## 8. Volgorde

De onderdelen zijn niet even groot en niet even riskant, dus in deze volgorde:

1. **Scope-veld** (sectie 2), met alleen `shared` en `project`, en de migratie van `namespace-postgresql-database`. Geen gedragswijziging, wel de basis voor de rest.
2. **Meerdere schema's** (secties 3 en 4), inclusief de kloonweg die een lijst gaat dragen. Nog steeds één credential per deployment, dus alle componenten mogen alles; dat is precies het huidige gedrag.
3. **UI voor schema's** (sectie 6, zonder het component-deel).
4. **Toegang per component** (sectie 5), als eigen ronde, want die raakt de secretgeneratie.
5. **Namespace-verhuizing** (sectie 7), los van alles.
6. **Scope `deployment`**, als er vraag naar is.

Stap 1 en 2 zijn onafhankelijk van elkaar; 3 hangt aan 2; 4 hangt aan 2 en 3.

## 9. Wat dit plan bewaakt

Drie invarianten die bij elke stap moeten blijven gelden, en waarop de verificatie zich richt:

- **Extra schema's leven in dezelfde database als het default schema.** Zodra ze eigen databases krijgen, breken generaties, klonen en backups.
- **Een project dat niets nieuws opgeeft, verandert niet.** `shared` is de default, `DATABASE_SCHEMA` blijft het default schema, een component zonder schemakeuze houdt wat het had.
- **Naamgeving faalt luid.** Een botsende schemanaam, een postfix die na afkappen samenvalt met een andere, of een variabelenaam die een bestaande overschrijft: alle drie horen bij het opslaan te falen met een leesbare fout, niet bij het uitrollen.

Verificatie per stap in de vorm die deze codebase gebruikt: alle productiebestanden inlezen, `migrate_to_latest()` in memory, dan `validate_project_schema` en `validate_service_configs`, en daarnaast `instructions/service-review-checklist.md` als sluitstuk.

## 10. Genomen beslissingen (2 augustus 2026)

1. **`namespace-postgresql-database` blijft voorlopig bestaan.** `scope` komt erbij, het servicetype blijft werken, en het opruimen is een latere stap zodra de drie projecten gemigreerd op schijf staan.
2. **Geen connectiestring per schema.** Alleen `DATABASE_SCHEMA_{POSTFIX}` met zijn `APP_`-alias.
3. **`DATABASE_SCHEMA` is het primaire schema van het component**, oftewel het eerste in zijn lijst. Voor een component zonder eigen keuze is dat het default schema.
4. **Eén rol per component, geen leesrecht-variant.** De deployment-brede `_ro` blijft en krijgt SELECT op alle schema's. Daaruit volgt de eis dat de database-console alle schema's van een deployment moet kunnen tonen.
5. **Schema's zijn strikt project-breed.** Een deployment kan er geen toevoegen.
6. **Alle toegewijde databases gaan naar de projectnamespace, ook de bestaande**, maar als eigen traject met een terugvalplan, want het is een datamigratie op drie productiedatabases.
