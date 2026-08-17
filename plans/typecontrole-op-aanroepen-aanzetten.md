# Typecontrole op aanroepen aanzetten

Status: gebouwd, 6 augustus 2026 (RC-40). Beide vlaggen staan aan en pyright is schoon. Aanleiding: pyright draait hier met de controle op aanroepargumenten uit, dus getypeerde signatures geven documentatie en editorhulp maar geen enkele afdwinging.

## Wat er uit staat

`pyproject.toml` zet **negentien** pyright-regels op `false`. Twee daarvan raken de kern:

```toml
reportCallIssue = false      # klopt deze aanroep met deze functie
reportArgumentType = false   # past dit argument bij dit parameter-type
```

Dat kwam aan het licht toen een proef met `@overload` nul fouten gaf. Met standaardinstellingen geeft diezelfde proef er twee. De typehints in deze codebase worden dus geschreven en gelezen, maar niet gecontroleerd.

## Wat er gebeurt als ze aangaan

Gemeten op 6 augustus, met beide vlaggen tijdelijk aan (371 bestanden nagekeken):

```
182 fouten, verdeeld over 38 bestanden

  argumenttype           102
  geen overload           70
  ontbrekend argument      6
  te veel argumenten       4
```

Eén bestand draagt 42%: `api/router.py` heeft er 76. Daarna `forms/visualizers/flows.py` en `manager/project_manager.py` met 13, en dan snel aflopend.

## Niet alles is een fout, en dat is de kern van dit plan

Drie steekproeven op de tien "harde" gevallen leverden drie verschillende soorten op. Blind repareren zou dus schade doen.

**Vals alarm door een ongetypeerde afhankelijkheid.** Vier meldingen op `connectors/prometheus.py:59` beweren dat `PrometheusConnect` geen parameter `url` kent. De echte signatuur is `(url, headers, disable_ssl, retry, ...)`, dus de aanroep klopt gewoon. Het pakket is ongetypeerd (`# type: ignore[import-untyped]`) en pyright raadt ernaast. Hier is de oplossing een stub of een gerichte onderdrukking, niet de code.

**Een echt typegat waar de code werkt.** `forms/editables/processor.py:211` roept `validator.validate(value, context=...)` aan terwijl het protocol `validate(self, value)` declareert. Het werkt door een `try/except TypeError` eromheen: sommige validators nemen context, andere niet. Pyright heeft gelijk, en de oplossing is het protocol verbreden zodat het beschrijft wat er echt gebeurt, niet de aanroep aanpassen.

**Vermoedelijk echte fouten.** De overige gevallen van "ontbrekend" of "te veel argument" zijn geen typekwestie maar een aanroep die niet klopt. Dat zijn er hooguit een handvol, en die verdienen als eerste aandacht.

## Voorstel

1. **Triage vóór reparatie.** Deel de 182 in die drie bakken. Dat is de eerste en belangrijkste stap; zonder die indeling wordt dit een zoektocht waarbij echte fouten ondersneeuwen in ruis.
2. **De harde gevallen eerst**, want daar zit de kans op een echte bug. Per stuk beoordelen, niet per patroon.
3. **Ongetypeerde afhankelijkheden apart afhandelen.** Een stub of een gerichte `# type: ignore` met de reden erbij; geen algemene uitzondering die ook onze eigen fouten wegmoffelt.
4. **Typegaten dichten door het type te verbreden**, niet door de aanroep te versmallen. Het `validate`-protocol is daar het voorbeeld van: de code doet al iets dat het type niet beschrijft.
5. **Dan pas de vlaggen aan**, per stuk: eerst `reportCallIssue`, daarna `reportArgumentType`, zodat een terugval te herleiden is.
6. **De overige zeventien uitgezette regels inventariseren.** Niet aanzetten, wel opschrijven waarom ze uit staan, want nu weet niemand of dat een besluit was of een restant.

## Volgorde

1. Triage, met de uitkomst per bak vastgelegd in dit plan.
2. De harde gevallen, met per stuk een oordeel: echte fout, vals alarm, of te verbreden type.
3. `api/router.py`, want daar zit 42% en het is waarschijnlijk één of twee patronen die zich herhalen.
4. De rest in groepen per bestand.
5. De vlaggen aan, één voor één, met de suite groen na elke stap.

## Waar op te letten

**Dit is geen opruiming maar een zoektocht naar bugs.** Tien meldingen gaan over aanroepen die niet kloppen met hun functie. Die draaien vandaag gewoon mee. De opbrengst is niet een schoner scherm maar die tien.

