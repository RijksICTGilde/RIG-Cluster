# De connectielimiet wordt instelbaar

Status: plan, herzien 17 september 2026. Niet gebouwd. De blokkade is weg: `een-service-declareert-zijn-speelruimte.md` is gebouwd en gemerged als RC-168, dus `IntegerSetting` en `setting_field()` staan klaar.

Aanleiding van de herziening: de grenzen gaan naar 1 tot en met 500, het veld krijgt een keuzelijst met vaste stappen die een afwijkende waarde opneemt, en het bijwerken van een bestaande rol wordt een expliciete controle in plaats van een onvoorwaardelijke schrijfactie.

Oorspronkelijke aanleiding: 1 september 2026. Aanleiding: het MOZa-team zette een magazijn-simulator neer die 98 magazijnen simuleert, elk met een eigen sessie naar de database. Die viel om, en de databaseconsole wilde niet meer starten. De oorzaak bleek niet `max_connections` (250, met een piek van 91 in 24 uur) maar een **rollimiet van 20 die hardgecodeerd in OPI staat**. In de postgres-logs stonden 1023 keer `FATAL: too many connections for role "mpfm_w3h_pr_250"` op één dag, alle 1023 op die ene rol, met een uitbarsting van 26 weigeringen in 0,7 seconde toen de simulator al zijn magazijnen tegelijk opstartte.

Als noodgreep staat die ene rol nu handmatig op 60 (`ALTER ROLE mpfm_w3h_pr_250 CONNECTION LIMIT 60`, 1 september 12:25 UTC). Dat overleeft geen heraanmaak van de gebruiker, want OPI zet er bij `CREATE USER` opnieuw 20 neer. Deze taak maakt er een echte instelling van.

Dit plan leunde op [een-service-declareert-zijn-speelruimte.md](een-service-declareert-zijn-speelruimte.md), en **dat is inmiddels gebouwd** (RC-168, gemerged). Daarmee ligt het gereedschap klaar en hoeft deze taak er niets van te bouwen:

- `IntegerSetting(minimum=..., maximum=..., default=..., path=..., label=...)` in `opi/services/catalog/config_settings.py` is de declaratie van de speelruimte. De docstring noemt een connectielimiet al als voorbeeld;
- `IntegerSetting.check()` doet de toetsing, inclusief de val dat `bool` in Python een `int` is;
- `IntegerSetting.latitude()` levert de hulptekst, zodat het scherm en de validatie niet uit elkaar kunnen lopen;
- `setting_field(setting, service, layer, widget=..., values_provider=...)` in `opi/forms/visualizers/config_setting_fields.py` maakt het wizardveld uit diezelfde declaratie, met `setting.yaml_path(service, layer)` als pad.

Gebruik die machinerie. Zet je de drie getallen alsnog als losse waarden in een pydantic-model, dan staan ze op drie plekken uit elkaar te lopen: in het model, in de wizard en in de validatie.

**Scope.** Dit plan raakt `opi/services/catalog/postgresql_database/`, `opi/connectors/postgres.py` en `opi/manager/database_manager.py`, plus de twee drift-locked schemafragmenten van die service. Het verandert **niets** aan `max_connections` van de databaseserver zelf: dat is een aparte, geplande ingreep met een herstart en hoort niet in deze taak. Het voegt ook **geen rechtencheck** toe; zie "Waar op te letten".

## Besluiten, vastgesteld

Deze vier waren open en zijn op 17 september 2026 vastgesteld. Ze staan vast: bouw ze zo, en kom terug met een vraag in plaats van met een eigen invulling als een van de vier in de weg blijkt te zitten.

