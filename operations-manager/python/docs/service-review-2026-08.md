# Service review — augustus 2026

Controle achteraf van alle services in `opi/services/catalog/` tegen
`instructions/service-review-checklist.md`. De service-opzet is op 1 augustus 2026
afgemaakt; die checklist is pas daarná geschreven. Dit is de controle achteraf: per
service een checklisttabel (secties 1-12), de bevindingen eronder, en — waar het
aantoonbaar en veilig kon — de reparatie zelf (met een test die eerst faalde op de oude code).

## Werkwijze

Per service is de waarheid uit de registry gelezen, niet uit bestandsnamen
(`SERVICES[ServiceType.X].config_model`, `.config_schema_version`,
`.config_model_for(layer)`), precies omdat `persistent-storage` en `temp-storage` hun model
delen via `catalog/shared/storage.py` en `minio`/`postgresql-database` `CloneState` uit
`catalog/shared/revisions.py` mixen. Daarna is de checklist per sectie afgelopen en is elke
lezer/schrijver van de config (managers, forms, generation) nagelopen.

Elke cel in de tabellen is `PASS`, `FAIL` of `N.v.t.` met reden. Elke `FAIL` heeft eronder een
bevinding: het bestand en de regel, wat er mis is, de gevolgen, en of het gerepareerd is of
waarom niet. Reparaties vallen strikt binnen sectie 3 van het plan ("veilig te repareren");
al het andere is vastgelegd als bevinding met een aanbeveling.

## Reikwijdte van de verificatie

Checklistsectie 9 ("verify against real project files") vraagt om de audit over de 47
productie-projectbestanden in de externe repo
`~/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github/projects`. **Die
repo is in deze omgeving niet aanwezig.** De audit is daarom gedraaid over de wél beschikbare
echte vormen: `projects/simple-example.yaml` (repo-root) en de fixtures onder
`operations-manager/python/tests/fixtures/` en `tests/golden/`. Waar sectie 9 op een service
van toepassing is, staat dit als beperking (`PARTIAL` / `N.v.t. (omgeving)`) genoteerd; de
conclusie "elke config-blok wordt door zijn model geclaimd" is dus getoetst tegen de
beschikbare data, niet tegen de volledige productieset.

## Testbaseline

Vóór enige wijziging: `uv run pytest tests/ -q` gaf **4850 passed, 6 skipped, 32 errors**. De 32
errors zijn uitsluitend de `@pytest.mark.kind`/`integration`-tests in
`tests/integration/test_kubectl_write_ops.py` (en `test_kubectl_logs_real.py`), die in hun
fixture een echte Kind-cluster proberen op te zetten (`tests/integration/conftest.py:210`); dat
kan zonder Docker/Kind in deze omgeving niet. Ze zijn omgevingsgebonden en niet door deze branch
veroorzaakt.

Ná de reparaties (inclusief de 12 nieuwe tests in `tests/test_service_review_2026_08.py`):
**4862 passed, 6 skipped, 32 errors** — precies de baseline (4850) plus de 12 nieuwe tests,
dezelfde 32 Kind-only errors, geen enkele nieuwe failure of error.

Twee dingen die het plan vooraf vastlegt en die tijdens de sweep bevestigd zijn:

- **`config_schema_version` en `config_model` reizen samen.** De default is `None`
  (`base.py:273`); de koppeling wordt bewaakt door
  `tests/test_service_config_schema.py::TestConfigModelAndVersionArePaired`. Een versie zonder
  model (of andersom) is dus een testfout, geen losse bevinding.
- **Geen enkele service overschrijft `migrate_config`; elk model staat op 1.0.** De
  per-service migratieweg is aanwezig en bedraad, maar nog nooit gebruikt. Dat is bij elke
  service als `N.v.t.` in sectie 6 genoteerd, niet als gat.

Terzijde, een gemeten correctie op een projectnotitie: de omgeving draait **Python 3.14.6**,
waar `except ValueError, TypeError:` (PEP 758, `RangeValidator` in
`opi/forms/editables/validators.py`) geldige syntax is en beide excepties vangt — geverifieerd
op runtime. Dat is dus geen bug. De MEMORY-notitie die naar Python 3.13 verwijst is verouderd.

---

## Samenvatting

### Wat is gerepareerd (12 reparaties, elk met een falende-eerst test in `tests/test_service_review_2026_08.py`)

Checklist 4 (guardrails op editables):
1. `metrics-scraper` poort — `RangeValidator(1, 65535)` toegevoegd (poort was in editable én
   model onbegrensd).
2. `health-check` poort — `RangeValidator(1, 65535)` toegevoegd op de editable (het model
   begrenst al; nu ook veldniveau, gelijk aan `metrics-scraper`).
3. `attachments` env-name — nieuwe `EnvNameValidator` (spiegelt de model-regex
   `AttachmentUse._valid_env_name`), zodat een ongeldige naam op het veld verschijnt in plaats
   van pas als hele-config-fout bij opslaan.
4. `authorization-wall` banner — `EmptyToNoneConverter` + `remove_when_none`, zodat een leeg
   veld geen `banner: ""`/`null` wegschrijft. Veilig want `banner` default `None`, geen bool.

Checklist 10 (logging):
5. `redis_manager._get_redis_service_config` — debug-regel logt de config-**keys** i.p.v. het
   hele config-dict.
6. `minio_manager._get_minio_service_config` — idem.
7. `database_manager._get_database_service_config` en `_get_database_cluster_config` — dumpten
   het hele (cluster)config-dict op DEBUG; nu identificerende waarden (instances, storage,
   image, aantallen; namespace/endpoint/storage-class).
8. `approvals.apply_approval_verdicts` — logde niets bij een vastgelegd oordeel; nu één
   INFO-regel per oordeel met subject, nieuwe status en approver (een toestandswijziging hoort
   één regel te loggen).

Checklist 5 (identiteit via `service_entry_name`):
9. `project_file_handler.get_deployment_service_generation` — matchte alleen op de
   `reference`-vorm; nu format-agnostisch via `service_entry_name`, zodat een `{name}`- of
   bare-string-entry op deployment-niveau ook gevonden wordt.

Checklist 3 (accepted-field hint):
10. `redis` en `minio-storage` — `config_api_fields` overschreven (net als de siblings), zodat
    een validatiefout de geaccepteerde configvelden noemt. Alleen foutmelding-verrijking, geen
    gedrags-, schema- of schijfwijziging.

Checklist 8 (verouderde doc/comment):
11. `database_manager._get_existing_database_credentials_from_k8s` — docstring klopte niet meer
    (zei "None otherwise" terwijl de fouttak `raise`t) en er stond een dode `# return None`;
    beide gecorrigeerd. Het `except`-gedrag zelf (re-raise) is ongewijzigd gelaten.