**Onderdruk niet wat je niet begrijpt.** De verleiding bij 182 meldingen is een `# type: ignore` waar het rood is. Dat maakt het scherm schoon en de code slechter. Elke onderdrukking krijgt een reden erbij, en "pyright snapt dit pakket niet" is een geldige reden terwijl "geen tijd" dat niet is.

**Het aantal is een momentopname.** Er komt dagelijks code bij. Meet opnieuw voor je begint, en zet de vlaggen aan zodra een bak leeg is, anders loopt het terug tijdens het werk.

**De prijs van uit laten staan groeit.** Elke nieuwe typehint die niet gecontroleerd wordt, is een belofte die niemand nakomt. Dat is precies waarom het haaksysteem (`plans/een-haaksysteem-op-events.md`) op getypeerde payloads leunt: die keuze is alleen wat waard als de controle aanstaat.

---

# Uitvoering (RC-40, 6 augustus 2026)

Opnieuw gemeten voor aanvang: **182 fouten over 38 bestanden**, gelijk aan de meting in
het plan. Beide vlaggen staan nu aan en `uv run pyright` geeft 0 fouten.

## Triage, per bak

| bak | aantal | uitkomst |
|---|---|---|
| harde gevallen (ontbrekend/te veel argument, onbekende parameter) | 10 | 6 echte fouten, 4 vals alarm (alle vier prometheus) |
| `reportCallIssue` in `api/router.py` | 70 | twee herhaalde patronen, allebei echt |
| `reportArgumentType` | 102 | 6 echt, de rest een narrowing die de code al garandeerde of een type dat te smal was |

## De harde gevallen, per stuk

1. **`core/git_monitor.py:162`** -- riep `has_deployments_for_current_cluster(content)`
   aan. Die methode neemt geen argumenten en is een coroutine, dus **elk projectbestand
   met deployments gaf hier een TypeError**, vóór de namespace-controle eronder. De vraag
   wordt nu beantwoord uit de inhoud die de handler al heeft; een kale `ProjectManager`
   kon hem sowieso niet beantwoorden.
2. **`manager/bootstrap_manager.py:144`** -- gaf `generate_public_url` vier positionele
   argumenten; die functie neemt er al lang drie. **Geen enkele bootstrap-actie kon
   draaien.** De hostnaam wordt nu afgeleid zoals de deployment-omgevingsvariabelen dat
   doen (base-domain, anders subdomain, anders deploymentnaam).
3. **+ 4. `manager/delete_project_manager.py:1561,2196`** -- gaven
   `MarkedForDeletionService` een pool mee; die neemt er geen. De TypeError werd door een
   `except Exception` opgeslokt en als waarschuwing gelogd, waarna de operatie zichzelf
   "skipped" noemde: **manifesten werden nooit voor uitgesteld opruimen gemarkeerd.**
5. **`web/router.py:2111`** -- de domain-settings-route vulde `domain_format` niet,
   terwijl het veld op het responsemodel bestaat en de deployment het draagt. De modal
   kreeg altijd `null` en kon het huidige formaat niet voorselecteren.
6. **`forms/editables/processor.py:211`** -- het typegat uit het plan. Opgelost door het
   type te verbreden (`ContextAwareEditableValidator`) en de aanroepvorm van de signatuur
   van de validator te lezen, in plaats van hem te ontdekken door `TypeError` te vangen.
   Dat ving namelijk óók een `TypeError` uít een context-bewuste validator, waarna die
   stilletjes zonder zijn context werd overgedaan.
7-10. **`connectors/prometheus.py:59`** -- vals alarm, zoals verwacht. Niet onderdrukt:
   de pakketwortel deelt zijn klassen uit via een `__getattr__`-shim, die een typechecker
   als de unie van álles resolveert. De import wijst nu naar de submodule; zelfde object.

## `api/router.py`

Twee patronen:

- **64x `Field(..., example=X)`**. `example` is geen parameter van `Field`. Pydantic v2
  veegde het in `json_schema_extra` mét deprecation-waarschuwing en v3 accepteert het
  niet meer. Nu `examples=[X]` -- de echte parameter, en wat OpenAPI 3.1 verwacht.
- **6x `error_status_codes.get(result.get("error_type"), 400)`** met een sleutel die None
  kon zijn.

## Nog twee echte fouten uit de argumenttype-bak