| keuze | vastgesteld op | waarom |
|---|---|---|
| veldnaam | `connection-limit` | **Vastgesteld.** Sluit aan bij de PostgreSQL-term `CONNECTION LIMIT` en bij de kebab-case van de andere configvelden. Hij loopt door tot in de schemafragmenten, dus wijk er niet van af. |
| minimum, maximum, standaard | 1, 500, 20 | **Vastgesteld op 17 september 2026**, niet langer mijn invulling. Alle drie hardgecodeerd in het servicepakket als `IntegerSetting`, niet als losse getallen in een pydantic-model. Lees bij "Waar op te letten" wat een maximum van 500 betekent voor een server die op 250 staat. |
| de stappen in de keuzelijst | 10, 20, 40, 50, 75, 100, 150, 200, 250, 500 | **Vastgesteld.** Een gemaksgreep, geen grens: elke waarde van 1 tot en met 500 blijft geldig en een afwijkende waarde wordt in de lijst opgenomen. Zie stap 5. |
| de `_ro`-rol | volgt **wel** mee | Beide rollen van een deployment krijgen dezelfde limiet. Eén regel om uit te leggen, en een read-only-rol die op 20 blijft steken terwijl de read-write-rol op 80 staat is een verrassing die niemand verwacht. |

## Wat er nu is, gemeten

### De 20 staat op één plek, en alleen bij aanmaak

`opi/connectors/postgres.py:499`:

```python
create_sql = f"CREATE USER {quoted_username} WITH PASSWORD '{escaped_password}' CONNECTION LIMIT 20"
```

Hardgecodeerd, geen parameter, nergens instelbaar. Gemeten op productie: alle negen `mpfm_*`- en `mpfb_*`-rollen staan op 20, inclusief de vier `_ro`-varianten.

Belangrijker is dat dit **alleen bij `CREATE USER`** gebeurt. Een bestaande rol krijgt nooit een nieuwe waarde, hoe vaak een project ook herverwerkt wordt. Dat is de val: een configveld toevoegen zonder dit te repareren verandert niets voor alles wat al draait.

### De rol is per deployment, de config staat op het project

De rolnaam is `<project>_<deployment>`, dus `mpfm_w3h_pr_250` hoort bij deployment `pr-250` en `mpfm_w3h_test` bij `test`. Elke deployment heeft zijn eigen rol en dus zijn eigen limiet van 20.

Heeft een deployment meerdere databases, dan delen die samen die ene 20: de limiet zit op de rol, niet op de database. Gemeten: `mpfb_8wh_pr_250` bezit `mpfb_8wh_pr_250`, `_v1` en `_v2` en heeft één limiet van 20 voor alle drie.

De gebruikersconfig van deze service staat vandaag op de **projectlaag** (`services[{postgresql-database}].config`), als een discriminated union op `scope`. Van deze service draagt de **deploymentlaag** (`deployments[*].services[{postgresql-database}].config`) nu uitsluitend clone-state die `opi/manager/revision_manager.py` schrijft.

Dat is echter een eigenaardigheid van deze ene service, niet van het platform. `ConfigLayer.DEPLOYMENT` is een regulier niveau naast `PROJECT`, `COMPONENT` en `DEPLOYMENT_COMPONENT`, tien services declareren er config op, en `cross_domain_access` heeft er volwaardige gebruikersgerichte editables op staan via `config_path(ConfigLayer.DEPLOYMENT, ServiceType.CROSS_DOMAIN_ACCESS, "config", ...)`. Een deployment is dus gewoon bewerkbaar, en de haak die we nodig hebben bestaat al. Deze taak gebruikt hem, hij bouwt hem niet.

### Er is al een idempotente tak om op aan te haken

`opi/manager/database_manager.py:314`, in `create_or_update_database_user`:

```python
create_result = await postgres_conn.create_user(...)
if create_result["status"] == "exists":
    update_result = await postgres_conn.update_user_password(...)
    if database_privileges:
        await postgres_conn.update_user_privileges(...)
```

Die `exists`-tak werkt wachtwoord en privileges al idempotent bij op elke herverwerking. Daar hoort het reconciliëren van de limiet thuis. Er hoeft dus geen nieuwe structuur voor te komen.

## Het model

### De service declareert de speelruimte, het project kiest een waarde

Vandaag staat de 20 in `postgres.py`, dus in de laag die alleen maar praat met de buitenwereld. Dat is de verkeerde plek: `instructions/services.md` zegt dat de connector uitvoert en de service de declaratieve thuisbasis van de beslissing is, precies zoals dat bij resource-tuning en deployment-health geregeld is.

