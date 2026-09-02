# De connectielimiet wordt instelbaar

Status: plan, 1 september 2026. Aanleiding: het MOZa-team zette een magazijn-simulator neer die 98 magazijnen simuleert, elk met een eigen sessie naar de database. Die viel om, en de databaseconsole wilde niet meer starten. De oorzaak bleek niet `max_connections` (250, met een piek van 91 in 24 uur) maar een **rollimiet van 20 die hardgecodeerd in OPI staat**. In de postgres-logs stonden 1023 keer `FATAL: too many connections for role "mpfm_w3h_pr_250"` op één dag, alle 1023 op die ene rol, met een uitbarsting van 26 weigeringen in 0,7 seconde toen de simulator al zijn magazijnen tegelijk opstartte.

Als noodgreep staat die ene rol nu handmatig op 60 (`ALTER ROLE mpfm_w3h_pr_250 CONNECTION LIMIT 60`, 1 september 12:25 UTC). Dat overleeft geen heraanmaak van de gebruiker, want OPI zet er bij `CREATE USER` opnieuw 20 neer. Deze taak maakt er een echte instelling van.

Dit plan leunt op [een-service-declareert-zijn-speelruimte.md](een-service-declareert-zijn-speelruimte.md), waarin een service zijn ondergrens, bovengrens en standaard declareert en de projectinvoer daartegen wordt getoetst. **Dat gaat hieraan vooraf.** Wordt dat niet eerst gebouwd, dan komen de drie getallen hieronder alsnog als losse waarden in een pydantic-model terecht, en dat is precies wat dit plan zegt niet te doen.

**Scope.** Dit plan raakt `opi/services/catalog/postgresql_database/`, `opi/connectors/postgres.py` en `opi/manager/database_manager.py`, plus de twee drift-locked schemafragmenten van die service. Het verandert **niets** aan `max_connections` van de databaseserver zelf: dat is een aparte, geplande ingreep met een herstart en hoort niet in deze taak. Het voegt ook **geen rechtencheck** toe; zie "Waar op te letten".

## Besluiten die ik heb ingevuld

Vier keuzes waren nog open. Ik heb ze ingevuld zodat dit plan een contract is en de bouwer niet hoeft te gokken. Corrigeer ze voordat dit naar de bouw gaat.

| keuze | ingevuld op | waarom |
|---|---|---|
| veldnaam | `connection-limit` | **Dit is mijn voorstel, geen bestaande naam.** Sluit aan bij de PostgreSQL-term `CONNECTION LIMIT` en bij de kebab-case van de andere configvelden. Vervang hem gerust; hij loopt door tot in de schemafragmenten. |
| minimum, maximum, standaard | 1, 100, 20 | **Alle drie hardgecodeerd in het servicepakket**, niet als losse getallen in een pydantic-model. De service bepaalt de speelruimte, het project kiest daarbinnen een waarde. Zie "Het model". |
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

Beide `int | None = None`, waarbij `None` "niets gezegd" betekent en niet "nul". De grenzen komen uit de declaratie op de service, niet als losse getallen in het model: valideer tegen die ene bron, zodat het model, de wizard en de foutmelding niet uit elkaar kunnen lopen.

### 2. Eén samenvoegfunctie

Een functie die project- en deploymentwaarde en de platformstandaard tot één getal herleidt, volgens de volgorde hierboven. Woont bij de service, niet in de manager, want het is een eigenschap van de configuratie.

### 3. Toepassen, ook op bestaande rollen

`CONNECTION LIMIT 20` verdwijnt uit `postgres.py:499` en wordt een parameter van `create_user`. De connector kent geen standaard meer: de waarde komt van de service.

In `database_manager.py` geeft `create_or_update_database_user` de berekende waarde mee, en de bestaande `exists`-tak krijgt er een `ALTER ROLE ... CONNECTION LIMIT` bij, naast de wachtwoord- en privilege-update. Dat geldt voor **beide** rollen: de read-write-rol en de `_ro`-rol op regel 385 krijgen dezelfde waarde.

### 4. Schemafragmenten opnieuw genereren

`postgresql-database.v1.0.json` en `postgresql-database.deployment.v1.0.json` zijn drift-locked en worden door tests bewaakt. Beide moeten opnieuw gegenereerd en meegecommit.

Het veld is optioneel met een standaard, dus er is **geen datamigratie** nodig en de schema-versie hoeft niet omhoog. Een bestaand projectbestand valideert ongewijzigd.

### 5. De wizard

Editables en visualizers voor beide lagen, plus een alinea in `help.md` die uitlegt wat de limiet betekent, dat meerdere databases van één deployment hem delen, en dat een hogere waarde ten koste gaat van de gedeelde ruimte.

Voor de deploymentlaag is `opi/services/catalog/cross_domain_access/editables.py` de referentie: die gebruikt `config_path(ConfigLayer.DEPLOYMENT, ServiceType.X, "config", *segments)` om het yaml-pad te vormen. Neem dat patroon over in plaats van een pad met de hand te schrijven, anders lopen de editable en het schemafragment uit elkaar.