### Belangrijkste bevindingen (vastgelegd, niet gerepareerd — buiten "veilig te repareren")

- **`authorization-wall` cookie-secret is niet replay-safe** (`__init__.py:103,120-125`): een
  vers `secrets.token_urlsafe(32)` bij elke render, onvoorwaardelijk overschreven. Elke
  reprocess wisselt het cookie-secret → git-churn en sessie-invalidatie voor alle gebruikers.
  Aanbeveling: bestaand secret lezen-en-hergebruiken, alleen genereren als het ontbreekt.
  Gedragswijziging in de manifest/secret-stroom → bevinding.
- **`sleep-mode` disable-while-sleeping** (`project_manager.py:5162`): de replica-berekening
  leest de `sleep`-toestand onvoorwaardelijk, niet gepoort op `sleep_config.load(...)`. Een
  deployment die slaapt terwijl sleep-mode daarna wordt uitgezet blijft op `replicas: 0` en is
  niet meer te wekken. Combineert met het niet-opruimen van het residuele `sleep`-blok bij
  service-verwijdering. Reconcile/replica-stroom → bevinding.
- **`minio` provisioning slikt fouten** (`minio_manager.py:294-298, 353-357, 403-407,
  419-424`): meerdere faal-takken loggen en `return`en, zodat `provision()` als succes eindigt
  terwijl er geen secret is opgeslagen; de fout duikt pas later op als "no object storage
  credentials found". Aanbeveling: laten falen (raise). Publieke provisioning-stroom → bevinding.
- **`minio` `config_model_for` niet overschreven**: project (`enable-versioning`) en deployment
  (clone state) dragen echt verschillende inhoud, maar één permissief unie-model valideert
  beide, zodat een misgeplaatst veld valideert en stil genegeerd wordt. Schema-vormwijziging /
  nieuw terrein → bevinding.
- **`keycloak` `_get_keycloak_service_config`** (`keycloak_manager.py:362-617`): valideert met
  rauwe `dict.get()` in plaats van `provider.validate_config()`, met afwijkende garanties t.o.v.
  het opslaan-chokepoint. Publieke provisioning-stroom → bevinding.
- **`namespace-redis` config-asymmetrie**: `redis_manager` leest `acl-key-prefix` óók van een
  `namespace-redis`-entry, maar die service heeft `config_model=None`, dus validatie slaat het
  over. Ontwerp/product-keuze, gekoppeld aan de nog-niet-geïmplementeerde stub → bevinding.
- **Diverse per-run INFO-regels op idempotente no-ops** (redis-, minio-, keycloak-,
  database-manager): loggen bij elke reconcile ook als er niets veranderde. Deels
  platform-breed patroon (`is_disabled`), deels oordeelskwestie welke regels waardevolle trace
  zijn. Vastgelegd per service; niet mechanisch weggehaald.
- **Model-begrenzingen vs schema-fragment** (`metrics-scraper` poort zonder `ge/le`;
  `namespace-postgres` `instances` UI-cap 5 vs model `ge=1` zonder bovengrens; `StorageEntry.size`
  niet als k8s-quantity gevalideerd): het model is het echte chokepoint, maar het begrenzen
  ervan regenereert het gecommitte fragment en kan bestaande bestanden afwijzen → nieuw terrein,
  bevinding.
- **`config_path(...)` i.p.v. hardgecodeerde yaml-paden** (`keycloak/editables.py`,
  `persistent_storage`/`temp_storage/editables.py`): op de veilig-te-repareren-lijst, maar een
  zuivere refactor met identieke uitvoer heeft geen falende-eerst test; conform de
  test-discipline van het plan vastgelegd als aanbevolen follow-up, niet uitgevoerd.

### Documentatie (checklist 12) — systemische bevinding

Er ontbreekt een `features/<service>.md` voor: `namespace-redis`, `platform`, `attachments`,
`minio-storage`, `persistent-storage`, `temp-storage`, `postgresql-database`,
`metrics-scraper`, `health-check`. Wel aanwezig: `keycloak-*`, `authorization-wall`,
`sleep-mode`, `namespace-postgresql-database`, `redis-*`, `publish-on-web-*`. Het schrijven van
negen feature-docs valt buiten deze controle-PR (het plan scoped dit als controle, niet als
doc-ronde) en is als één follow-up-taak vastgelegd.

---

## Services

De volgorde volgt de aanbeveling uit het plan (laatst-gehard eerst), gevolgd door de rest.
`shared/` staat als laatste subsectie omdat het geen service is maar wel door twee services
gedragen wordt.

### redis

Registry: `config_model=RedisConfig`, `config_schema_version="1.0"`, config-laag = PROJECT
(`acl-key-prefix`), `config_model_for` override = nee, `cleanup_manager_key="redis"`,
`manifest_secret_class=RedisSecret`, `manifest_activated_by=(REDIS, NAMESPACE_REDIS)`.

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + `ServiceDefinition` (services.py:568) + registry.py:51; naam niet hardgecodeerd in `project_v2.json`. |
| 2 | Configmodel & fragment | PASS | model+versie beide gezet; `extra=forbid, populate_by_name=True`; alias `acl-key-prefix` met streepje; fragment matcht. |
| 3 | Configlagen | PASS (gerepareerd) | config wordt op PROJECT gevalideerd; `config_api_fields` ontbrak → **gerepareerd** (fix 10). |
| 4 | Editables | N.v.t. | `acl-key-prefix` heeft bewust geen UI (zie bevinding). |
| 5 | Lezen/schrijven via service | PASS | `_get_redis_service_config` via `service_entry_name`/`service_entry_config`. |
| 6 | Migratie | N.v.t. | model op 1.0. |
| 7 | Tests | PASS | guardrail-suites groen. |
| 8 | UI | PASS | niet `hidden`; `acl-key-prefix` is YAML-only (advanced). |
| 9 | Echte projectbestanden | N.v.t. (omgeving) | prod-repo afwezig. |
| 10 | Logging | PASS (gerepareerd) | state-change-regels goed; config-dump op DEBUG **gerepareerd** (fix 5). No-op INFO-regels: bevinding. |
| 11 | Beveiliging & hygiëne | PASS | replay-safe; cleanup verwijdert de ACL-user. |
| 12 | Documentatie | PASS | `features/redis-acl-persistence.md` + `redis-cloning-consideration.md`. |

Bevindingen:
- [FINDING] `acl-key-prefix` heeft geen `config_form_section`/`config_editables` — alleen via
  YAML te zetten. Plausibel bewust (Grist-achtige escape hatch); UI-plaatsing is een
  productkeuze. Niet gerepareerd.