De scheiding die daaruit volgt:

| wat | waar | waarom |
|---|---|---|
| minimum, maximum, standaard | **hardgecodeerd in het servicepakket** | Dat is een platformbeslissing over wat verantwoord is, geen keuze van een project. Niemand mag hem van buitenaf oprekken. |
| de gekozen waarde | het projectbestand, op project- of deploymentniveau | Dat is wel een keuze van het project, binnen de door de service afgegeven ruimte. |
| het toepassen | de connector, als parameter | `create_user` krijgt een `connection_limit` mee en kent verder geen enkele standaard. |

Zet je die grenzen als losse getallen in het pydantic-model, dan staan ze op drie plekken uit elkaar te lopen: in het model, in de wizard en in de validatie. Eén declaratie op de service, waar alle drie uit putten.

Dit geldt voor de gedeelde database net zo goed als voor een toegewijde. Een project op de gedeelde instantie mag zeker een limiet opgeven; wat het niet mag is buiten de speelruimte komen die de service afgeeft.

### Drie lagen, met een vaste voorrangsvolgorde

```
deployment-override   >   projectstandaard   >   servicestandaard (20)
```

De deployment wint van het project, het project wint van wat de service zelf declareert. Wie niets invult houdt exact het gedrag van vandaag.

```yaml
services:
  - postgresql-database:
      config:
        connection-limit: 30      # standaard voor elke deployment van dit project

deployments:
  - name: pr-250
    services:
      - postgresql-database:
          config:
            connection-limit: 80  # alleen deze deployment
  - name: test
    services:
      - postgresql-database: {}   # krijgt 30, de projectstandaard
```

Eén functie berekent dat, op één plek. Verspreid je die logica, dan lopen de wizard, de provisioning en de API uit elkaar.

## Wat er moet gebeuren

Vijf stappen, in deze volgorde.

### 1. Het veld op beide lagen

In `opi/services/catalog/postgresql_database/config_model.py`. Op de projectlaag hoort het bij **zowel** `SharedScopeConfig` als `ProjectScopeConfig`, want de rol bestaat in beide scopes: dus via een gedeelde basisklasse, niet twee keer los gedefinieerd. Op de deploymentlaag komt het erbij op `PostgresqlDatabaseConfig`.

Op de projectlaag staat het naast `schemas`, in dezelfde databaseconfig: dat is waar een gebruiker de database van zijn project instelt, en waar hij het volgens de aanvraag verwacht.

Beide `int | None = None`, waarbij `None` "niets gezegd" betekent en niet "nul". Op de deploymentlaag betekent `None` bovendien "volg het project", en dat is wat stap 2 uitrekent.

De grenzen horen niet in dit model. Declareer ze eenmaal als `IntegerSetting(minimum=1, maximum=500, default=20, ...)` in het servicepakket en laat het model, de wizard en de foutmelding daaruit putten. Schrijf je hier `Field(ge=1, le=500)`, dan heb je de grenzen alsnog verdubbeld en loopt de melding van pydantic naast die van `IntegerSetting.check()`.

### 2. Eén samenvoegfunctie

Een functie die project- en deploymentwaarde en de platformstandaard tot één getal herleidt, volgens de volgorde hierboven. Woont bij de service, niet in de manager, want het is een eigenschap van de configuratie.

### 3. Toepassen, ook op bestaande rollen, en alleen als het nodig is

`CONNECTION LIMIT 20` verdwijnt uit `postgres.py:499` en wordt een parameter van `create_user`. De connector kent geen standaard meer: de waarde komt van de service.

In `database_manager.py` geeft `create_or_update_database_user` de berekende waarde mee. De bestaande `exists`-tak, die vandaag al wachtwoord en privileges idempotent bijwerkt, krijgt er het reconciliëren van de limiet bij. Dat geldt voor **beide** rollen: de read-write-rol en de `_ro`-rol op regel 385 krijgen dezelfde waarde.

**Vergelijken voor je schrijft.** De vraag was of een onvoorwaardelijke `ALTER ROLE` niet net zo makkelijk is. Dat is het bijna, maar doe het niet:

```sql
SELECT rolconnlimit FROM pg_roles WHERE rolname = $1;
```

Dat is één goedkope query, en alleen bij een verschil volgt `ALTER ROLE <rol> CONNECTION LIMIT <n>`. Drie redenen om die vergelijking wel te doen:

- een `ALTER ROLE` neemt een lock op de rol. Onnodig, maar niet gratis op een gedeelde server waar elk project langskomt;
- het maakt de logregel betekenisvol. "limiet van 20 naar 80 gezet" is een gebeurtenis; dezelfde regel bij elke herverwerking is ruis waarin je de echte wijziging niet meer terugvindt;
- het houdt het reconciliëren van de limiet los van de wachtwoordtak ernaast. Zie "Waar op te letten": die tak zet vandaag ook een nieuw wachtwoord, en hoe minder je daar onvoorwaardelijk aanraakt, hoe kleiner de kans dat een configwijziging ongemerkt credentials roteert.

De uitkomst hoort in de terugkoppeling van de taak: ongewijzigd, of van welke waarde naar welke.

**Dit geldt voor de gedeelde database en voor een toegewijde.** De rol is in beide gevallen `<project>_<deployment>` en de weg loopt door dezelfde `create_or_update_database_user`. Schrijf de reconciliatie dus op de rol en niet op iets wat alleen in de gedeelde opstelling bestaat, dan werkt een eigen projectdatabase later zonder tweede implementatie. Een toegewijde database is geen onderdeel van deze taak, maar mag er ook niet door uitgesloten worden.

### 4. Schemafragmenten opnieuw genereren

`postgresql-database.v1.0.json` en `postgresql-database.deployment.v1.0.json` zijn drift-locked en worden door tests bewaakt. Beide moeten opnieuw gegenereerd en meegecommit.

Het veld is optioneel met een standaard, dus er is **geen datamigratie** nodig en de schema-versie hoeft niet omhoog. Een bestaand projectbestand valideert ongewijzigd.

### 5. De wizard: een keuzelijst die een afwijkende waarde opneemt

Het veld is vrij invulbaar binnen 1 tot en met 500, maar wie het in het scherm zet krijgt geen leeg getallenvak. Hij krijgt een keuzelijst met vaste stappen:

```
10, 20, 40, 50, 75, 100, 150, 200, 250, 500
```

Die lijst is een gemaksgreep, geen grens. De grens is en blijft de `IntegerSetting`: een waarde van 37 is geldig, komt door de validatie heen, en hoort dus ook getoond te kunnen worden.

**Wat er gebeurt bij een waarde die niet in de lijst staat.** Een project kan via de API of door het projectbestand met de hand te bewerken op 37 uitkomen. Opent daarna iemand de wizard, dan mag die waarde niet stil verdwijnen of terugvallen op de dichtstbijzijnde stap. Dat is precies de klasse fout die `tests/test_modal_noop_roundtrip.py` bewaakt: een rondgang door een modal die een waarde weggooit.

De oplossing loopt via de values provider, want die bepaalt wat de lijst bevat:

- registreer een `ConnectionLimitOptionsProvider` naast de andere providers in `opi/forms/visualizers/providers.py`. `StorageSizeOptionsProvider` is de vorm om over te nemen: `options_source: ClassVar[OptionsSource | None] = None`, want de lijst is voor elk project gelijk, plus een `get_options()` die `{"value": ..., "label": ...}` teruggeeft;
- de provider zet de vaste stappen neer **en voegt de huidige waarde toe als die er niet bij zit**, op de juiste plaats in de oplopende volgorde. Merk zo'n opgenomen waarde herkenbaar, bijvoorbeeld als `37 (eigen waarde)`, zodat een lezer ziet dat het geen platformkeuze is;
- koppel hem met `setting_field(..., widget=WidgetType.SELECT, values_provider="ConnectionLimitOptionsProvider")`. De docstring van `setting_field` beschrijft precies deze combinatie: een select heeft een provider nodig, en de validator blijft hoe dan ook de declaratie.

