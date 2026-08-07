# Sandboxrun 7 augustus 2026 — alles groen met de echte projectbestanden

Aanleiding: op 7 augustus 2026 zijn twaalf taken gemerged (RC-38 t/m RC-49, RC-51, RC-52,
RC-53). Elke PR is afzonderlijk geverifieerd, maar het geheel is niet tegen een draaiend
cluster gehouden met echte projectbestanden. Deze run doet dat.

Uitgevoerd op commit `f3882e2e` (branch `alles-groen-op-de-sandbox-met-de-echte-projectbest`).

## Samenvatting

Wordt ingevuld aan het eind van de run.

## 1. Cluster en context

| | |
|---|---|
| kubectl-context | `kind-rig-sandbox` (bevestigd, enige context) |
| sandbox-URL | https://zad.sandbox.rijksapp.dev |
| sleutel | sandbox, niet productie |

## 2. Testsets

Alle commando's vanuit `operations-manager/python`, met `-p no:randomly` zodat de run
naspeelbaar is.

| set | commando | uitkomst |
|---|---|---|
| unit | `uv run pytest tests/ -q` | **6532 passed, 7 skipped**, 258 deselected (3:56) |
| e2e lokaal | `uv run pytest tests/e2e/ -m "e2e and not sandbox" --timeout=300` | **151 passed, 1 skipped**, 42 deselected (4:58) |
| e2e sandbox | `uv run pytest tests/e2e/ -m "e2e and sandbox"` | zie hieronder |

Plus `ruff check .` (All checks passed) en `ruff format --check .` (891 files already formatted).

Het plan noemt 6538 unittests; er staan er nu 6532 groen plus 7 overgeslagen. Het plan noemt
151 browsertests en dat klopt exact.

### Een meetfout van deze run, voor de volledigheid

De eerste lokale e2e-run gaf 2 rode in `test_wizard_cross_domain_policy` met
`FileNotFoundError: Template file not found: manifests/service-network-policy.yaml.jinja`.
Dat was geen regressie maar een fout in de meting: `settings.MANIFESTS_PATH` is een relatief
pad, en pytest draaide vanuit `/workspace` in plaats van vanuit `operations-manager/python`.
Vanuit de juiste map zijn beide groen. Wie deze suite draait, moet dat vanuit
`operations-manager/python` doen.

### Bijvangst: drie eerder rode lokale e2e's zijn nu groen

Op 5 augustus faalden op basiscommit-niveau
`test_edit_wizard::TestEditServices::test_select_service_advance_through_config_to_review` en
de twee tests in `test_wizard_services_regressions`. Die zijn in deze run alle drie groen.

## 3. De 47 productiebestanden door de schemapoort

Bron: `robbert/rig-cluster-projects-github`, `projects/` (47 `*.yaml`), commit `30cfba1b3`.
Dat is dezelfde repo die `tests/test_upgrade_safety_replay.py` als `DEFAULT_PROJECTS_REPO`
noemt, dus de meting draait op de bestanden die de replay bedoelt.

### 3a. De blessed replay over de echte bestanden

```
RIG_PROJECTS_DIR=<checkout>/projects uv run pytest tests/test_upgrade_safety_replay.py -v
  -> 9 passed, 1 deselected
```

`test_real_project_files_migrate_and_validate` draait alle 47 door de exacte keten die
productie vóór een schrijfactie draait: `migrate_to_latest`, dan `validate_project_schema`,
dan `validate_project_structure` (inclusief de per-service typed-config gate). Alle 47 komen
er schoon door. De baseline-lijst met bekende defecten in dat bestand is nog steeds leeg.

### 3b. De poortmeting, vergeleken met 6 augustus

Los van de replay is de poort zelf gemeten, met dezelfde drie tellingen als
`features/project-schema-versions.md`:

| | 6 aug 2026 | deze run (7 aug) |
|---|---|---|
| bestanden met root-`domains:` van vóór v2.5 | 30 | **30** |
| bestanden met `config.keycloak`-restant van vóór v2.3 | 21 | **21** |
| afgekeurd door de poort (rauw, vóór migratie) | 22 | **0** |
| afgekeurd ná migratie | 0 | **0** |

Gedeclareerde schemaversies over de 47: versie `2` 5x, versie `2.2` 42x. 41 van de 47
bestanden migreren (`was_migrated`), en herschrijven zichzelf dus bij hun eerstvolgende
verwerking — verwacht gedrag, geen bevinding.

**De 22 zijn 0 geworden, en dat is de bedoelde winst, geen regressie.** Op 6 augustus
valideerde `git_monitor` rauwe inhoud tegen het *nieuwste* schema; 22 bestanden vielen
daardoor stil buiten de verwerking. Sinds RC-32 valideert de poort tegen de versie die het
bestand zelf declareert (`validate_declared_project_schema`), en halen alle 47 het.

Ter controle is ook de *oude* poortvorm nagebootst — rauwe inhoud tegen het nieuwste schema
(2.6). Dat geeft nu **34** afkeuringen, meer dan de 22 van 6 augustus. Dat getal is geen
regressie maar de verwachte uitkomst van precies de verandering die RC-32 mogelijk maakte:
het nieuwste schema heeft sindsdien de oude vormen `domains:` (v2.5) en `config.keycloak`
(v2.3) laten vallen, dus meer oude bestanden botsen ermee. Dat pad wordt niet meer gelopen;
het is hier alleen gemeten om te laten zien waar het verschil vandaan komt.

Geen enkel bestand dat op 6 augustus verwerkt werd, wordt vandaag geweigerd. De schemapoort
van RC-44 verandert dit oordeel niet: die weigert vroeg in de *wizard*, en de 47 bestanden
komen niet via de wizard binnen.

## 4. De 47 bestanden op de sandbox zetten — niet uitgevoerd

De 47 bestanden kunnen niet een-op-een op de sandbox. Ze moeten eerst door
`operations-manager/python/scripts/migrate_project_to_sandbox.py`, dat het cluster omzet naar
`sandboxed-local`, de domeinen naar `sandbox.rijksapp.dev` zet, de repository-URL's naar de
sandbox-Forgejo wijst, een sandboxbeheerder toevoegt, en — de stap die niet over te slaan is —
de AGE-geheimen herversleutelt van de productiesleutel naar de sandboxsleutel.

**Dat kan in deze sessie niet.** Het script leest de productiesleutel onvoorwaardelijk:

```
655:  _, prod_private_key = read_age_key_file(args.prod_key)   # default ../../security/key.txt
270:  decrypted = decrypt_age_content_sync(raw_content, prod_private_key)
274:  re_encrypted = encrypt_age_content_sync(decrypted, sandbox_public_key)
```

`security/` bevat in deze container alleen `readme.md` en `tls/`. `security/key.txt` bestaat
niet, is nooit gecommit en is expliciet ge-gitignored (`/security/*`). De private helft staat
op de werkplek, niet op de dev-server. Zonder die sleutel is er niets te ontsleutelen en kan
de herversleutelstap dus niet.

De sandboxkant is er wel: `kubectl -n rig-system get secret sops-age-key` levert
`age1t69nngvl9kfnawqcmytyaq7lrkkl28zs6fkqfvazqpauqny4my3s4tscjw`, genoeg voor
`--sandbox-public-key`, maar dat lost de ontsleutelkant niet op.

Een halve conversie — cluster, domeinen en repo's omzetten maar de geheimen laten staan — is
bewust niet gedaan: de sandbox kan die bestanden dan niet ontsleutelen, dus zo'n run toetst
niets en zou een groen vinkje geven dat nergens op slaat.

**Nog te doen, met de sleutel erbij**: de conversie draaien, melden hoeveel van de 47 de
conversie zelf niet halen (dat is op zichzelf een bevinding), de uitvoer naar de
sandbox-Forgejo `zad-projects` pushen, en de verwerking op het cluster bekijken.