- [FINDING] `redis_manager.py:130,219` — no-op-bevestigingsregels vuren op INFO bij elke
  reconcile. Aanbeveling: naar DEBUG. Managerlog-oordeel; niet gerepareerd.

### publish-on-web

Registry: `config_model=PublishOnWebConfig`, `config_schema_version="1.0"`, lagen = COMPONENT +
DEPLOYMENT_COMPONENT (`tls`/`attachment`) + PROJECT (`domains`-approvalblok), `config_model_for`
override = nee, `cleanup_manager_key=None`, `manifest_secret_class=None`. Root-level service; het
domein-wizard / root-`domains:`-approval / admin-router / globale subdomain-registry zijn
platform-infra en bewust buiten het pakket.

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:495 + registry.py:41. `$defs/publish-on-web-config` + `$defs/domains` blijven bewust (git_monitor valideert rauw pre-migratie). |
| 2 | Configmodel & fragment | PASS | `extra=forbid, populate_by_name=True`; streepjes-aliassen; fragment matcht; if/then gespiegeld. |
| 3 | Configlagen | PASS | COMPONENT editables + PROJECT `domains` via `config_model_for(PROJECT)` + DEPLOYMENT_COMPONENT via de dict-keyed walk. |
| 4 | Editables | PASS | tls = SELECT (gesloten set); attachment = SELECT met `remove_when_none`; geen bool-valkuil. |
| 5 | Lezen/schrijven via service | PASS | component/root via `service_entry_name`/`service_entry_config`; deployment-component gebruikt de dict-vorm (correct op die laag). |
| 6 | Migratie | PASS | domains-relocatie is een echte versie-gate v2.5 met één plaatsingsautoriteit `ensure_domains_config` en read-both `get_domains_config`. |
| 7 | Tests | PASS | `test_publish_passthrough.py` e.a. |
| 8 | UI | PASS | component-fieldset via `config_component_layout()`; per-deployment override met "inherit". |
| 9 | Echte projectbestanden | PARTIAL | prod-repo afwezig. |
| 10 | Logging | PASS (gerepareerd) | approval-oordeel logde niets → **gerepareerd** (fix 8). Overige per-render regels: bevinding. |
| 11 | Beveiliging & hygiëne | PASS | `provision` no-op; approvals via `config_approvals`; schrijft via save-chokepoint. |
| 12 | Documentatie | PASS | `features/publish-on-web-tls-modes.md` e.a. |

Bevindingen:
- [FINDING] `config_api_fields` niet overschreven is hier **correct** — het model spant meerdere
  lagen, dus teruggeven zou `domains` in de component-foutmelding lekken. Bewust laten.
- [FINDING] `project_manager.py:5596` / `project_file_handler.py:979` — per-render INFO/DEBUG-regels
  die niets-veranderde loggen. Laag; managerlog-oordeel. Niet gerepareerd.

### attachments

Registry: `config_model=AttachmentsConfig` (`RootModel[list[AttachmentUse]]`),
`config_schema_version="1.0"`, lagen = COMPONENT (couplings) + DEPLOYMENT_COMPONENT (override).
Het PROJECT-blok houdt de catalogus onder `data` (niet `config`), dus de project-walk slaat het
bewust over. YAML-opslag, geen DB. `cleanup_manager_key=None`.

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:599 + registry.py:54. `$defs/attachment-*` blijven bewust. |
| 2 | Configmodel & fragment | PASS | `extra=forbid, populate_by_name=True`; streepjes-aliassen; field- + model-validators; fragment matcht. |
| 3 | Configlagen | PASS | COMPONENT + DEPLOYMENT_COMPONENT gevalideerd; deployment-component-editables leven bewust in `forms/editables/fields/deployments`. |
| 4 | Editables | PASS (gerepareerd) | reference/provide-as = required SELECT; path met `PathValidator`; **env-name had geen validator → gerepareerd** (fix 3). |
| 5 | Lezen/schrijven via service | PASS | alle lezers via `service_entry_name`/`service_entry_config`; legacy `use`→`config` read-side geaccepteerd. |
| 6 | Migratie | N.v.t. | model op 1.0. |
| 7 | Tests | PASS | `test_attachment_schema.py` e.a. |
| 8 | UI | PASS | COMPONENT-sequence + PROJECT "Bijlagen"-upload-sectie. |
| 9 | Echte projectbestanden | N.v.t. (omgeving) | prod-repo afwezig. |
| 10 | Logging | PASS | upload/stage/remove loggen met de attachment-id; geen dumps. |
| 11 | Beveiliging & hygiëne | PASS | geen server-side resources; deletion weigert bij referentie; couplings gevalideerd bij opslaan. |
| 12 | Documentatie | FAIL | geen `features/attachments.md` (alleen futures-notities). Bevinding. |

### minio-storage

Registry: `config_model=MinioStorageConfig` (mixt `CloneState`), `config_schema_version="1.0"`,
lagen = PROJECT (`enable-versioning`) + DEPLOYMENT (clone state), `config_model_for` override =
nee, `cleanup_manager_key="minio"`, `manifest_secret_class=MinIOSecret`.

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:557 + registry.py:50. |
| 2 | Configmodel & fragment | PASS | model+versie gezet; `extra=forbid`/`populate_by_name` uit `CloneState`; alias met streepje; fragment matcht. |
| 3 | Configlagen | PASS (gerepareerd) | PROJECT+DEPLOYMENT gevalideerd; `config_api_fields` ontbrak → **gerepareerd** (fix 10). |
| 4 | Editables | N.v.t. | `enable-versioning` heeft geen UI (zie bevinding). |
| 5 | Lezen/schrijven via service | PASS (gerepareerd) | manager-lezers via `service_entry_name`; deployment-generation-lezer matchte alleen op `reference` → **gerepareerd** (fix 9). Setter: bevinding. |
| 6 | Migratie | N.v.t. | model op 1.0. |
| 7 | Tests | PASS | guardrail-suites groen. |
| 8 | UI | PASS | niet `hidden`; `enable-versioning` is YAML-only (zie bevinding). |
| 9 | Echte projectbestanden | N.v.t. (omgeving) | prod-repo afwezig. |
| 10 | Logging | PASS (gerepareerd) | config-dump op DEBUG **gerepareerd** (fix 6). No-op INFO + fout-slikken: bevindingen. |
| 11 | Beveiliging & hygiëne | PASS | replay-safe; cleanup verwijdert bucket/user/policy. |
| 12 | Documentatie | FAIL | geen `features/minio-storage.md`. Bevinding. |