**Uit te zoeken bij het bouwen, want hier zit de enige echte onbekende.** De provider moet de huidige waarde kennen om hem te kunnen opnemen. `resolve_options_for_editable(editable, context)` in `opi/forms/visualizers/bridge.py` geeft een `context` door aan `_resolve_options`, en in dezelfde functie wordt de waarde uit de YAML gelezen als `raw_value`. Ga na of die waarde de provider al bereikt. Zo niet, dan is dat de enige plek waar deze taak aan het formulierraamwerk zelf komt, en dan hoort in de PR te staan wat er is toegevoegd en waarom het niet anders kon. Bouw er geen tweede weg omheen: een provider die zelf het projectbestand gaat lezen is precies de dubbeling die `instructions/services.md` wil voorkomen.

**Beide lagen krijgen het veld.** Op de projectlaag hoort het bij de databaseconfig waar `schemas` ook staat, want dat is waar een gebruiker de database van het project instelt. Op de deploymentlaag is `opi/services/catalog/cross_domain_access/editables.py` de referentie: die vormt het yaml-pad met `config_path(ConfigLayer.DEPLOYMENT, ServiceType.X, "config", *segments)`. Neem dat patroon over in plaats van een pad met de hand te schrijven, anders lopen de editable en het schemafragment uit elkaar.

**Toon op de deploymentlaag wat er geldt als je niets invult.** De projectwaarde is daar de basis, en een leeg veld betekent "volg het project", niet "nul". Laat dat zien, zodat een gebruiker niet hoeft te raden waar de 30 vandaan komt die zijn deployment gebruikt.

Plus een alinea in `help.md` die uitlegt wat de limiet betekent, dat meerdere databases van één deployment hem delen, dat de `_ro`-rol dezelfde waarde krijgt en dat een hogere waarde ten koste gaat van de gedeelde ruimte.

## De toets

- een projectbestand zonder `connection-limit` levert een rol met `rolconnlimit = 20`, precies als vandaag;
- `connection-limit: 30` op het project levert 30 voor élke deployment van dat project;
- een deployment die er 80 van maakt krijgt 80, terwijl de andere deployments van hetzelfde project op 30 blijven;
- **een bestaande rol volgt een gewijzigde waarde**: zet de config om, herverwerk het project, en `SELECT rolconnlimit FROM pg_roles WHERE rolname = '<project>_<deployment>'` geeft het nieuwe getal. Dit is de assertie die vandaag zou hebben gefaald en de reden dat stap 3 bestaat;
- **een herverwerking zonder wijziging stuurt geen `ALTER ROLE`.** Meetbaar op de query, niet op het eindresultaat: het eindresultaat is in beide gevallen gelijk, en juist dat maakt een onvoorwaardelijke schrijfactie zo makkelijk over het hoofd te zien;
- `connection-limit: 0` en `connection-limit: 501` worden geweigerd door de `IntegerSetting`, met de melding die `check()` opstelt, niet stil afgekapt;
- `connection-limit: true` wordt geweigerd. In Python is `bool` een `int`, en `IntegerSetting.check()` vangt dat af; een test die dat vastlegt hoort erbij;
- de `_ro`-rol van diezelfde deployment krijgt hetzelfde getal als de read-write-rol, ook bij een wijziging achteraf;
- de keuzelijst toont de tien vaste stappen;
- **een waarde van 37, gezet via de API, verschijnt in de lijst en overleeft een rondgang door de wizard.** Dit is de kern van stap 5. `tests/test_modal_noop_roundtrip.py` is de bestaande detector voor die klasse fouten;
- `grep -rn "CONNECTION LIMIT 20" opi/` levert niets meer op: de standaard staat in het servicepakket;
- de drift-tests op de twee schemafragmenten zijn groen.

## Waar op te letten

