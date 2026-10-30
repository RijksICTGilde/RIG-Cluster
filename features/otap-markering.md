# OTAP-markering op een deployment

**Status**: Implemented
**Created**: 2026-10-18

## Samenvatting

Elke deployment draagt een markering die zegt in welke OTAP-fase ze zit: `ontwikkeling`,
`test`, `acceptatie` of `productie`. Een deployment zonder markering is `ontwikkeling`.

Het veld heet `environment`, in de lijn van de andere Engelse veldnamen in het
projectbestand. De waarden zijn de Nederlandse OTAP-termen.

Het cluster kan deze rol niet overnemen: vrijwel alle deployments draaien op
`odcn-production`, dus het cluster zegt niets over de fase.

## Waarom bij de deployment, en niet bij het project

De omgeving is een eigenschap van een draaiende instantie. Een project HEEFT omgevingen,
het IS er geen. Project en deployment beide markeren, met erfenis, geeft combinaties die
niets betekenen: een test-deployment in een productieproject is logisch, een
productie-deployment in een testproject niet. Daarom staat de markering op precies één
plek, en weigert het schema een `environment` op projectniveau.

Een project toont hooguit de HOOGSTE markering van zijn deployments
(`highest_environment`), afgeleid en niet opgeslagen. Een eigen lifecycle voor het project
(experiment, pilot, productie) is een andere as en bestaat niet.

Besloten door de opdrachtgever op 21 oktober 2026, nadat de eerste versie het veld op
beide niveaus had.

## De waarden en de standaard

| Waarde | Betekenis |
|---|---|
| `ontwikkeling` | de standaard, voor elke deployment zonder markering |
| `test` | |
| `acceptatie` | |
| `productie` | |

De standaard wordt bij het LEZEN toegepast en niet in elk bestand weggeschreven, zodat
bestaande projectbestanden geen diff krijgen.

Er is bewust **geen** automatische invulling: geen migratie leidt een markering af uit een
naam of iets anders. Projectbeheerders zetten de markering zelf.

```yaml
schema-version: "2.11"
name: demo
deployments:
  - name: pr-123
    cluster: odcn-production
    namespace: demo
                               # geen markering: dit is 'ontwikkeling'
  - name: live
    cluster: odcn-production
    namespace: demo
    environment: productie
```

Dit project toont op zijn projectpagina `Productie`: de hoogste markering van zijn
deployments.

## Waar het vandaan komt in de code

Alles staat in `opi/core/project_environment.py`: de enum `Omgeving`, de labels voor het
scherm, `effective_environment(deployment)` (de markering met de standaard toegepast),
`highest_environment(project_data)` en `is_production_downgrade`. Elke lezer gaat
daarlangs.

De waarden staan ook in `project_v2.json`, als één definitie (`$defs/environment`) waar
het deploymentveld naar verwijst. `tests/test_project_environment.py` houdt die lijst en
de enum gelijk, en toetst dat een markering op projectniveau wordt geweigerd.

## Wie het zet, en waar

| Weg | Wat |
|---|---|
| Portaal, Deployment bewerken / Deployment toevoegen | de markering van die deployment |
| `POST /api/v2/projects/{project}/:upsert-deployment` | het veld `environment` op die deployment; weglaten laat de markering staan zoals ze is |
| Portaal, Terugzetten naar een nieuwe deployment | de markering van de deployment die daar wordt aangemaakt |

De rollen die het mogen zetten zijn de rollen die de projectinstellingen al mogen
wijzigen (`PROJECT_EDIT_ROLES`: admin en owner). Er is geen apart recht bijgekomen.

Met een uitzondering: de restore-flow (`modal-restore`) staat open voor een operationele
rol (`PROJECT_OPERATE_ROLES`: admin, owner en `developer`; de leesrol `member` niet), en
daar hoort het veld bij de deployment die die flow aanmaakt. Wie die rol heeft zet dus de
markering van ZIJN nieuwe deployment, en raakt geen bestaande: de sectie draagt
alleen het pad van het nieuwe slot (`deployments[len]/environment`), en een lichaam dat een
ander pad meestuurt verandert daar niets. Dat is dezelfde reikwijdte als de naam, de images
en het webadres die diezelfde flow al zette. `productie` zetten is vrij (zie hieronder), dus
dit geeft geen weg om een markering te verlagen.

In het keuzeveld heeft "Ontwikkeling" de lege waarde en staat het er een keer: zo hoeft
de standaard niet in elk bestand te staan. Een bestand dat `ontwikkeling` expliciet
draagt, valt in de lijst op die keuze terug.

Het veld is niet platform-managed: het staat niet in `PLATFORM_MANAGED` en een
projectbeheerder mag het wijzigen.

## Tonen

- Het paneel van een deployment zet de markering naast het cluster.
- De v2-API draagt op een deployment twee velden: `environment` (de markering zoals ze in
  het bestand staat, of `null`) en `effective_environment` (wat er geldt, alleen-lezen).

De projectpagina toont de hoogste markering niet meer naast de titel: als los woord zei
die tag niet wat hij betekende, en de markering staat al op elk deployment-paneel.

Het label komt uit `environment_label`, geregistreerd als Jinja-global, zodat het paneel
geen eigen regel over de standaard verzint.

## Productie verlaten

`is_production_downgrade(before, after)` zegt of een overgang een markering `productie`
verlaat. OPI logt zo'n verlaging per deployment op WARNING met wie, project en deployment.
Een markering weghalen is ook een verlaging: de deployment komt dan op `ontwikkeling` uit.

Een deployment NAAR of VAN `productie` zetten vraagt `ops` met een verse step-up
(`environment.set`, ontwerp paragraaf 7): wie hem van productie haalt omzeilt elke
productieregel met een klik, en wie hem erop zet bepaalt waar die regels gelden. Een
nieuwe deployment die meteen op productie komt telt mee. Een wissel tussen ontwikkeling,
test en acceptatie is een deployment schrijven en vraagt verder niets.
`production_marking_change(before, after)` is de vergelijking die dat leest. De keuzelijst
in het portaal (`EnvironmentOptionsProvider`) zet de stages die je niet mag kiezen uit,
met "(alleen ops)" erachter.

De melding staat op de wegen die een BESTAANDE markering kunnen wijzigen: de twee
portaalwegen (wizard-edit en modal-edit) en de upsert-deployment van de API. Ze noemen
allemaal wie het doet. Op de API-weg komt die identiteit uit de taakpayload (`actor`,
dezelfde bron als `created_by` op de taak zelf): het e-mailadres uit de sessie, anders
"API". Elke weg logt NA de opslag, zodat een geweigerde wijziging niets meldt.
Welke handelingen een step-up vragen staat in
[otp-op-de-zad-gebruiker.md](otp-op-de-zad-gebruiker.md), en welke rol een handeling
opent in [rollen-en-rechten.md](rollen-en-rechten.md).

## Schemaversie

Schemaversie `2.11`, met de legacy-patch `opi/schemas/project_legacy/v2.9.json` voor de
vorige versie. De migratiestap `introduce_environment` verhoogt alleen het versienummer en
vult niets in. De stamp komt als string in het bestand (`"2.11"`), zoals elke versie sinds
RC-363; zie `features/project-schema-versions.md`.

## Buiten scope

- Een promotieservice die deployments door het OTAP-pad loodst; die kan het eigendom van
  het veld later overnemen.
- Een eigen lifecycle op het project (experiment, pilot, productie).
- Ander gedrag dat van de markering afhangt, zoals resource-defaults of sleep-mode per
  omgeving.