Bevindingen:
- [FINDING] `enable-versioning` heeft geen `config_form_section`/`config_editables` — alleen
  YAML, hoewel het "de enige echte gebruikersinstelling" is. UI-plaatsing = productkeuze.
- [FINDING] `config_model_for` niet overschreven terwijl project- en deployment-laag echt
  verschillende inhoud dragen (zie samenvatting). Nieuw terrein; niet gerepareerd.
- [FINDING] `minio_manager.py` faal-takken slikken fouten en returnen (zie samenvatting).
- [FINDING] `set_deployment_service_generation` (`project_file_handler.py:2078`) matcht nog op
  `reference` en kan bij een niet-reference-entry een duplicaat toevoegen (waarna
  `_validate_services_listed_once` het project afkeurt). Aanbeveling: find-or-create met
  in-place promotie via `service_entry_name`. Schrijfpad → bevinding.

### persistent-storage

Registry: `config_model=StorageConfig` (uit `shared/storage.py`), `config_schema_version="1.0"`,
lagen = COMPONENT (mount specs) + DEPLOYMENT_COMPONENT (per-mount clone state
`StorageCloneState`), `config_model_for` override = **ja** (de enige service met een override),
`cleanup_manager_key="pvc"`, `manifest_secret_class=None`.

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:514 + registry.py:46. |
| 2 | Configmodel & fragment | PASS | `StorageEntry` `extra=forbid, populate_by_name=True`; alias `mount-path`; fragment matcht; override aanwezig. |
| 3 | Configlagen | PASS | beide lagen gelopen door `validate_service_configs`. Yaml-paden zijn literals i.p.v. `config_path` (bevinding). |
| 4 | Editables | PASS met kanttekening | `size` is een SELECT met provider; geen aparte validator (de gesloten set komt via de provider). Model-begrenzing van `size`: bevinding. |
| 5 | Lezen/schrijven via service | PASS | `extract_storage_from_component_services` via `service_entry_name`/`service_entry_config`. |
| 6 | Migratie | N.v.t. | model op 1.0; legacy naamloze storage wordt door de project-migratie afgehandeld. |
| 7 | Tests | PASS | guardrail-suites groen. |
| 8 | UI | PASS | component-scope; component-form Sequence. |
| 9 | Echte projectbestanden | PARTIAL | prod-repo afwezig; clone-state-vorm matcht `StorageCloneState`. |
| 10 | Logging | PASS | state-change-regels dragen identificerende waarden. |
| 11 | Beveiliging & hygiëne | PASS | replay-safe; `cleanup_manager_key="pvc"` verwijdert/hernoemt PVC's. |
| 12 | Documentatie | FAIL | geen `features/persistent-storage.md`. Bevinding. |

Bevindingen:
- [FINDING] `StorageEntry.size: str` wordt niet als k8s-quantity gevalideerd; `size: "banana"`
  passeert `validate_service_configs` en faalt pas bij PVC-apply. Echte fix zit in het model
  (fragment-regen) → nieuw terrein, niet gerepareerd.
- [FINDING] `editables.py` yaml-paden zijn literals waar `config_path(...)` hoort (zie
  samenvatting; zuivere refactor, niet uitgevoerd).

### temp-storage

Registry: identiek aan `persistent-storage` (deelt `StorageConfig` en de `config_model_for`
override) behalve `cleanup_manager_key=None` — ephemeral storage maakt geen PVC/server-side
resource, dus `None` is correct.

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:525 + registry.py:47. |
| 2 | Configmodel & fragment | PASS | zelfde `StorageConfig`; fragment matcht (byte-identiek aan persistent). |
| 3 | Configlagen | PASS | beide lagen gelopen. Yaml-paden literals (bevinding, zie persistent). |
| 4 | Editables | PASS met kanttekening | zelfde `size`-SELECT als persistent. |
| 5 | Lezen/schrijven via service | PASS | zelfde gedeelde extractor. |
| 6 | Migratie | N.v.t. | model op 1.0. |
| 7 | Tests | PASS | guardrail-suites groen. |
| 8 | UI | PASS | component-scope; niet hidden. |
| 9 | Echte projectbestanden | PARTIAL | zoals persistent. |
| 10 | Logging | PASS | temp-storage maakt geen PVC, dus geen per-render PVC-regel. |
| 11 | Beveiliging & hygiëne | PASS | geen server-side resource → `cleanup_manager_key=None` correct. |
| 12 | Documentatie | FAIL | geen `features/temp-storage.md`. Bevinding. |

### keycloak

Registry: `config_model=KeycloakConfig`, `config_schema_version="1.0"`, config-laag = PROJECT,
`config_model_for` override = nee, `cleanup_manager_key="keycloak"`,
`manifest_secret_class=KeycloakSecret`. Polymorf (intern template vs extern) met
advanced pass-through (`extra="allow"`, bewust).

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:503 + registry.py:42; `"keycloak"` in project_manager is de secret-type-key, geen projectbestand-contract. |
| 2 | Configmodel & fragment | PASS | `RestrictAccessConfig` `extra=forbid`; hoofdmodel `extra=allow` (polymorf, gedocumenteerd); fragment matcht. |
| 3 | Configlagen | PASS | config op PROJECT; editables/api_fields daar; yaml-paden literals (bevinding). |
| 4 | Editables | FAIL | template-default-mismatch en dode realm-roles-editables (bevindingen); validators aanwezig waar nodig. |
| 5 | Lezen/schrijven via service | FAIL | `_get_keycloak_service_config` valideert met rauwe `dict.get()` i.p.v. `validate_config()` (bevinding). Identiteit wél via `service_entry_*`. |
| 6 | Migratie | N.v.t. | model op 1.0. |
| 7 | Tests | PASS | guardrail + golden aanwezig. |
| 8 | UI | PASS | niet hidden; detail-sectie achter admin/owner-rol, onthult admin-wachtwoord (bewuste autorisatiekeuze). |
| 9 | Echte projectbestanden | PARTIAL | prod-repo afwezig; `realistic-project.yaml` gebruikt legacy bare `- keycloak` (afgehandeld). |
| 10 | Logging | PASS met kanttekening | goede state-change-regels; per-apply 5 INFO-detailregels + per-run "Found N" (bevinding). |
| 11 | Beveiliging & hygiëne | PASS | cleanup verwijdert realm/clients; replay-safe (bestaande creds hergebruikt). |
| 12 | Documentatie | PASS | `features/keycloak-*.md`. |

Bevindingen:
- [FINDING] editable-default `template="sso-support"` vs model/manager-default `"sso-only"` —
  twee effectieve defaults. Welke de bedoelde is, is een productkeuze. Niet gerepareerd.