**Wat een gebruiker schrijfbaar krijgt, en wat dat tegenhoudt.** De schrijfweg zelf is niet nieuw: `ConfigLayer.DEPLOYMENT` bestaat, wordt door tien services gebruikt en heeft in `cross_domain_access` al gebruikersgerichte editables. Nieuw is uitsluitend dat een gebruiker voor déze service een waarde op die laag gaat zetten, en dat die waarde rechtstreeks een `ALTER ROLE` op de gedeelde productiedatabase stuurt. Wat dat tegenhoudt is op dit moment **alleen de speelruimte die de service declareert**: 1 tot en met 500. Er is geen rechtencheck, geen approval-hook en geen quotum per project. Dat is een bewuste keuze voor nu, want een gebruiker mag dit vandaag zelf bepalen, maar het moet opgeschreven staan en niet per ongeluk zo blijven. De rechtencheck is een aparte, opvolgende taak.

**Bij een maximum van 500 is de bovengrens geen veiligheidsgrens meer.** Dit moet iemand hardop gezegd hebben voordat dit gebouwd wordt. De server staat op `max_connections = 250`. Omdat de `_ro`-rol meeloopt, kan één deployment die 500 vraagt er 1000 opeisen, en dat is vier keer de hele server. Bij het oude voorstel van 100 was de bovengrens nog een rem die één project ervan weerhield de boel in zijn eentje om te trekken; bij 500 is hij dat niet meer. Wat er dan resteert is een bescherming tegen een tikfout, en een rem die pas afgaat als het al misgaat, namelijk `max_connections` zelf.

Dat is geen reden om 500 niet te bouwen: het getal is bewust gekozen en de simulator die dit plan uitlokte laat zien dat 20 te weinig is. Het is wel de reden dat de twee opvolgende taken onderaan geen luxe zijn. Een quotum per project en een rechtencheck waren bij 100 nog uitstelbaar; bij 500 zijn ze de enige dingen die overinschrijving tegenhouden. Overweeg om dit niet naar productie uit te rollen voordat ten minste `max_connections` mee omhoog is.

**De globale limiet is een andere taak, met downtime.** `max_connections` is een postmaster-parameter en kan niet warm herladen. `rig-db` draait `instances: 1`, dus verhogen betekent een herstart en daarmee downtime voor Keycloak, Forgejo, de mailrelay en élke projectdatabase tegelijk. Bovendien past het niet zomaar in het geheugen: de pod gebruikt 744 MiB bij 73 verbindingen tegen een limiet van 2 GiB, en lineair doorgetrokken kom je bij 500 verbindingen rond 3,6 GiB uit. De geheugenlimiet moet dus mee omhoog in hetzelfde pakket. En de Cluster-CR valt onder de ArgoCD-app `production-infrastructure`.

**Reconciliëren mag geen wachtwoordwissel uitlokken.** De `exists`-tak zet vandaag ook een nieuw wachtwoord. Als het aanpassen van een limiet daar ongemerkt in meelift, roteert een configwijziging de credentials van een draaiende deployment. Controleer of dat pad al zo werkt en of dat gewenst is; het is geen onderdeel van deze taak om het te veranderen, wel om het niet erger te maken.

**De handmatige 60 op `mpfm_w3h_pr_250` is tijdelijk.** Zodra dit uitgerold is hoort die waarde in het projectbestand van `mpfm-w3h` te staan, anders verdwijnt hij bij de eerstvolgende heraanmaak van die gebruiker en staat het MOZa-team weer stil.

## Wat hierna nodig is

**Een quotum per project en een rechtencheck.** Bij een bovengrens van 500 is dit geen nette afronding meer maar de enige echte rem. Zie de waarschuwing hierboven.

**`max_connections` omhoog, met de geheugenlimiet erbij, als één gepland pakket via git en ArgoCD.** Los van deze taak, met downtime.

**Een eigen projectdatabase.** Deze taak zet de limiet op de rol en loopt via `create_or_update_database_user`, dus een toegewijde database erft het gedrag zonder tweede implementatie. Toets dat als die er komt; ga er niet van uit.

**De handmatige 60 op `mpfm_w3h_pr_250` in het projectbestand zetten.** Zodra dit uitgerold is, anders verdwijnt die waarde bij de eerstvolgende heraanmaak van die gebruiker en staat het MOZa-team weer stil. Dit hoort in de uitrolstap, niet in de bouwtaak.