Dit is expliciet gewenst: een gebruiker mag dit vandaag zelf aanpassen, dus het hoort zichtbaar in de wizard en niet verstopt in YAML.

## De toets

- een projectbestand zonder `connection-limit` levert een rol met `rolconnlimit = 20`, precies als vandaag;
- `connection-limit: 30` op het project levert 30 voor élke deployment van dat project;
- een deployment die er 80 van maakt krijgt 80, terwijl de andere deployments van hetzelfde project op 30 blijven;
- **een bestaande rol volgt een gewijzigde waarde**: zet de config om, herverwerk het project, en `SELECT rolconnlimit FROM pg_roles WHERE rolname = '<project>_<deployment>'` geeft het nieuwe getal. Dit is de assertie die vandaag zou hebben gefaald en de reden dat stap 3 bestaat;
- `connection-limit: 0` en `connection-limit: 101` worden door pydantic geweigerd met een leesbare melding, niet stil afgekapt;
- de `_ro`-rol van diezelfde deployment krijgt hetzelfde getal als de read-write-rol, ook bij een wijziging achteraf;
- `grep -rn "20" opi/connectors/postgres.py` levert geen connectielimiet meer op: de standaard staat in het servicepakket;
- de drift-tests op de twee schemafragmenten zijn groen;
- het veld verschijnt in de wizard, en een waarde overleeft een modal-rondgang: `tests/test_modal_noop_roundtrip.py` is de detector voor die klasse fouten;
- `grep -n "CONNECTION LIMIT 20" opi/` levert niets meer op.

## Waar op te letten

**Wat een gebruiker schrijfbaar krijgt, en wat dat tegenhoudt.** De schrijfweg zelf is niet nieuw: `ConfigLayer.DEPLOYMENT` bestaat, wordt door tien services gebruikt en heeft in `cross_domain_access` al gebruikersgerichte editables. Nieuw is uitsluitend dat een gebruiker voor déze service een waarde op die laag gaat zetten, en dat die waarde rechtstreeks een `ALTER ROLE` op de gedeelde productiedatabase stuurt. Wat dat tegenhoudt is op dit moment **alleen de speelruimte die de service declareert**: 1 tot 100. Er is geen rechtencheck, geen approval-hook en geen quotum per project. Dat is een bewuste keuze voor nu, want een gebruiker mag dit vandaag zelf bepalen, maar het moet opgeschreven staan en niet per ongeluk zo blijven. De rechtencheck is een aparte, opvolgende taak.

**Het plafond in het schema is geen quotum, en het telt dubbel.** Omdat de `_ro`-rol meeloopt, kost een deployment die 100 vraagt er in het slechtste geval 200. Maal 135 login-rollen is dat een veelvoud van wat de server aankan. Het schema voorkomt alleen dat één project in zijn eentje de boel omtrekt. Wat er echt tegen overinschrijving beschermt is `max_connections` op de server, en dat is een grove rem die pas afgaat als het al misgaat. Een quotum per project hoort bij dezelfde opvolgende taak als de rechtencheck.

**De globale limiet is een andere taak, met downtime.** `max_connections` is een postmaster-parameter en kan niet warm herladen. `rig-db` draait `instances: 1`, dus verhogen betekent een herstart en daarmee downtime voor Keycloak, Forgejo, de mailrelay en élke projectdatabase tegelijk. Bovendien past het niet zomaar in het geheugen: de pod gebruikt 744 MiB bij 73 verbindingen tegen een limiet van 2 GiB, en lineair doorgetrokken kom je bij 500 verbindingen rond 3,6 GiB uit. De geheugenlimiet moet dus mee omhoog in hetzelfde pakket. En de Cluster-CR valt onder de ArgoCD-app `production-infrastructure`.

**Reconciliëren mag geen wachtwoordwissel uitlokken.** De `exists`-tak zet vandaag ook een nieuw wachtwoord. Als het aanpassen van een limiet daar ongemerkt in meelift, roteert een configwijziging de credentials van een draaiende deployment. Controleer of dat pad al zo werkt en of dat gewenst is; het is geen onderdeel van deze taak om het te veranderen, wel om het niet erger te maken.

**De handmatige 60 op `mpfm_w3h_pr_250` is tijdelijk.** Zodra dit uitgerold is hoort die waarde in het projectbestand van `mpfm-w3h` te staan, anders verdwijnt hij bij de eerstvolgende heraanmaak van die gebruiker en staat het MOZa-team weer stil.

## Wat hierna nodig is

Een rechtencheck en een quotum per project, zodat niet elke gebruiker zichzelf 100 verbindingen kan geven. En los daarvan de verhoging van `max_connections` met de bijbehorende geheugenlimiet, als één gepland pakket via git en ArgoCD.
