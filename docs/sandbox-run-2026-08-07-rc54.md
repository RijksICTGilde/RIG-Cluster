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

## 4b. De geconverteerde bestanden verwerkt, een voor een

De geconverteerde set is aangeleverd (`rig-cluster-projects-sandbox` @ `368fca9`) nadat een
eerste poging nog productie-images en -resources bevatte. Nagemeten: 313 van de 314
image-regels zijn de probe, de ene rest is een database-image; 45 bestanden dragen
32Mi/10m met limiet 128Mi/200m; 47/47 staan op `sandboxed-local`; geen `cluster:` op
productie en geen repository-url naar productie.

Verwerken gaat per project met `POST /api/v2/projects/<naam>/:refresh`. Dat is nodig omdat
`ENABLE_GIT_MONITOR=false` staat op deze sandbox; een bestand in `zad-projects` zetten start
uit zichzelf niets. Het in git zetten is daarom ook geen verwerking en is in een keer gedaan;
de verwerking zelf is strikt een voor een.

**25 projecten gemeten: 24 completed, 1 failed.** Mediaan ongeveer 60s.

De ene fout is `amt-odc-prd`, reproduceerbaar over twee ronden:

```
"Alle deployments opnieuw verwerken": Remote source clone failed:
  ['Source validation failed: Failed to connect to external source
    localhost:41177/amt: [Errno 104] Connection reset by peer']
```

Dat is de `clone-from`-stap, die een database uit een externe bron haalt via een
chisel-tunnel. Die bron bestaat op een sandbox niet. Negen van de 47 bestanden dragen zo'n
`clone-from`. `amtbz-2m9` gaf in de eerste ronde dezelfde fout en was in de tweede gewoon
groen, dus het pad is bovendien wisselvallig.

### Waarom er 25 gemeten zijn en geen 47

De node liep vol: 117 pods op een capaciteit van 110, met 7 in `Pending`. Alles daarna zou
capaciteit meten in plaats van code, dus de ronde is daar gestopt. Het is niet dat 47
projecten niet passen, het zijn er een paar die heel groot zijn:

```
29 pods  rig-asses-k2n      (losse deployments per PR: pr-431, pr-432, pr-433, pr-450, ...)
17 pods  rig-mpfm-w3h       (hier zaten de 7 Pending)
12 pods  rig-mpfb-8wh
 3 pods  de rest, typisch
```

### De migratie, op een draaiend cluster bewezen

Bij verwerking migreert een bestand en schrijft OPI het terug. Dat is precies gebeurd:

```
git log zad-projects
  1e14de2 Process project mpfm-w3h
  00929b8 auto-migrate mpfm-w3h to schema v2.6
  880e746 auto-migrate mpfb-8wh to schema v2.6
  ...
  a4f378b Persist Keycloak realm credentials for jc-77j (sandboxed-local)
```

Dezelfde meting als in 3b, maar nu op wat OPI zelf heeft teruggeschreven:

| | 47 originelen | na verwerking van 25 |
|---|---|---|
| `config.keycloak`-restant | 21 | **0** |
| root-`domains:` | 30 | 13 (exact de nog niet verwerkte) |
| afgekeurd door de poort | 0 | **0** |
| afgekeurd na migratie | 0 | **0** |

De 13 die overblijven zijn de bestanden die nog niet aan de beurt waren. Geen enkel verwerkt
bestand haalt de validatie niet.

### ArgoCD-prestaties

Apart nagekeken of ArgoCD nog zijn cache leegt bij een nieuwe namespace (vroeger ~5 minuten
sync, verwachting nu maximaal 30s). Bij 23 projecten die allemaal een nieuwe namespace
aanmaken staat er **geen enkele** `progressing, waiting 10s... (elapsed: Ns)`-lus in de log.
De doorlooptijd per project is de hele keten (namespace, manifests, services, sync tot
Healthy) en zit op een mediaan van ~60s, met uitschieters omlaag naar 11-15s. De twee
uitschieters omhoog zijn niet Argo: `algor-odc` 626s (ImagePullBackOff op het database-image)
en `mb-grist-helmfile` 356s.

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

### Bevinding 2 (blokkeerde deze run): OPI wordt OOM-killed op 512Mi

De eerste ronde over de 47 klapte na zes projecten om; vanaf project 7 gaf alles `HTTP 503`.

```
Last State: Terminated   Reason: Error   Exit Code: 137
Limits: memory: 512Mi
```

Exit 137 kan ook een probe zijn, dus opgezocht op de node:

```
docker exec rig-sandbox-control-plane dmesg | grep -i oom
  Memory cgroup out of memory: Killed process 3413004 (kubectl) ... oom_score_adj:984
  oom_reaper: reaped process 3333162 (python)
```

Cgroup-OOM, niet de node (`MemoryPressure False`, 5GB vrij op de host). Vlak na een herstart,
zonder werk, stond de pod al op 429 MiB van de 512 MiB. Dat laat ~60 MiB over, en de connectors
forken `kubectl` binnen diezelfde cgroup - vandaar dat een kubectl-proces het slachtoffer werd.