- **`jobs/reconciliation.py:286`** -- riep `get_minio_host(None)` aan.
  `get_cluster_config` weigert een onbekend cluster, dus **de hele MinIO-opruimtak liep
  vast voordat hij iets opruimde**, opgeslokt door zijn eigen `except Exception` en
  gelogd als "kon de connector niet initialiseren". Nu het cluster dat deze instantie
  beheert.
- **`forms/extractor.py:96`** -- een veld met `FormMeta` zonder label kreeg
  `label=None`. Nu valt het terug op de afgeleide naam.

## Waar het type verbreed is in plaats van de aanroep versmald

- `RevisionManager.record_clone/record_restore/record_initial`: `generation: int | None`.
  De schrijver onderaan had altijd al "never write None as generation - 0 is the
  default"; alleen de annotatie zei iets anders.
- `FormRenderer.render_fields_from_editables`: `layout` mag None zijn, want
  `FormSection.layout` is optioneel; dan worden alle velden op volgorde gerenderd.
- `FormField.schema_type` / `infer_widget_type`: `type | None`, want pydantics
  `FieldInfo.annotation` is dat ook en de body kon er altijd al tegen.
- `ChiselConnector.get_local_endpoint`: een `TunnelEndpoint`-TypedDict in plaats van
  `dict[str, str | int]`, zodat `endpoint["port"]` weer een int is.
- `AnyTaskProgressManager`: de twee voortgangsmanagers zijn losse klassen met dezelfde
  synchrone interface (de persistente noemt zichzelf een drop-in replacement), dus een
  signatuur die er één noemt wijst de helft van zijn echte aanroepers af.
- `_validate_csrf(form_data)`: een `Mapping`, want de handlers geven starlettes
  `FormData` mee en dat is geen dict.

Onderdrukt is er niets. Geen enkele `# type: ignore` toegevoegd.

## De overige zeventien regels: inventaris

Gemeten op 6 augustus door elke regel afzonderlijk aan te zetten. **Niet aangezet** --
dit is de inventaris die stap 6 van het plan vroeg, zodat de volgende die dit oppakt weet
wat het kost.

| regel | fouten | oordeel |
|---|---|---|
| `reportPossiblyUnboundVariable` | 0 | **gratis aan te zetten** |
| `reportOptionalIterable` | 0 | **gratis aan te zetten** |
| `reportUnboundVariable` | 0 | **gratis aan te zetten** |
| `reportIndexIssue` | 0 | **gratis aan te zetten** |
| `reportOperatorIssue` | 0 | **gratis aan te zetten** |
| `reportAssignmentType` | 1 | een middag |
| `reportOptionalOperand` | 1 | een middag |
| `reportOptionalSubscript` | 3 | een middag |
| `reportGeneralTypeIssues` | 3 | een middag |
| `reportReturnType` | 7 | een middag |
| `reportUndefinedVariable` | 8 | **eerst kijken**: een niet-bestaande naam is meestal een echte fout (ruff `F821` staat hier ook uit) |
| `reportMissingTypeStubs` | 13 | over ongetypeerde afhankelijkheden, niet over onze code |
| `reportOptionalMemberAccess` | 36 | eigen PR waard, dezelfde soort werk als deze |
| `reportUnnecessaryIsInstance` | 58 | ruis, geen fouten: overbodige isinstance-checks |
| `reportAttributeAccessIssue` | 59 | eigen PR waard |
| `reportUnusedVariable` | 62 | opruimwerk (ruff `F841` staat hier ook uit) |
| `reportUnknownArgumentType` | 1481 | dit is "gradual typing uit"; niet realistisch |
| `reportUnknownVariableType` | ~2600 | idem |
| `reportUnknownMemberType` | 2601 | idem |

Vijf regels staan op nul en kosten dus niets. De volgende stap met de beste verhouding is
die vijf, daarna `reportUndefinedVariable` (8) -- want een naam die niet bestaat is geen
stijlkwestie. De drie `Unknown`-regels horen bij elkaar en betekenen "elke waarde moet een
bekend type hebben"; dat is een andere discussie dan deze en niet een die je per ongeluk
begint.

## Tests

- `tests/test_typed_call_sites.py` -- 16 tests op de harde gevallen, plus twee
  AST-poorten (geen `MarkedForDeletionService(...)` met argumenten, geen `example=`).
- `tests/test_typed_argument_sites.py` -- 12 tests op de echte gevallen uit de
  argumenttype-bak.
- Volledige suite: 5932 passed, 7 skipped.