- [FINDING] `realm-roles`-editables/visualizers zijn gedefinieerd/geëxporteerd maar in geen
  formsectie bedraad (dood in de UI). Verwijderen of bedraden is een gedragskeuze. Niet
  gerepareerd.
- [FINDING] `_get_keycloak_service_config` (keycloak_manager.py:362) valideert rauw i.p.v. via
  `validate_config`, met afwijkende garanties t.o.v. het opslaan-chokepoint. Publieke stroom →
  bevinding.
- [FINDING] editables.py yaml-paden literals waar `config_path` hoort (zuivere refactor). Niet
  gerepareerd. `KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG_EDITABLE` heeft een editable-`default` gelijk
  aan de model-default; `remove_when_none` toevoegen zónder de editable-default te verwijderen
  bevriest de default nog steeds — de volledige fix (default weghalen) is een UI-wijziging →
  bevinding, niet gerepareerd.
- [FINDING] `keycloak_manager.py:1076-1080` — 5 INFO-regels per access-restriction-apply. Naar
  DEBUG/één regel. Managerlog-oordeel; niet gerepareerd.

### authorization-wall

Registry: `config_model=AuthorizationWallConfig`, `config_schema_version="1.0"`, config-laag =
PROJECT (`banner`), `config_model_for` override = nee, `cleanup_manager_key=None`,
`manifest_secret_class=None`. Levert de oauth2-proxy-sidecar, `service_port` 8080→4180 en een
cookie-secret via `contribute_manifest_context`.

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:607 (`requires` publish-on-web+keycloak+restrict-access) + registry.py:43. |
| 2 | Configmodel & fragment | PASS | `extra=forbid`; één optioneel `banner`; fragment matcht. |
| 3 | Configlagen | PASS | PROJECT; editable + formsectie beide via `config_path(...)`. |
| 4 | Editables | PASS (gerepareerd) | `banner` had geen `remove_when_none`/converter → **gerepareerd** (fix 4). |
| 5 | Lezen/schrijven via service | PASS | banner via `service_entry_config`, identiteit via `service_entry_name`. |
| 6 | Migratie | N.v.t. | model op 1.0. |
| 7 | Tests | PASS | golden aanwezig. |
| 8 | UI | PASS | niet hidden; het ene veld toont in de projectstap. |
| 9 | Echte projectbestanden | PARTIAL | prod-repo afwezig; golden aanwezig. |
| 10 | Logging | PASS | skip-tak logt WARNING met componentnaam; geen dumps. |
| 11 | Beveiliging & hygiëne | FAIL | cookie-secret niet replay-safe (bevinding). `cleanup_manager_key=None` correct. |
| 12 | Documentatie | PASS | `features/authorization-wall.md`. |

Bevinding:
- [FINDING] cookie-secret wordt bij elke render opnieuw gegenereerd en onvoorwaardelijk
  overschreven (zie samenvatting). Publieke manifest/secret-stroom → bevinding, niet
  gerepareerd; impact (sessie-invalidatie bij elke reconcile) te bevestigen tegen een echte
  dubbele reconcile.

### namespace-postgresql-database

Registry: `config_model=NamespacePostgresConfig`, `config_schema_version="1.0"`, config-laag =
PROJECT, `config_model_for` override = nee, `cleanup_manager_key="database"`,
`manifest_secret_class=None` (de gedeelde `postgresql-database` levert het DB-secret).

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:545 (`hidden=True`) + registry.py:49. |
| 2 | Configmodel & fragment | PASS | `extra=forbid`; geen verplichte velden (alles default); fragment matcht (privilege-enum, `additionalProperties:false`). `postInitSQL` camelCase matcht de echte YAML-key. |
| 3 | Configlagen | PASS | PROJECT; editables/api_fields daar; paden via `config_path(...)`. |
| 4 | Editables | PASS (kanttekening) | `instances` `RangeValidator(1,5)`; `storage` SELECT; `postInitSQL`/`privileges` geen editable (API/YAML-only, dus geen lege-`[]`). |
| 5 | Lezen/schrijven via service | PASS | `_get_database_service_config` via `Project.service_config` + `provider.validate_config`. |
| 6 | Migratie | N.v.t. | model op 1.0; `_uppercase_privileges` is een normalizer. |
| 7 | Tests | PASS | pairing/fragment-drift gelocked. |
| 8 | UI | FINDING | `hidden=True` → de configsectie rendert niet in de wizard (de sleep-mode-valkuil). Bevinding. |
| 9 | Echte projectbestanden | N.v.t. (omgeving) | prod-repo afwezig. |
| 10 | Logging | PASS (gerepareerd) | config-dumps op DEBUG **gerepareerd** (fix 7). No-op INFO + WARNING-dumps 780/856: bevinding. |
| 11 | Beveiliging & hygiëne | PASS | `cleanup_manager_key` gezet; provisioning replay-safe. |
| 12 | Documentatie | PASS | `features/namespace-postgresql-database.md`. |

Bevindingen:
- [FINDING] `hidden=True` maakt de opgebouwde configsectie wizard-onzichtbaar (alleen zichtbaar
  in modal-edit van een project dat het al draagt). Bedoeld YAML/API-opt-in? Productkeuze. De
  docstring "Owns its project-level config UI" is niet strikt onwaar (de service bezit de
  sectie), dus niet aangepast.
- [FINDING] `instances` UI-cap 5 vs model `ge=1` zonder bovengrens: `instances: 10` passeert het
  model maar niet de UI. Model begrenzen = fragment-regen/productkeuze. Niet gerepareerd.
- [FINDING] `database_manager.py` — meerdere no-op INFO-regels + twee whole-dict WARNINGs (780,
  856). Managerlog-oordeel; niet gerepareerd.

### postgresql-database (gedeeld)

Registry: `config_model=PostgresqlDatabaseConfig` (= `CloneState`), `config_schema_version="1.0"`,
config-laag = DEPLOYMENT (clone state), `config_model_for` override = nee,
`cleanup_manager_key="database"`, `manifest_secret_class=DatabaseSecret`,
`manifest_activated_by=(POSTGRESQL_DATABASE, NAMESPACE_POSTGRESQL_DATABASE)`.

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:534 (niet hidden) + registry.py:48. |
| 2 | Configmodel & fragment | PASS | `CloneState` `extra=forbid, populate_by_name=True`; beide velden optioneel (33 echte revisies); fragment matcht. |
| 3 | Configlagen | PASS (kanttekening) | config op DEPLOYMENT (clone state, machine-geschreven); bewust geen editables/api_fields — een gebruiker schrijft het nooit. |
| 4 | Editables | N.v.t. | OPI-beheerde clone state. |
| 5 | Lezen/schrijven via service | PASS (gerepareerd) | clone state via `revision_manager` (`service_entry_*`); de gedeelde generation-lezer **gerepareerd** (fix 9). Remote-source read (562): bevinding. |
| 6 | Migratie | N.v.t. | model op 1.0. |
| 7 | Tests | PASS | guardrail-suites groen. |
| 8 | UI | PASS | clone state toont niets in de projectstap (correct). |
| 9 | Echte projectbestanden | N.v.t. (omgeving) | prod-repo afwezig. |
| 10 | Logging | PASS (gerepareerd) | deelt database_manager (fix 7); `build_secret_files` logt WARNING bij ontbrekende creds. |
| 11 | Beveiliging & hygiëne | PASS | provision replay-safe; cleanup gezet. |
| 12 | Documentatie | FAIL | geen `features/postgresql-database.md`. Bevinding. |