Alleen de overlay `sandboxed-local` knijpt de limiet naar 512Mi; `base/deployment.yaml` staat op
1Gi, dus productie wordt hier niet door geraakt. **Gerepareerd**: de overlay staat nu ook op 1Gi.

Geen lek: over 23 projecten liep het verbruik van 393 naar 427 MB en vlakte af. Daarom is de
limiet verhoogd en niet gezocht naar een lek.

### Bevinding 3: er was geen manier om de projectenrepo op commando in te lezen

De store leest `zad-projects` op een poll van 300s. Een bestand dat daar door iets anders dan ZAD
in gezet wordt bestaat tot die tik niet: `No project found: wies`, en dus `401` op elke aanroep.
De git-monitor helpt niet - hij staat uit op de sandbox, kijkt naar een enkel pad
(`GIT_PROJECTS_SERVER_FILE_PATH=projects/simple-example.yaml`) en zijn handler doet alleen een
namespace-controle, geen verwerking.

**Toegevoegd** (`01ef77fe`): `POST /api/v2/admin/projects/:reconcile`, achter dezelfde
`ADMIN_API_KEY` als de andere admin-endpoints, met `head_before`/`head_after`/`changed` in het
antwoord. Het is dezelfde operatie als de poll, dus veilig op elk moment en een no-op als er
niets veranderd is.

Bijvangst: **`ADMIN_API_KEY` stond nergens gezet**, waardoor elk bestaand admin-endpoint
`501 This endpoint requires ADMIN_API_KEY to be configured` gaf. Voor de sandbox staat er nu een
vaste dev-waarde in de overlay, met dezelfde afweging als `SECRET_KEY`. Voor productie is dat een
eigen keuze (SOPS-secret) en die is hier niet gemaakt. Let op dat het bestaande
`POST /api/v2/admin/reconciliation/trigger` iets anders doet: dat gaat over
marked-for-deletion-resources, niet over de projectenrepo.

### Bevinding 4 (ernstig, niet gerepareerd): OPI start niet meer op zodra er genoeg realms zijn

Een VERS opgestarte pod komt niet meer READY; hij blijft steken in de Keycloak-bootstrap met
`400 Request Header Or Cookie Too Large` op `admin-otp`. De al draaiende pod merkt niets, dus
het bijt pas bij een herstart.

Het is niet de call maar de header. Zelfde token, zelfde verzoek:

| weg | uitkomst |
|---|---|
| `http://keycloak:8080` (intern) | 200 |
| `https://keycloak.sandbox.rijksapp.dev` (ingress) | 400 |

`KEYCLOAK_URL` wijst naar de publieke ingress, dus elke admin-call draagt een
`Authorization`-header van 8446 bytes langs een nginx met een standaardbuffer van 8k.

Waarom dat token zo groot is, gemeten en niet geraden:

```
payload 7984 van de 8437 b64-bytes
resource_access: 5268 bytes over 15 entries -- een per realm, elk ~337 bytes
```

Twee dingen samen: `opi-admin-service` heeft in master de realmrol `admin`, en dat is een
composite die de client-rollen van elke `<realm>-realm` omvat; en op die client staat
`fullScopeAllowed: True`, waardoor Keycloak die uitgeklapte rollen ook echt in elk token zet.
Het token groeit dus ~351 bytes per project, zonder bovengrens: 8.439 bytes bij 15 realms,
geprojecteerd ~19.223 bij de 47 van productie (`8439 + (47-15)*337`).

Met een handvol realms bleef het onder de 8k, en daarom is het niet eerder opgevallen. Deze run
maakte 25 projecten aan en liep er zo in.

Bewust niet gerepareerd: dit raakt Keycloak-autorisatie. De reflex `fullScopeAllowed` uitzetten
is een val, want de admin-REST-API autoriseert op de rollen IN het token; zonder expliciete
scope-mapping ruil je 400 in voor 403. Opties in volgorde van voorkeur: admin-verkeer intern
laten lopen (`http://keycloak:8080`, met de publieke URL alleen voor OIDC-redirects);
`fullScopeAllowed: false` met een expliciete mapping op alleen `admin`; of de nginx-buffers
omhoog, wat symptoombestrijding is omdat het token blijft groeien.

### Geen bevinding, wel goed om te weten

- **`algor-odc` hangt op een ImagePullBackOff** van
  `ghcr.io/rijksictgilde/algoritmeregister/postgresql-with-dictionaries:2024.11.19`, de ene image
  die de conversie bewust laat staan. Niet te trekken op dit cluster. Eén project, één image.
- **Negen bestanden dragen een `clone-from`** die een externe database via een chisel-tunnel
  ophaalt. Die bron bestaat op een sandbox niet.
- **De sandbox past geen 47 productieprojecten.** Drie grote projecten zijn samen 58 pods.