## 5. Bevindingen

### Bevinding 1 (blokkerend): projectverwerking faalt op `cli_client_id`

Geen unittest en geen lokale e2e ziet dit, want het gebeurt pas als er een echte
Keycloak-realm gerenderd wordt. Op de sandbox stopt de verwerking ermee:

```
opi.manager.project_manager - ERROR - Error processing project:
"Variable path not found: 'cli_client_id'. Available variables: ['project_name', 'cluster',
'keycloak_url', 'platform_realm_name', 'project_realm_name', 'project_display_name',
'platform_client_id', 'realm_name', 'realm_display_name', 'operations_manager_domain',
'invite_client_id', 'account_link', 'frontend_redirect_uris']. Ensure the variable is
defined in the 'variables' section of the Keycloak configuration."
```

**Oorzaak.** `611e2085` (RC-51) zette `{{ cli_client_id }}` en `{{ cli_token_audience }}` in de
projectrealm-templates `opi/configs/keycloak/sso-only.yaml` (r198, r219) en
`opi/configs/keycloak/sso-support.yaml` (r184, r205), maar definieerde ze alleen in de
platform-bootstrapcontext `opi/bootstrap/keycloak_setup.py` (r225-226). De projectrealm-context
uit `opi/manager/keycloak_manager.py` kent ze niet. De twee contexten staan in de log naast
elkaar: de platformcontext heeft `cli_client_id`, de projectcontext heeft in plaats daarvan
`account_link` en `frontend_redirect_uris`. `git log -S cli_client_id` op beide bestanden geeft
precies die ene commit.

**Omvang.** Niet één project maar de standaardweg: 18 van de 47 bestanden gebruiken
`template: sso-support` en 3 gebruiken `sso-only`, samen **21 van de 47**. Het raakt ook nieuwe
projecten, want `editables.py:16` heeft `default="sso-support"` en `config_model.py:103`
`default="sso-only"`. De sandbox-e2e liep er zelf op vast bij het aanmaken van `alls3-sm5`.

**Wel gerepareerd** (`cc1f4ed9`). Het plan zei "repareer niet onderweg", maar de opdrachtgever
vroeg tijdens de run om gevonden problemen ook op te lossen. De bevinding hierboven blijft staan
zoals hij gemeten is, zodat zichtbaar blijft wat er kapot was.

De eerste reflex — de twee variabelen aan de projectrealm-context toevoegen — is niet de goede.
Dan krijgt elk van de 47 projectrealms een publieke `zad-cli`-client die daar niets te zoeken
heeft: de CLI authenticeert een gebruiker die een project *aanmaakt*, en dat gebeurt tegen het
realm van de operations manager voordat er een projectrealm bestaat. Dat is een uitbreiding van
het aanvalsoppervlak, geen reparatie.

De client hoort dus niet in een blueprint die projectrealms delen. Hij staat nu in
`opi/configs/keycloak/operations-manager-realm.yaml`, het eigen blueprint van dat realm, en
`opi/configs/projects/operations-manager*.yaml` wijzen daarnaar.

Om dat te kunnen zonder een template van 266 regels te dupliceren kent een blueprint nu
`extends:`. `sso-support.yaml` blijft de basis; het eigen blueprint voegt alleen de client toe.
Bij het samenvoegen mergen dicts, en lijstitems overschrijven op identiteit (`clientId`, `realm`,
`alias`, `username`, `name`) of vullen aan als die identiteit nog niet bestaat — zodat een child
een realm kan aanpassen in plaats van er een tweede met dezelfde naam naast te zetten.

`tests/test_keycloak_template_variables.py` houdt de twee kanten voortaan tegen elkaar: elke
variabele die een projecttemplate noemt moet door `build_project_realm_context()` geleverd
worden. Die toets is afgeleid van de bestanden op schijf, dus een nieuwe template of een nieuwe
`{{ ... }}` valt er vanzelf onder. Dit is de toets die RC-51 zou hebben tegengehouden.