Bevinding:
- [FINDING] `database_manager.py:562` — `remote_source.get("services", {}).get("postgresql-database", {})`
  is een single-key lookup die `service_entry_name` omzeilt en bij een list-vorm zou breken.
  Maar dit blok is een remote-source *connection descriptor* (andere vorm dan clone state), dus
  geen drop-in `service_entry_name`-swap. Aanbeveling: vorm bevestigen/normaliseren. Niet
  gerepareerd.

### metrics-scraper

Registry: `config_model=MetricsScraperConfig`, `config_schema_version="1.0"`, config-laag =
COMPONENT, `config_model_for` override = nee, `cleanup_manager_key=None`,
`manifest_secret_class=MetricsAuthSecret`. On-disk vorm is de legacy inline single-key dict
`{metrics-scraper: {port, path}}` (geen `config:`-wrapper).

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:621 + registry.py:44. |
| 2 | Configmodel & fragment | PASS (kanttekening) | model+versie gezet; `extra=forbid`; `populate_by_name` ontbreekt (geen aliassen, onschadelijk); `port` onbegrensd in model (bevinding); fragment matcht. |
| 3 | Configlagen | PASS | COMPONENT; api_fields/editables daar. |
| 4 | Editables | PASS (gerepareerd) | `path` `PathValidator`; **`port` had geen range-validator → gerepareerd** (fix 1). |
| 5 | Lezen/schrijven via service | PASS | `contribute_manifest_context` via `service_entry_name`/`service_entry_config`. |
| 6 | Migratie | N.v.t. | model op 1.0. |
| 7 | Tests | PASS | `test_metrics_scraper.py` + golden. |
| 8 | UI | PASS (zachte bevinding) | component-scope; niets in de projectstap. De picker-beschrijving zegt niet expliciet dat config per-component wordt ingevoerd (checklist 8). |
| 9 | Echte projectbestanden | N.v.t. (omgeving) | prod-repo afwezig; golden gebruikt de inline vorm. |
| 10 | Logging | PASS | `build_secret_files` logt WARNING met deployment-naam bij ontbrekend token. |
| 11 | Beveiliging & hygiëne | PASS | render replay-safe; `cleanup_manager_key=None` correct. |
| 12 | Documentatie | FAIL | geen `features/metrics-scraper.md`. Bevinding. |

Bevindingen:
- [FINDING] `config_model.py` `port: int | None = None` zonder `ge/le` (anders dan health-check).
  De editable begrenst nu de UI, maar een API/YAML-directe write accepteert nog elke int.
  Model begrenzen = fragment-regen + kan bestaande out-of-range bestanden afwijzen. Niet
  gerepareerd.
- [FINDING] editables schrijven de legacy inline vorm i.p.v. het uniforme `{reference, config}`.
  Lezers tolereren beide; vorm-migratie = publieke stroom + schijfvorm. Niet gerepareerd.

### health-check

Registry: `config_model=HealthCheckConfig`, `config_schema_version="1.0"`, config-laag =
COMPONENT, `config_model_for` override = nee, `cleanup_manager_key=None`,
`manifest_secret_class=None`. On-disk `config:`-wrapper.

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:645 + registry.py:45. |
| 2 | Configmodel & fragment | PASS | `extra=forbid, populate_by_name=True, regex_engine=python-re`; streepjes-aliassen; poort `ge=1/le=65535`; paden `PATH_PATTERN`; fragment matcht (`\Z`). |
| 3 | Configlagen | PASS | COMPONENT; api_fields/editables daar. |
| 4 | Editables | PASS (verbeterd) | `scheme` SELECT (gesloten set) met lege "Standaard"-optie; alle velden optioneel met `remove_when_none`, lege waarden gedropt (`processor.py:372`); geen bool-valkuil. Poort-editable heeft nu ook `RangeValidator` (fix 2, pariteit met metrics). |
| 5 | Lezen/schrijven via service | PASS | `contribute_manifest_context` via `service_entry_*`; geen-inbound-poort-guard. |
| 6 | Migratie | N.v.t. | model op 1.0. |
| 7 | Tests | PASS | `test_service_health_check.py` + e2e. |
| 8 | UI | PASS (zachte bevinding) | zelfde component-copy-punt als metrics-scraper. |
| 9 | Echte projectbestanden | N.v.t. (omgeving) | prod-repo afwezig; template-consumptie geverifieerd tegen `deployment.yaml.jinja`. |
| 10 | Logging | PASS | pure idempotente template-var-override; geen logging is hier correct. |
| 11 | Beveiliging & hygiëne | PASS | render replay-safe; padwaarden pattern-guarded vóór ongequote interpolatie. |
| 12 | Documentatie | FAIL | geen `features/health-check.md`. Bevinding. |

Bevinding (beide component-services):
- [FINDING] noch `metrics-scraper` noch `health-check` vertelt in de picker-beschrijving expliciet
  dat het aanvinken op de projectstap geen config toont (config is per-component). Checklist 8;
  copy/productkeuze. Niet gerepareerd.

### sleep-mode

Registry: `config_model=SleepModeConfig`, `config_schema_version="1.0"`, config-laag = PROJECT,
`config_model_for` override = nee, `cleanup_manager_key=None`, `manifest_secret_class=None`
(de waker-Deployment/ConfigMap/Secret worden bewust bespoke geëmit; de generieke
envFrom-hook past niet op een aparte Deployment).

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:629 + registry.py:55; `_CLUSTER_DEFAULTS` is per-cluster, geen naam-contract. |
| 2 | Configmodel & fragment | PASS | `extra=forbid, populate_by_name=True`; streepjes-aliassen; fragment matcht. |
| 3 | Configlagen | PASS | PROJECT; paden via `config_path(...)` (`_path`-helper). |
| 4 | Editables | PASS | de historische valkuil is correct vermeden: `remove_when_none` staat op de optionele velden (match, waker-component, title, description) en **niet** op de booleans `enabled`/`waker` (beide default `True`). Gesloten sets zijn SELECTs met echte providers. |
| 5 | Lezen/schrijven via service | PASS | `config._project_config` via `service_entry_name`/`service_entry_config`; validatie via `SleepModeConfig.model_validate`. |
| 6 | Migratie | N.v.t. | model op 1.0. |
| 7 | Tests | PASS | guardrail-suites groen; dedicated tests aanwezig. |
| 8 | UI | PASS | **geen** `hidden=True` (de valkuil die het ooit onzichtbaar maakte); sectie rendert; wake/sleep-knop via `actions_provider`. |
| 9 | Echte projectbestanden | N.v.t. (omgeving) | prod-repo afwezig. |
| 10 | Logging | PASS (kanttekening) | sleep/wake loggen elk één INFO-regel; no-ops loggen niets; skips WARNING; geen secret gelogd. Twee per-render regels: bevinding. |
| 11 | Beveiliging & hygiëne | PASS | manifest-builders puur/deterministisch; token per-deployment, AGE-versleuteld, constant-time compare; `cleanup_manager_key=None` correct (waker = declaratieve manifests, gepruned door de sweep). |
| 12 | Documentatie | PASS | `features/sleep-mode.md`. |

Bevindingen:
- [FINDING] `project_manager.py:5162` disable-while-sleeping = niet-wekbaar (zie samenvatting).
  Reconcile/replica-stroom; niet gerepareerd.
- [FINDING] `handle_service_removal` niet overschreven → residueel `sleep`-blok blijft staan bij
  service-verwijdering; combineert met het punt hierboven. Publieke stroom; niet gerepareerd.
- [FINDING] `project_manager.py:5168` en `:1339` loggen op INFO bij elke render terwijl een
  deployment slaapt. `:5168` spiegelt het bestaande `is_disabled`-patroon twee regels erboven,
  dus platform-breed; niet gerepareerd (managerlog-oordeel).
- [FINDING/minor] de `match`-editable heeft geen veldniveau-validator; glob-validatie zit alleen
  in `SleepModeConfig._validate_match` (model). `match` is vrije tekst (geen gesloten set), dus
  geen schone safe-fix. Observatie.

### namespace-redis

Registry: `config_model=None`, `config_schema_version=None`, geen config-laag,
`cleanup_manager_key="redis"`, `manifest_secret_class=None`, `hidden=True` (nog-niet-
geïmplementeerde stub die terugvalt op gedeelde Redis).

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:578 + registry.py:52; niet hardgecodeerd in `project_v2.json`. |
| 2 | Configmodel & fragment | PASS (N.v.t.-body) | model/versie beide afwezig, correct gepaard (`TestConfigModelAndVersionArePaired` + `test_provider_without_config_model_raises`). |
| 3 | Configlagen | PASS (kanttekening) | geen laag; `_validate_one_config` slaat het over (model None). Zie bevinding. |
| 4 | Editables | N.v.t. | geen config. |
| 5 | Lezen/schrijven via service | PASS | `redis_manager` via `service_entry_name`/`service_entry_config`. |
| 6 | Migratie | N.v.t. | geen config. |
| 7 | Tests | PASS | de "service zonder configmodel"-voorbeeldtests gebruiken nu namespace-redis. |
| 8 | UI | PASS (bewust) | `hidden=True` bedoeld (interne variant). |
| 9 | Echte projectbestanden | N.v.t. (omgeving) | hidden stub; prod-repo afwezig. |
| 10 | Logging | PASS | fallback logt WARNING met project/deployment. |
| 11 | Beveiliging & hygiëne | PASS | idempotente gedeelde-redis-ACL-weg; `cleanup_manager_key="redis"` verwijdert de ACL-user. |
| 12 | Documentatie | FAIL | geen `features/namespace-redis.md`. Bevinding. |

"Geen config" is **correct voor de huidige hidden stub**. Bevinding:
- [FINDING] `redis_manager` leest `acl-key-prefix` óók van een `namespace-redis`-entry, maar die
  heeft `config_model=None`, dus validatie slaat het over (typo wordt stil genegeerd). Laag
  risico vandaag (hidden, valt terug op shared redis). Of namespace-redis `RedisConfig` moet
  delen of uit de gedeelde lezer moet, is een ontwerp/product-keuze gekoppeld aan de echte
  implementatie. Niet gerepareerd.

### platform

Registry: `config_model=None`, `config_schema_version=None`, geen config-laag,
`cleanup_manager_key=None`, `manifest_secret_class=None`, `hidden=True`, altijd-aan.

| # | Sectie | Uitkomst | Reden |
|---|--------|----------|-------|
| 1 | Identiteit & registratie | PASS | enum + services.py:589 + registry.py:53; niet hardgecodeerd in `project_v2.json`. |
| 2 | Configmodel & fragment | PASS (N.v.t.-body) | model/versie beide afwezig, correct gepaard. |
| 3 | Configlagen | PASS | geen laag; correct. |
| 4 | Editables | N.v.t. | geen config. |
| 5 | Lezen/schrijven via service | PASS (N.v.t.) | leest geen per-project config. |
| 6 | Migratie | N.v.t. | geen config. |
| 7 | Tests | PASS | `test_non_secret_file_services_declare_none` groen. |
| 8 | UI | PASS (bewust) | `hidden=True`; impliciet/altijd-aan. |
| 9 | Echte projectbestanden | N.v.t. (omgeving) | verschijnt nooit als gekozen entry; per-component geïnjecteerd bij generatie. |
| 10 | Logging | PASS | secret-creatie logt één INFO per component; alias-resolutie logt counts. |
| 11 | Beveiliging & hygiëne | PASS | secret afgeleid uit namen (deterministisch/replay-safe); geen server-side resource → `cleanup_manager_key=None` correct. |
| 12 | Documentatie | FAIL | geen `features/platform.md`. Bevinding. |

"Geen config" is **onomstotelijk correct**: `PlatformSecret` wordt uitsluitend uit
`deployment_name` + `component_name` gebouwd (`project_manager.py:6020-6023`), `PlatformVariables`
= `DEPLOYMENT_NAME`/`COMPONENT_NAME`. Geen manager/generator/form leest per-project
platform-config. Bevinding: alleen de ontbrekende feature-doc.

### shared/ (subsectie — geen service)

`shared/storage.py` (`StorageEntry`/`StorageConfig`/`StorageCloneState`) en `shared/revisions.py`
(`RevisionAction`/`Revision`/`CloneState`). Beide modules: `extra=forbid, populate_by_name=True`;
alias `mount-path`; docstrings accuraat (documenteren de `config_model_for`-uitzondering en de
`$defs/deployment-service-config`-/JSONPath-beperkingen). De `config_model_for`-override van de
storage-services is **correct** en beide lagen worden werkelijk gelopen: COMPONENT via de
list-aware `validate_config`, DEPLOYMENT_COMPONENT via `model.model_validate` in de `else`-tak;
de geschreven clone-state (`revision_manager.py:269-281`) matcht `StorageCloneState`/`Revision`/
`RevisionAction`.

Bevinding:
- [FINDING/laag] `RevisionAction.source: str` is niet-nullable, terwijl de (dode)
  `revision_manager.record_initial` `source=None` doorgeeft. Niet actief (geen call sites; alle
  live writers geven een niet-None source, waardoor het model schoon valideerde tegen de 33 echte
  revisies). Aanbeveling: `source: str | None = None` typen of de dode `record_initial`
  verwijderen. Raakt het gedeelde clone-state-contract (minio/postgresql-database), dus geen
  safe-fix binnen de storage-scope. Niet gerepareerd.

---

## Naloop: API-configureerbaarheid (checklist sectie 13)

**Aanleiding.** De sweep hierboven toetste per service of `config_api_fields(layer)`
gedeclareerd was, maar niet of een service ook *end-to-end via de API* te configureren
was. Meting wees uit: dat kon niet. De REST-API (v1 én v2) liet services alleen **op
naam** toevoegen/koppelen — elk `services`-veld was `list[str]`, geen enkel requestmodel
had een per-service `config`-veld. Alle échte config (keycloak template,
namespace-postgres storage/instances, storage-mounts, health-check probes,
metrics-scraper, auth-wall banner, sleep-mode, redis, minio, publish-on-web tls) was
uitsluitend via de wizard/forms-laag te schrijven. `config_api_fields` bestond wél, maar
werd op één plek gebruikt (`project_validation.py:40`) om een foutmelding-hint te bouwen —
niet om config via de API binnen te laten.

**Opgelost (nieuw terrein, geen safe-fix maar een bewuste bouw op verzoek).** Er is een
uniform, registry-gedreven service-config-oppervlak bijgebouwd: `GET /api/v2/services`
(catalogus + ondersteunde targets), een read `GET …/services/{service}/config`, en per
configureerbare (service, target) een **eigen getypeerde** route
`PUT/DELETE …/services/<service>/config/<target>[/<naam>]`. De PUT-body **is** het
configmodel van die service, dus de OpenAPI-spec documenteert de velden + enum-waardes
expliciet per service (een client is eruit te genereren); FastAPI valideert de body al
synchroon (422 op een onbekende/out-of-enum waarde). De routes worden bij startup uit de
registry gegenereerd, dus niets hardcodet een servicenaam. Zie
`features/service-config-api.md`. De schrijfweg gaat door hetzelfde validatie-chokepoint
(`save_and_commit_project` → `validate_service_configs`) als backstop, zodat een door het
model afgewezen config de task laat falen mét de accepted-fields hint. Geen schemaversie
opgehoogd, geen globaal schema geraakt, geen projectbestand aangeraakt — de
`{name, config}` / `{reference, config}`-records zijn daar al geldig.

**Per-service dekking (gemeten via de registry, niet aangenomen):**

| Service | Config-target(s) via API | Opmerking |
|---|---|---|
| authorization-wall | project | |
| keycloak | project | |
| namespace-postgresql-database | project | |
| redis | project | api-fields-only (geen editables) |
| sleep-mode | project | |
| minio-storage | project, deployment | api-fields-only |
| attachments | component | sequence-config; via `config_editables` |
| health-check | component | |
| metrics-scraper | component | |
| persistent-storage | component | `RootModel[list]`; geen platte api-fields, correct |
| temp-storage | component | idem |
| publish-on-web | component | alleen tls/attachment; domeinen blijven platform-infra |
| namespace-redis | — | draagt bewust geen config; correct |
| platform | — | draagt bewust geen config; correct |
| postgresql-database | — | **FINDING** |

- [FINDING/postgresql-database] Heeft een `config_model` (schema 1.0) maar declareert het
  op géén enkele laag (`config_api_fields`/`config_editables`/`config_component_layout`
  alle leeg). Daardoor is het via geen enkele target configureerbaar — noch UI, noch API.
  Dit is dezelfde soort gat als de RC-12-reparaties voor redis/minio, maar hier hangt de
  keuze *welke* laag (en of het veld überhaupt user-facing hoort te zijn) aan een
  productbeslissing over de gedeelde-database-service. Niet gegokt; vastgelegd als
  bevinding. Aanbeveling: bepaal of `postgresql-database` per-project config hoort te
  dragen; zo ja, declareer de laag zoals de siblings, zo nee, verwijder het ongebruikte
  configmodel.

**Grensbesluit (bewust niet gedaan):** de image-update-endpoint draagt per-mount
storage-*acties* (clone/recreate) via `ServiceReference`. Dat is een opdracht, geen
config, en heeft geen equivalent op het config-endpoint; ongemoeid gelaten. De
component-`services: list[str]`-velden blijven (pure selectie, bare names, kunnen geen
config dragen); hun beschrijving verwijst nu naar het config-endpoint. De
add-service-endpoints (v1 + v2) zijn `deprecated` gemarkeerd omdat de uniforme PUT
selectie + config in één doet.

**Poort-validatie aangescherpt (gedragswijziging, met akkoord).** Het exposen van
config via de getypeerde API bracht aan het licht dat de poort-range te ruim was:
`health-check.port` en `metrics-scraper.port` stonden op `1..65535`, terwijl een
non-root container (images draaien niet als root) een privileged poort (<1024) nooit
kan binden of bereiken. Beide zijn naar `1024..65535` gebracht, model én editable
gespiegeld; `metrics-scraper` had de bound alleen op de editable, niet op het model,
dus de API accepteerde daar elke int — nu ook in het model. Fragmenten
geregenereerd.
- [FINDING/vervolg] De poort hoort idealiter een op de component *gedefinieerde*
  inbound-poort te zijn (cross-veld-validatie). Dat kent de service-config nu niet en
  is bewust uitgesteld; los op te pakken.

**Tests:** `tests/test_service_config_api.py` (pure kern, round-trip door het
validatie-chokepoint, endpoint-helpers, een gemeten dekkings-guard over álle services,
en de privileged-poort-afwijzing) en `tests/test_v2_flow.py::TestConfigureServiceFlow`
(het HTTP-oppervlak: catalogus, getypeerde upsert/clear-payloads, OpenAPI-per-service,
auth, en de 404/422-poorten). Alle falende-eerst geverifieerd.
