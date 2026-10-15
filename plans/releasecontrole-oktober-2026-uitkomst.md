# Releasecontrole oktober 2026: uitkomst

Deze controle toetst of `forgejo/main` releasebaar is voor de productierelease van oktober
2026.

| | |
|---|---|
| Geteste commit | `78074173cd0c5595561f7217cbca4aeeaa91cd52` ("plans: invoer eerst schoon, dan getoetst", 2026-10-14 18:42:50 +0200) |
| Gelijk aan | `origin/main` en `main` op het moment van meten |
| Vorige productierelease (vergelijkingspunt) | `a5fef3052` (2026-09-28), de commit achter de image die de odcn-overlay pint: `operations-manager:2026.09.28.2215-a5fef305-dirty` |
| Image-tag op de sandbox | `localhost:5001/operations-manager:a3b014c8e`, pod `operations-manager-756c9d7bdc-5khzh`, `/version` meldt `a3b014c8ec90fae82804339071321b375d67a49f` |
| Machine | 4 cpu, 15 GiB geheugen, Python 3.14.6, pytest 9.0.3 |
| Sandbox-node | 1 node, 4 cpu, 16 GiB, max 110 pods |

Het verschil tussen de image en de geteste commit `78074173c` is uitsluitend dit rapport plus
de drie `bootstrap/`-overlaybestanden die `sandbox-deploy` zelf patcht en daarna terugzet. Geen
van die bestanden is Python-code, dus de code in de pod is die van de release.

## Uitkomst per stap

| # | Stap | Commando | Uitkomst | Aantallen | Duur |
|---|---|---|---|---|---|
| 1 | Statische controles | `uv sync --all-groups`; `uv run ruff check .`; `uv run ruff format --check .`; `uv run pyright` | groen | 0 lintfouten; 1285 bestanden al geformatteerd; 0 errors, 0 warnings, 0 informations | 36s |
| 2 | Unittests | `task test` | **1 rood** | 13254 geslaagd, 1 gefaald, 12 overgeslagen, 687 gedeselecteerd, 0 xfail | 11m46s |
| 3 | Upgrade-veiligheid, fixtures | `task test-upgrade-safety` | groen | 9 geslaagd, 1 overgeslagen, 1 gedeselecteerd | 1,4s |
| 3b | Upgrade-veiligheid, echte projectbestanden | `uv run pytest tests/test_upgrade_safety_replay.py -m "requires_infra and upgrade_safety"` | groen | 47 productiebestanden door migrate en validate | 3,6s |
| 4 | Integratie zonder cluster | `task test-authwall` | groen | 6 geslaagd, 0 overgeslagen | 52s |
| 5 | Lokale e2e | `task test-e2e` | **5 rood, 4 errors** | 518 geslaagd, 5 gefaald, 4 errors, 1 overgeslagen, 124 gedeselecteerd (van 528) | 18m20s |
| 5b | Lokale e2e, geschud | `task test-e2e-random` | **identiek aan 5**, seed `3105864670` | 518 geslaagd, 5 gefaald, 4 errors, 1 overgeslagen: exact dezelfde negen tests | 18m18s |
| 6 | Sandbox: uitrol en controle | `sandbox-deploy --sso-loos` | groen, met kanttekeningen | pod Running/Ready, 0 restarts; migraties door; portaal en inlog werken; 14 ArgoCD-apps Synced, 13 Healthy, 1 Suspended | 5m07s |
| 7 | Sandbox-e2e | `task test-e2e-sandbox` | **8 errors, 2 rood**, en de taak zoals hij er staat loopt niet af | van 102 geselecteerde tests: 77 geslaagd, 2 gefaald, 8 errors, 15 overgeslagen. De taak selecteert er 107; de 4 punt14-tests en 1 vastloper zijn eruit gelaten, zie bevinding 3 en 5 | 1u45m |
| 7b | Sandbox-e2e, reallife | `task test-e2e-sandbox-reallife` | groen | 7 geslaagd, 1 xfailed | 19m15s |
| 8 | Sandbox vrijgeven | `sandbox-release` | gedaan | `orch sandbox status` = FREE; node terug op 1635m (40%) en 34 pods | - |

Stap 3b staat er apart bij omdat `task test-upgrade-safety` alleen de vier gecommitteerde
fixtures draait: de replay over de echte projectbestanden is `requires_infra` en valt daarmee
buiten de standaard-markerselectie. Het plan vraagt "elk bestaand projectbestand", dus die is
er los bij gemeten; hij kloont de read-only projects-repo en draait 47 bestanden door
`migrate_to_latest` en de volle validatieketen.

## Wat de sandbox liet zien (stap 6)

- **Pod**: `operations-manager-756c9d7bdc-5khzh`, `Running`, `Ready`, **0 restarts**.
- **Migraties**: door. `Running Alembic migrations...` gevolgd door
  `Database migrations completed successfully` in de entrypoint, en daarna de sanity check uit
  `opi.core.startup` die niets meer te doen vond.
- **Portaal en inlog**: OIDC-keten werkt van `/auth/login` (realm `operations-manager`) tot
  dashboard met gebruikersgegevens ("10 Actieve Projecten, 12 Deployments").
- **ArgoCD**: alle 14 applicaties `Synced`; 13 `Healthy`, 1 `Suspended`
  (`algor-odc-infrastructure`, bevinding 4).

## Bevindingen

### 1. `test_sops_rotation_round`: een uitzondering voor een bestand zonder ciphertext (nieuw in deze release)

```
FAILED tests/test_sops_rotation_round.py::test_every_exception_names_a_file_that_is_really_there_and_really_holds_ciphertext
AssertionError: operations-manager/python/tests/e2e/fixtures/projects/test-project.yaml no longer carries ciphertext
```

**Classificatie: nieuw in deze release.** Gemeten op worktrees, niet aangenomen:

| commit | datum | uitkomst |
|---|---|---|
| `a5fef3052` (de productie-image) | 2026-09-28 | het hele bestand groen, 96 geslaagd |
| `c74f7178e^` | 2026-10-01 | groen |
| `c74f7178e` merge(RC-197) | 2026-10-02 | **rood** |
| `78074173c` (deze release) | 2026-10-14 | **rood** |

Oorzaak: `c74f7178e` haalde het AGE-blok uit de e2e-fixture
`operations-manager/python/tests/e2e/fixtures/projects/test-project.yaml` (de regels
`age-public-key:` en `-----BEGIN AGE ENCRYPTED FILE-----` zijn daar verdwenen), terwijl
`scripts/sops_rotation.py:106` dat bestand nog als uitzondering op de rotatiedekking noemt.
De toets is precies de guard die zo'n achterhaalde uitzondering moet vinden, en hij doet zijn
werk.

**Voorstel:** haal vier regels uit `COVERAGE_EXCEPTIONS` in `scripts/sops_rotation.py`, niet
een. De assertie loopt in een lus over die lijst
(`tests/test_sops_rotation_round.py:2444-2448`) en stopt bij de eerste achterhaalde entry, dus
de melding noemt er maar een terwijl er vier staande blijven:

| regel | entry (pad vanaf de repowortel) |
|---|---|
| `:106` | `operations-manager/python/tests/e2e/fixtures/projects/test-project.yaml` |
| `:107` | `operations-manager/python/tests/e2e/fixtures/projects/test-project-detail.yaml` |
| `:108-110` | `operations-manager/python/tests/e2e/fixtures/projects/test-project-with-services.yaml` |
| `:111` | `operations-manager/python/tests/e2e/test_lotc_niet_goedgekeurd_domein.py` |

Alle vier raakten achterhaald met dezelfde commit `c74f7178e`
(`git log -S 'BEGIN AGE ENCRYPTED FILE'` per pad). Zelf gedraaid met alleen `:106` weggehaald:
`1 failed`, nu op `test-project-detail.yaml`. Alle vier weggehaald: `96 passed`. Dat
`test-project-detail.yaml` nog armor-regels draagt helpt niet: het base64-blok is afgekapt tot
de AGE-header, zonder recipient- en MAC-regel, en `is_real_ciphertext` verwerpt het daarom. De
twee entries onder `docs/doorloop-rc108` en `-rc110` dragen wel nog echte ciphertext en blijven
staan.

**Weegt dit voor de release?** Het raakt geen draaiende code. `scripts/sops_rotation.py` is
gereedschap voor het roteren van de SOPS-sleutel, en de guard weigert pas iets op het moment
dat iemand die rotatie draait. Het risico zit dus niet in de uitrol maar in de volgende
sleutelrotatie, en daar is het een weigering en geen stille misser.

**Gerepareerd in `fa4f73cb0`.** De vier entries zijn uit `COVERAGE_EXCEPTIONS`. `uv run pytest tests/test_sops_rotation_round.py`: **96 passed**, waar dezelfde opdracht ervoor 1 failed en 95 passed gaf.

### 2. De sandbox-e2e: acht harde roden door een formulierveld dat verplicht werd (bestond al)

Twee module-fixtures die een project via de wizard aanmaken vallen om, in drie gangen
identiek gereproduceerd: `all_services_project` (`tests/e2e/test_sandbox_all_services.py:85`,
3 afnemers) en `invite_project` (`tests/e2e/test_sandbox_invite_ui.py:142`, 5 afnemers). Samen
precies de acht errors. De melding:

```
AssertionError: Did not reach the review page
(stuck at https://zad.sandbox.rijksapp.dev/forms/wizard/create-project/step/keycloak-config)
tests/e2e/helpers/lifecycle.py:288
```

De wizard is daarna met een browser doorlopen en op het scherm bekeken. Op de keycloak-stap
staat het veld **Keycloak template** op zijn placeholder "Kies een template", met eronder
`Dit veld is verplicht`.

Dat is geen storing in de app maar een bewuste wijziging die de toets niet heeft meegekregen.
In `opi/services/catalog/keycloak/editables.py` staat het er woordelijk bij: "GEEN default, en
dat is de hele reparatie. Hier stond `default="sso-support"`, terwijl het configmodel, het
API-schema en `KeycloakManager.DEFAULT_CONFIG` alle drie `sso-only` gebruiken. (...)
`required` dwingt dan een keuze af in plaats van er een te verzinnen."

Maar `tests/e2e/helpers/lifecycle.py:183` zegt nog steeds dat hij "leave[s] the template at
its default (sso-support)", en vult het veld dus niet. Dezelfde fout zit in
`test_sandbox_invite_ui.py::_walk_create_wizard`: die kiest keycloak + invite maar vult de
template niet, blijft op dezelfde stap staan en eindigt op
`assert saw_invite_step, "the invite config step never appeared in the create wizard"`.

**Classificatie: bestond al, sinds 2026-09-02.** Gemeten op de commit achter de
productie-image (`a5fef3052`): daar stond `required=True` al in de editable **en** stond in de
helper al diezelfde regel over "its default (sso-support)". De wijziging kwam met `d0ff2d6d0`
(2026-09-02, "fix(keycloak): het scherm toont de template uit het projectbestand"), bijna een
maand vóór de huidige productie-image. Deze acht tests stonden dus al rood toen de huidige
productierelease werd gebouwd.

**Voorstel:** op de keycloak-configstap de template kiezen, op één plek:
`_fill_service_config_step` in `tests/e2e/helpers/lifecycle.py` detecteert de stap al aan zijn
velden, dus daar hoort `wizard_helpers.kies_verplichte_keuzelijsten(page)` bij. Die helper
bestaat al (`tests/e2e/helpers/wizard.py:149`), doet precies dit (`select[required]` dat nog
leeg staat krijgt `select_option(index=1)`), noemt in zijn docstring precies deze wijziging
("De keycloak-template heeft geen default meer") en wordt al op zes plekken in drie
testbestanden zo aangeroepen, waaronder `test_wizard_services_regressions.py:144` en `:177`.
Op de keycloak-stap is index 1 `sso-only`, want dat is de eerste echte optie onder de lege
(`KeycloakTemplateOptionsProvider.get_options`, `opi/forms/visualizers/providers.py:514`). Wil
je de waarde expliciet pinnen in plaats van de eerste keuze te nemen, dan
`wizard_helpers.veldbesturing_eindigend_op(page, 'keycloak/config/template').first.select_option('sso-only')`;
dat pad is de staart van `KEYCLOAK_TEMPLATE_EDITABLE.yaml_path`
(`opi/services/catalog/keycloak/editables.py:25`).

Neem de helper; zet er geen eigen selector naast (hergebruik, één plek). Dit lost `all_services`
(3 errors) en `invite_ui` (5 errors) in één keer op.

Los daarvan, over het formulier zelf: de hulptekst van het veld zegt "Staat er nog niets in
het projectbestand, dan bouwt het platform de realm uit 'Alleen SSO Rijk'", terwijl het veld
`required` is. Die twee spreken elkaar tegen: of het mag leeg blijven en dan vult het platform
aan, of het moet gekozen worden. Een gebruikersvraag, geen releaseblokkade.

**Gerepareerd in `e10122158`.** `_fill_service_config_step` roept `kies_verplichte_keuzelijsten` aan en de docstring zegt niet langer dat de template op zijn default blijft; `_walk_create_wizard` in `test_sandbox_invite_ui.py` heeft zijn eigen lus en gaat daar niet langs, dus de aanroep staat er ook (dezelfde plek in de lus als `test_wizard_services_regressions.py:75`). Nagemeten op de sandbox tegen image `cac354d67`, elke module los op een rustig cluster: `test_sandbox_all_services.py` **5 passed**, `test_sandbox_invite_ui.py` **5 passed**, dus nul van de acht errors. Beide fixturen komen door de wizard heen. Een gang met de drie modules achter elkaar is hier GEEN geldige meting meer, om de reden die in bevinding 14 staat. En de reparatie legde een tweede fout bloot die hierachter zat: zie bevinding 15.

### 3. De sandbox-e2e loopt vast op de capaciteit van de node, door één suite die er niet in hoort (bestond al)

De eerste gang kwam in 65 minuten tot 46 van de 107 tests en stond toen 20 minuten stil. De
node op dat moment:

```
Allocated resources:  cpu 3995m (99%)   memory 11691Mi (73%)      102 pods
FailedScheduling  0/1 nodes are available: 1 Insufficient cpu.
```

Per namespace is het niet verdeeld maar één project:

```
  3100m  62 pods  rig-p1413-bj2      <- het project van test_sandbox_punt14.py
   950m  10 pods  kube-system
   315m  14 pods  rig-system
   270m   8 pods  de acht weesprojecten van eerdere runs, bij elkaar
```

Wat de app ondertussen deed was wat je dan verwacht: de ArgoCD-applicaties waren `Synced` maar
`Degraded`, nieuwe pods bleven Pending, en OPI liep per deployment tegen zijn eigen grens aan:

```
opi.manager.project_manager - ERROR - Timed out after 300s waiting for 'p1413-bj2-p14g3'
(p14g3) to sync; last observed state: wachten tot componenten gereed zijn
```

Waarom die ene test zo groot is: `tests/e2e/test_sandbox_punt14.py` maakt **per ronde een
nieuwe deployment** aan in één project (de fixture is module-breed), elk met een
`web`-component plus een tweede component die eraan gehangen wordt. `PUNT14_RONDES=10`
(`:71`) telt twee keer mee, want twee tests gebruiken hem: `p14r0` tot en met `p14r9`
(`:300-301`) en `p14g0` tot en met `p14g9` (`:352`). Daarnaast vijf uit
`PUNT14_RONDES_UITROL` (`:73`): `p14u0` tot en met `p14u4` (`:376`), en `p14nowait` (`:397`).
De bovengrens is dus 26 deployments en 52 pods.

Zo ver kwam hij niet, en hoe ver wel staat in de git-historie van `zad-projects`
(`projects/p1413-bj2.yaml`): `p14r0` tot en met `p14r9` tussen 19:50:05 en 19:53:01, `p14g0`
tot en met `p14g9` tussen 19:53:14 en 19:55:24, daarna `p14u0` (19:58:34), `p14u1` (20:15:29)
en `p14u2` (20:17:39). Dat gat van bijna zeventien minuten vóór `p14u1` valt samen met de
stilstand hierboven. Op zijn hoogtepunt stonden er 24 deployments in dat ene project, de
`productie` van de wizard meegeteld, met 45 componentinstanties. Elke ronde patcht de test
bovendien het geheugenlimiet van de extra componenten, en elke patch geeft elke deployment die dat
component gebruikt een nieuwe ReplicaSet; de oude pods blijven Pending omdat de nieuwe er
niet bij kunnen. Zo werd het 62 pods. De deployments worden met `rollout=false` aangemaakt,
maar dat houdt ze niet tegen: de app-of-apps maakt de ArgoCD-applicaties uit het
projectbestand, en daar staan ze in. Die 62 pods in `rig-p1413-bj2` zijn daar het bewijs van.

**En die suite hoort niet in de standaardrun.** Zijn eigen docstring zegt het:

> Bedoeld om te draaien TERWIJL de reallife-suite loopt, zodat er werkelijk belasting op de
> store staat. Eigen marker (``punt14``), buiten de standaard sandboxrun.

Maar `task test-e2e-sandbox` selecteert `-m "e2e and sandbox and not reallife"`, en het bestand
draagt ook `e2e` en `sandbox`. Dus hij zit er wel in: 4 van de 107 geselecteerde tests. En zijn
timeouts zijn 5400s, 5400s, 7200s en 1800s, bij elkaar 5,5 uur voor die ene module.

**Classificatie: bestond al, en het is een gat tussen twee wijzigingen.** De markerexpressie
staat in de Taskfile sinds 2026-07-22 (`3aa91f71b`); de punt14-suite kwam er op 2026-08-14 bij
met die docstring. `punt14` heeft **nooit** in de Taskfile gestaan (`git log -S punt14 --
Taskfile.yaml` is leeg).

**Voorstel:** `not punt14` aan de markerexpressie van `test-e2e-sandbox`, zoals `not reallife`
daar al staat, en een eigen taak `test-e2e-sandbox-punt14` ernaast voor wie hem wel wil
draaien, zoals `test-e2e-sandbox-reallife` dat doet.

**Gerepareerd in `38c7081b3`.** `not punt14` staat in de markerexpressie van `test-e2e-sandbox`, en de taak `test-e2e-sandbox-punt14` staat ernaast naar het model van `test-e2e-sandbox-reallife`. Gemeten met `--collect-only`: **107 geselecteerd met, 103 zonder**, en de nieuwe taak verzamelt precies die 4.

### 4. ArgoCD: `algor-odc-infrastructure` staat Suspended (omgeving, 58 dagen oud)

De applicatie is `Synced` maar `Suspended` sinds 2026-10-12. De oorzaak zit een laag dieper:
het CNPG-cluster `algor-odc-db` meldt `Unable to create required cluster objects`, en zijn
initdb-pod hangt sinds 58 dagen in `ImagePullBackOff`:

```
ghcr.io/rijksictgilde/algoritmeregister/postgresql-with-dictionaries:2024.11.19
ErrImagePull: failed to authorize: failed to fetch oauth token: ... 403 Forbidden
```

**Classificatie: omgeving.** Een private image uit een GitHub-registry waarvoor deze sandbox
geen geldige credential heeft; het project `algor-odc` gebruikt hem via zijn
`image-registries`-config. De 403 komt van ghcr.io, niet van de code, en stond er al in de
baseline van vóór deze uitrol.

**Voorstel:** niets voor de release. Wil iemand de sandbox schoon hebben, dan is de weg een
geldige `github-registry`-credential in dat projectbestand, of het projectbestand uit de
sandbox-`zad-projects` halen.

### 5. Een vastloper in een sandboxtest raakt geen enkele timeout

Test 50 van de tweede gang,
`test_sandbox_repetitie.py::test_een_afgewezen_component_levert_een_taak_die_zegt_dat_het_misging`,
heeft meer dan 25 minuten stilgestaan terwijl `--timeout=300` aanstond. OPI deed in die
periode niets meer voor de taak van die test; pytest stond in `selectors.select` binnen
`playwright/sync_api/_context_manager.py:56 greenlet_main`. De SIGALRM van pytest-timeout komt
daar niet door, dus de suite hangt onbeperkt in plaats van die ene test rood te maken.

**Classificatie: omgeving/gereedschap, niet gemeten of het nieuw is.** De test is daarna
gedeselecteerd om de rest van de suite wel te kunnen meten.

**Voorstel:** `tests/e2e/conftest.py` legt uit dat per-test-timeouts "from the helpers' own
bounded waits" komen. Voor de browserkant is dat niet waar. Een harde grens op de
Playwright-acties zelf (`page.set_default_timeout` op de sandbox-context) bijt hier wel, en
maakt een vastloper één rode test in plaats van een vastgelopen run.

**Gerepareerd in `cac354d67`, maar de oorzaakclaim hierboven is weerlegd.** Het budget staat nu expliciet en met een naam op de sandbox-context (`SANDBOX_ACTIE_BUDGET_MS`, voor handelingen en voor navigeren), en de bewering in `conftest.py` noemt naast de wachtlussen van de helpers ook pytest-timeout en dit budget. Twee metingen spreken de diagnose echter tegen. Ten eerste is de impliciete grens van Playwright **30s** en niet oneindig: een `click` op een onbestaande selector faalt na 30,0s, en `context.set_default_timeout(5000)` brengt dat op 5,0s. De browserhandelingen waren dus al begrensd, en dit is de grens benoemen met ruimte erboven voor een live cluster, geen verscherping. Ten tweede **komt de SIGALRM van pytest-timeout daar wel door**: met `@pytest.mark.timeout(5)` om een `click` met `timeout=60000` is de test rood na 5,3s, met de traceback in `selectors.poll`, precies het frame dat hierboven staat. De test waar het gebeurde doet bovendien zelf geen enkele Playwright-aanroep (httpx plus `_wacht_op(timeout_s=300)`) en zijn module draagt geen `timeout`-marker die `--timeout=300` zou overschrijven. Die 25 minuten zijn dus nog niet verklaard en blijven een open punt; zie ook bevinding 14.

### 6. Twee ERROR-regels bij het opstarten: een 409 op een client scope die al default is (bestond al)

```
opi.connectors.keycloak - ERROR - [-] Failed to assign client scope as realm default: 409: b'{"error":"unknown_error","error_description":"For more on this error consult the server log at the debug level."}'
```

Twee keer, in de realms `rig-platform` en `operations-manager`, telkens direct na
`Client scope 'custom_attributes_passthrough' already exists in realm '...'`. Het opstarten
loopt er zonder onderbreking langs: de regel erna is `Keycloak configuration execution
completed` en daarna `Bootstrap realm setup completed`.

**Classificatie: bestond al.** De logregel staat op `opi/connectors/keycloak.py:2119`, is daar
op 2025-11-21 (`d081d980a`) neergezet en staat ook in de commit achter de huidige
productie-image. Hij vuurt alleen bij een herhaalde bootstrap tegen een Keycloak waar de scope
al realm-default is, en dat is wat de sandbox elke uitrol doet.

**Voorstel:** `assign_client_scope_as_realm_default` vangt `KeycloakError` en logt alles als
ERROR. Een 409 betekent daar "staat er al" en is de gewenste eindtoestand, dus die hoort INFO
te zijn zoals de aanroepen eromheen ("already exists, skipping").

**Gerepareerd in `ab6314c59`.** Een 409 logt nu als INFO en geeft `True` terug, want de eindtoestand IS bereikt; andere fouten blijven ERROR en `False`. Nagemeten op de sandbox na de uitrol van `cac354d67`: beide realms melden `Client scope '...' is already realm-level default, skipping` op INFO, en het volle opstartlog heeft **geen enkele ERROR-regel** meer. Drie unittests in `tests/test_keycloak_realm_default_scope.py`, waarvan de 409-tak rood is op de oude code (1 failed, 2 passed).

### 7. Federatieve SSO is op deze sandbox niet te meten, en dat is een keuze

`sandbox-deploy` weigerde eerst, omdat
`bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/operations-manager-env-secrets.yaml`
in een verse checkout niet bestaat (gitignored) en de generator hem anders met **lege**
OIDC-secrets vult op een **gedeeld** cluster. Gemeten na overleg:

- `.sandbox-sso` op de hostmachine bevat `no`. Dat is het bewaarde antwoord uit de setup van
  2 augustus. `task sandbox:ask-sso` zegt er zelf bij: zonder SSO werkt de sandbox met een
  lokale Keycloak-beheerder, en applicaties kunnen Keycloak wel gebruiken maar geen SSO.
- Echt SSO vraagt het clientsecret van de `development-clusters`-client uit de productierealm
  `rig-platform` op `keycloak.rijksapp.nl`. Dat is een productiecredential en hoort niet in een
  workercontainer. Aanzetten is `task sandbox:configure-sso` door een mens die dat secret heeft.
- De uitrol is daarom met `--sso-loos` gedaan. Dat degradeert hier niets: het secret
  `operations-manager-env-secrets` in `rig-system` had `OIDC_CLIENT_SECRET` en
  `KEYCLOAK_MASTER_OIDC_CLIENT_SECRET` al op 0 bytes staan **voor** deze run.

Wat daarmee wel gemeten is: de volle OIDC-inlog op het portaal tegen de lokale Keycloak, van
`/auth/login` tot een gevuld dashboard, plus de inlogketen door oauth2-proxy in stap 4. Wat
niet gemeten is: de federatieve inlog via SSO-Rijk, inclusief de terugreis en de uitlogketen
over de productie-IdP.

**Voorstel:** RC-246 ("Het sandboxed-local secret versleuteld in git, met een sleutel die de
sandbox heeft") staat op `needs_human_review` en is de reparatie van deze poort. Is de lat voor
de release een federatieve inlogtoets, dan moet SSO op de sandbox aan met dat productiesecret,
en dat is een releasebesluit.

### 8. De infra van deze release heeft op de sandbox nooit gedraaid, en de taak die dat zou oplossen is destructief

Dit is de zwaarste kanttekening bij stap 6.

| | |
|---|---|
| laatste commit in de in-cluster `zad-argo-infrastructure` | `c6a4a3d5d9`, **2026-09-26** ("RC-212: sandbox terug naar de toestand van voor de proef") |
| laatste ArgoCD-sync van `sandbox-infrastructure` | 2026-09-26T12:58, Succeeded; Synced/Healthy, maar op die oude stand |
| Keycloak-overlay in die in-cluster repo | geen `images:`-blok, dus de base |
| Keycloak die draait | `quay.io/keycloak/keycloak:25.0.6`, pod gestart 2026-09-26, deployment 61 dagen oud |
| Keycloak-overlay op main | `ghcr.io/rijksictgilde/zad/keycloak-zad:2026.10.13.1948-72684cb4` |
| gewijzigd in `infrastructure/` + `bootstrap/` sinds 26 sep | **85 bestanden**, waarvan 9 in `mail` en 9 in `keycloak` |

`sandbox-deploy` rolt alleen de operations-manager uit. De sandbox is dus bewijs voor de **app**
van deze release op infra van 18 dagen oud. De ZAD-Keycloak uit RC-260 en de mailrelay uit
RC-261 zitten wel in deze main maar hebben op dit cluster nooit gedraaid.

De taak die dat zou bijwerken, `task sandbox:sync` (Taskfile.yaml:3100), doet:

```
rsync -av --delete --exclude='.git' infrastructure/ "$TEMP_DIR/"
```

In de in-cluster repo staan 15 bestanden die niet in main zitten (naast 24
`overlays/local`-bestanden die main terecht heeft verwijderd). Dertien daarvan zijn de
gegenereerde sandbox-secrets onder `bootstrap/infrastructure/secrets/config/overlays/sandboxed-local/`:
keycloak-admin, keycloak-db, postgres-admin, minio-admin, forgejo-db, redis-admin, pgadmin,
vault-init, chisel-auth, mail-relay, prometheus-auth, plus `decrypt-sops.yaml` en de
`kustomization.yaml` die ze bindt. `.gitignore:41` negeert die hele map, dus ze bestaan alleen
in de in-cluster repo. Met `--delete` gooit de sync ze weg en prunet ArgoCD ze daarna uit het
cluster: Keycloak, Postgres, MinIO, Forgejo en Redis verliezen in één keer hun credentials.

Dat is geen theorie, het staat in de geschiedenis van die repo:

```
71bb57b25b  2026-09-26  RC-212: de gegenereerde sandbox-secrets terug (die staan niet in git)
7f2ca36d9c  2026-09-26  RC-212: sandbox terug naar de toestand van voor de proef
```

Daarom **niet gedraaid**: het cluster is gedeeld en dit zou het onbruikbaar maken voor elke
volgende PR.

Waarom die secrets niet in git staan, en waarom RC-246 dat niet oplost:

| `.gitignore` | wat het negeert | status |
|---|---|---|
| regel 36 | `operations-manager-env-secrets.yaml`, het ene OPI-secret | RC-246, `needs_human_review`. Besluit: gaat versleuteld in git |
| regel 41 | de map `secrets/config/overlays/sandboxed-local/`, dertien infra-secrets | sinds 2026-02-11 (`a58981fd7`), nooit aangeraakt, geen taak voor |

Voor `odcn` staan diezelfde secrets wel in git. `sandboxed-local` is de enige overlay die ze
mist. RC-246 noemt alleen regel 36 en zet onder "Buiten scope" expliciet dat `local` en
`odcn-production` niet worden aangeraakt, dus ook daarna blijft de sync destructief.

**Voorstel, twee wegen die elkaar niet uitsluiten:**

1. De korte: `--exclude='bootstrap/infrastructure/secrets/config/overlays/sandboxed-local/'` aan
   die rsync, zodat `--delete` de gegenereerde secrets laat staan. Repareert de destructie, niet
   de oorzaak.
2. De echte: diezelfde behandeling als RC-246 voor regel 41, versleuteld in git met de sleutel
   die een sessie mount, met de `.sops.yaml` en creation_rule die daar al bij horen. Dan is de
   sandbox vanuit git op te bouwen en is de sync gewoon een sync.

**Weg 1 gerepareerd, nu als protect-regel.** Op de rsync van `sandbox:sync` staat
`--filter='P /bootstrap/infrastructure/secrets/config/overlays/sandboxed-local/***'`. Een
protect-regel houdt alleen `--delete` weg bij die map; de weg heen blijft open, wat nodig is
omdat fase 3 van `task sandbox:setup` ("Sync infrastructure (including generated secrets) to
Forgejo", `Taskfile.yaml:2321`) juist de secrets aflevert die
`sandbox:generate-infrastructure-secrets` daar net in de werkboom heeft geschreven.

De eerste poging was `--exclude='bootstrap/.../sandboxed-local/'` (voorstel 1 hierboven,
`02b9c687e`) en die is fout: een exclude sluit ook de push uit, dus een verse setup pusht een
boom zonder die dertien bestanden. Beide vormen zijn op de server gemeten (rsync 3.2.3, in
een scratchmap); in deze container zit geen `rsync`, dus de meting is niet van mij:

| vorm | uitkomst |
|---|---|
| `--filter='P bootstrap/.../sandboxed-local/'` | **werkt niet.** Beschermt alleen de map zelf; de bestanden die alleen de ontvanger heeft worden toch geprunet |
| `--filter='P /bootstrap/infrastructure/secrets/config/overlays/sandboxed-local/***'` | **werkt.** Bestanden die alleen de ontvanger heeft blijven staan, bestanden die de bron heeft gaan mee, en `--delete` werkt daarbuiten gewoon (een map die alleen de ontvanger had verdween) |

Het pad dat dit raakt was al nagemeten: de in-cluster `zad-argo-infrastructure` heeft onder
`bootstrap/infrastructure/secrets/config/overlays/sandboxed-local/` precies die **13
bestanden** (via de Forgejo-API opgevraagd), en in de werkboom bestaat onder
`secrets/config/overlays/` alleen `odcn`, dus `--delete` prunette alle dertien. Weg 2, de
echte, blijft open.

### 9. Oude resten op de sandbox, en één project dat niet te verwijderen is

Het cluster stond vol met projecten van eerdere runs. Opgeruimd met
`task sandbox:verwijder-weesproject`:

| | |
|---|---|
| weg | `amtbz-2m9 cot-zaq jc-77j jp-574 pgsch-2iw regis-0vf rest4-4sb` (van eerdere runs) en `p1413-bj2 e2e99-q3q repet-6xy nsv70-rwt` (van deze controle) |
| cpu-requests | 1945m (48%) -> 1685m (42%) |
| pods | 41 -> 35 |
| `rig-`-namespaces | 12 -> 4 |

Eén weigert, en dat is een eigen bevinding:

```
Project 'mzs-3ik' niet volledig verwijderd: ArgoCD folder 'sandboxed-local/mzs-3ik' was
expected but not found in the GitOps repository; refusing to continue so the project file
is not removed while ...
```

Het project is half verwijderd: zijn map in de GitOps-repo is al weg, en de guard weigert
daarom de rest. Dat is een redelijke guard zonder uitgang: via de normale weg is dit project
nooit meer op te ruimen, en het bestand blijft in `zad-projects` staan. Zijn namespace is er
niet meer, dus hij kost geen capaciteit, maar hij blijft in elke projectlijst staan.

**Voorstel:** een weg uit die toestand. Of de guard mag doorlopen als de map ontbreekt én de
namespace ook al weg is (er is dan niets meer om te beschermen), of er komt een expliciete
forceervorm die het projectbestand alsnog weghaalt.

Twee andere resten heb ik laten staan, omdat ze buiten deze taak vallen: namespace `rig-ron`
met een `Completed` pod `probeuid` van 56 dagen zonder projectbestand (60m cpu-requests), en de
`algor-odc`-initdb uit bevinding 4.

Bijvangst: `task sandbox:verwijder-weesproject` gaf exit 1 op `p1413-bj2` met "de taak was na
600s nog niet klaar", terwijl de verwijdering daarna gewoon doorliep en een halve minuut later
klaar was. Met 24 deployments in één project is die 600s krap, en de melding leest als een
mislukking terwijl het een nog-niet-klaar is.

### 10. Vijf sandbox-tests zijn wisselvallig onder belasting

Naast de acht harde roden uit bevinding 2 vielen vijf tests in de ene gang om en in de
andere niet: drie uit `test_sandbox_flows.py` (de afnemers van `lifecycle_project`, `:72`) en
twee uit `test_sandbox_all_services.py` (de afnemers van `namespace_variants_project`, `:373`).
Dat derde module-project wordt met alleen `publish-on-web` aangemaakt en raakt de
keycloak-configstap dus nooit, waarmee het buiten bevinding 2 valt. De twee groepen vielen niet
in dezelfde gang om. Eerst de drie uit `flows`, gemeten in drie standen:

| stand | `test_sandbox_flows` |
|---|---|
| los | **5 van 5 groen** (4m19s) |
| na `all_services` | **5 van 5 groen** |
| in de volle suite (2x) | fixture ERROR na 88,8s |

Geen vaste fout. De twee uit `all_services` zijn de spiegelkant: die vielen in de
gecombineerde gang juist wel om terwijl ze los groen waren (`namespace_variants`,
`namespace_postgres`), met
`playwright._impl._errors.TimeoutError: Locator.wait_for: Timeout 240000ms exceeded`. Hetzelfde
beeld: onder belasting halen de wizardwachten van 240s het niet.

**Classificatie: wisselvallig/omgeving**, op een node die voor deze suite te klein is. Dat
hangt samen met bevinding 3: zolang punt14 meedraait is de node vol en is elke wacht van 240s
een gok.

### 11a. Negen rode wizardtests in de lokale e2e: `PROJECT_REPO_PASSWORD` staat niet in de omgeving (omgeving)

```
FAILED tests/e2e/test_wizard_cross_domain_policy.py::TestTheWizardProducesANetworkPolicy::test_a_rule_filled_in_the_browser_becomes_a_networkpolicy
FAILED tests/e2e/test_wizard_cross_domain_policy.py::TestTheWizardProducesANetworkPolicy::test_a_peer_that_does_not_exist_here_still_yields_the_policy
FAILED tests/e2e/test_wizard_services_regressions.py::test_preset_stays_applied
FAILED tests/e2e/test_wizard_services_regressions.py::test_unlocking_a_dependency_does_not_duplicate_it
FAILED tests/e2e/test_wizard_services_regressions.py::test_een_uitnodiging_zonder_eigen_sleutel_wordt_opgeslagen
ERROR  tests/e2e/test_wizard_webadres_landt_onder_de_dienst.py (4 tests, fixture created_project)
```

De negen melden in vier vormen, verdeeld over drie bestanden, dat de wizard geen
projectbestand opleverde. Letterlijk, per vindplaats:

`test_wizard_cross_domain_policy.py:263`, de twee tests uit dat bestand:

```
AssertionError: the wizard created no project
(stuck at http://127.0.0.1:.../forms/wizard/create-project/step/domains)
```

`test_wizard_webadres_landt_onder_de_dienst.py:183`, de vier tests van het bestand, elk via een eigen run van de fixture `created_project` (`:160`):

```
AssertionError: de wizard maakte geen project aan
(vast op http://127.0.0.1:.../forms/wizard/create-project/step/domains)
```

`test_wizard_services_regressions.py:148` en `:181` (`test_preset_stays_applied` en
`test_unlocking_a_dependency_does_not_duplicate_it`):

```
AssertionError: no project YAML captured
```

`test_wizard_services_regressions.py:291`
(`test_een_uitnodiging_zonder_eigen_sleutel_wordt_opgeslagen`):

```
AssertionError: de wizard heeft geen projectbestand opgeleverd; het opslaan is mislukt
```

Geen van de negen liep vast op een stap. Alle vijf de meldingen komen van een
capture-assertie NA de submit: `test_wizard_cross_domain_policy.py:263`,
`test_wizard_webadres_landt_onder_de_dienst.py:183` en
`test_wizard_services_regressions.py:148`, `:181` en `:291`. Twee daarvan lezen op dat moment
ook `page.url` mee, en daar komen `(stuck at ...)` en `(vast op ...)` uit:
`test_wizard_cross_domain_policy.py:263` en
`test_wizard_webadres_landt_onder_de_dienst.py:183`. De drie uit
`test_wizard_services_regressions.py` melden kaal, zoals hun citaten hierboven laten zien.
Elke wandelhelper heeft een eigen melding voor een echte vastloper
(`test_wizard_cross_domain_policy.py:103`, `test_wizard_services_regressions.py:78` en `:104`,
`test_wizard_webadres_landt_onder_de_dienst.py:99`) en geen daarvan vuurde. De wizard liep dus
alle stappen af, `domains` incluis, en het OPSLAAN werd afgekeurd. Die afkeuring rendert de
wizard opnieuw op een stap (`router_wizard.py:2513-2525`: een veld dat geen stap bezit landt op
de stap waarvan is ingediend, of op de laatste actieve stap), en dat is de url die in die twee
meldingen terechtkomt.

Het log van de run zegt het woordelijk. Per wizardgang staat er eerst
`[domains] All validations passed. Forward navigation: is_forward=True, goto='next'`
(`router_wizard.py:1065`) en daarna, met per gang een andere gegenereerde projectnaam:

```
WARNING opi.web.router_wizard:router_wizard.py:2310 Wizard built an invalid project file for
  webad-l9u (field=repositories/0/password): de waarde is afgekeurd door schemaregel 'oneOf'
WARNING opi.web.router_wizard:router_wizard.py:2502 Wizard save rejected by validation for
  (new): Veld 'repositories/0/password' voldoet niet aan het projectschema: de waarde is
  afgekeurd door schemaregel 'oneOf'.
```

**Classificatie: omgeving.** De keten is aanwijsbaar: `opi/configs/project-template.yaml:20`
zet `password: "${PROJECT_REPO_PASSWORD}"`, `_resolve_placeholders`
(`opi/forms/editables/template.py:24-25`) vult zo'n placeholder met
`str(getattr(settings, naam))`, en `PROJECT_REPO_PASSWORD` staat op `None`
(`opi/core/config.py:242`), is in deze container niet gezet en staat niet in de getrackte
`.env`. Het veld wordt daarmee de tekst `None`, en het schema laat op
`repositories[].password` alleen `age-encrypted-or-plain` toe: age-versleuteld, of een waarde
met het voorvoegsel `plain:` (`project_v2.json:62-67`, `:97`). `None` is geen van beide, en dat
is de `oneOf` uit de melding.

De pin: dezelfde drie bestanden met `PROJECT_REPO_PASSWORD='plain:repo-secret'` geven
**12 geslaagd** in 2m24s. Zonder die variabele, in dezelfde container en op dezelfde commit:
5 gefaald, 3 geslaagd, 4 errors.

Daarmee zegt de vergelijking met een worktree van de productie-commit `a5fef3052` hier niets:
die liep in dezelfde container zonder die variabele en was dus per constructie identiek. Het is
geen grond voor "bestond al". Deze negen meten de omgeving van de run en niet de code van de
release.

Dat de wizard deze variabele nodig heeft is wel ouder dan deze release: `f59a2d819`
(2026-09-28 17:53) haalde de in de image gebakken default uit `config.py` en zette hem op
`None`, en die commit zit al in de productie-commit `a5fef3052` (2026-09-28 22:15). De eis is
dus niet nieuw; wat deze controle meet is dat de container waarin de suite liep er niet in
voorziet.

Geen ordeafhankelijkheid: `task test-e2e-random` met seed `3105864670` gaf exact dezelfde negen
tests rood (de lijsten zijn met `diff` vergeleken en identiek). Dat past bij een oorzaak in de
omgeving: die hangt niet van de volgorde af.

**Voorstel:** `PROJECT_REPO_PASSWORD` hoort bij het draaien van de lokale e2e-suite, dus zet
hem waar die suite start en schrijf hem op (bevinding 13: hij staat nergens gedocumenteerd).
Pas daarna zegt een rode wizardtest in deze suite iets over de code.

**Wat hier wel een eigenschap van de repo is:** `ci.yml:69` draait
`-m "not slow and not e2e and not requires_infra and not sandbox"`. De 528 lokale browsertests
zitten dus in **geen enkele automatische poort**, en dat is wat de acht van bevinding 2 en de
twee van 11b stil kon laten staan: een suite die alleen draait als iemand hem met de hand
start, verzamelt stille roden. Het is ook waarom deze omgevingseis pas bij een handmatige gang
opviel. Wie de lokale suite in CI zet, moet er `PROJECT_REPO_PASSWORD` bij zetten, anders is de
poort meteen rood op deze negen.

**Gerepareerd in `403a74fe7`.** `test-e2e` en `test-e2e-random` zetten `PROJECT_REPO_PASSWORD=${PROJECT_REPO_PASSWORD:-plain:repo-secret}`, dezelfde `${VAR:-default}`-vorm die `test-e2e-sandbox` al gebruikt, dus een eigen waarde in de omgeving gaat nog voor (beide takken in `/bin/sh` gemeten). Op de drie bestanden van deze bevinding: **zonder de variabele 5 failed, 4 errors en 3 passed; met de variabele 12 passed.**

### 11b. Twee rode VLAM-tests, beide drift tussen toets en code (bestond al)

```
FAILED tests/e2e/test_sandbox_vlam.py::test_the_egress_policy_opens_only_the_proxy[chromium]
E       assert [8081, 8443] == [8081]
E         Left contains one more item: 8443
tests/e2e/test_sandbox_vlam.py:162

FAILED tests/e2e/test_sandbox_vlam.py::test_the_test_vlam_button_gets_an_answer[chromium]
E       playwright._impl._errors.Error: Page.fill: Error: Element is not an <input>, <textarea> or [contenteditable] element
E         - locator resolved to <select required="" name="model" id="vlam-model">...</select>
tests/e2e/test_sandbox_vlam.py:331
```

De eerste: de uitgaande netwerkregel opent twee poorten en de toets eist er een. Dat de regel
er twee opent is bewust. `VlamEndpoint.ports` in
`opi/services/catalog/vlam/endpoint.py` zegt het erbij: "the doorlus sits on a second port of
the SAME pod, and an address the consumer is given but the network rule does not open is the
timeout this module exists to prevent". De toets is dus achtergebleven bij RC-167.

De tweede: de pagina die de toets invult komt niet uit OPI maar uit de testapplicatie
(`RUNNABLE_IMAGE = "ghcr.io/minbzk/base-images/e2e-allservices:latest"`, via een port-forward
naar poort 8080 van de pod). Die applicatie staat in deze repo: `images/e2e-allservices/`,
gepubliceerd met `task publish-e2e-allservices` (`Taskfile.yaml:1093-1105`), en
`lifecycle.py:51-53` wijst daar bij de definitie van `RUNNABLE_IMAGE` ook naartoe. In
`server.go` rendert `renderModelField` (`:302-325`) het modelveld in twee vormen: kent de
probe modellen, dan is het een `<select id="vlam-model">` (`:309`), en anders een
`<input id="vlam-model" type="text">` (`:304`). De toets vult met `page.fill`, en dat werkt
alleen op die tweede vorm.

**Classificatie: bestond al, voor beide.** Gemeten tegen de commit achter de productie-image:
`tests/e2e/test_sandbox_vlam.py` is byte-identiek, `endpoint.py` is byte-identiek, en
`passthrough_port: 8443` stond al in de `sandboxed-local`-clusterconfig (regel 161 daar, 163
nu). De enige wijziging in de vlam-code sinds die commit is een icoonnaam
(`square-and-arrow-down` naar `square-arrow-down`). Omdat de bron hier staat, is de
veroorzakende commit voor de tweede ook aan te wijzen:
`git log -S '<select id="vlam-model"' -- images/e2e-allservices/server.go` geeft `b774529cd`
(2026-09-22), ruim vóór de productiecommit `a5fef3052` (2026-09-28).

**Voorstel:**
- `test_the_egress_policy_opens_only_the_proxy` moet `endpoint.ports` verwachten in plaats van
  `[endpoint.port]`, want dat is wat de code bewust genereert.
- `test_the_test_vlam_button_gets_an_answer` moet kiezen op de tag die er staat:
  `select_option` op een `<select>`, `fill` op het tekstveld dat `renderModelField` teruggeeft
  als de probe nog geen modellen kende. Kaal `select_option` ruilt de ene rode in voor de
  andere zodra die tweede tak aan de beurt is.
- En los daarvan: `RUNNABLE_IMAGE` staat op `:latest`, terwijl publiceren een losse taak is
  die niemand aan een commit bindt. De suite draait dus wat er het laatst gepusht is, en een
  publicatie uit deze repo kan haar rood maken zonder dat er iets aan de suite verandert.
  Dat hoort een vastgezette tag te zijn, net als elke andere image die het platform pint.

**Gerepareerd in `753085af8`.** De poortassertie leest `endpoint.ports`, en het modelveld wordt gezet op de tag die er staat (`select_option` op een `<select>`, `fill` op het tekstveld), zodat beide takken van `renderModelField` werken. Nagemeten op de sandbox, de module los op een rustig cluster: `test_sandbox_vlam.py` **7 passed in 218,90s**, met `test_the_egress_policy_opens_only_the_proxy` en `test_the_test_vlam_button_gets_an_answer` daarin groen. De `:latest`-kanttekening blijft open, dat is een bewuste keuze.

### 11c. De sandbox-suite kost vooral fixturetijd, niet testtijd

Gemeten op de derde gang, uit `PYTEST_VOORTGANG` (dat per test de duur uit pytests eigen
report-object schrijft):

| | |
|---|---|
| wandkloktijd voor 76 van de 102 tests | 83 min |
| som van de testduren zelf | **28 min** |
| tijd buiten de tests, dus in fixtures | **55 min, 66%** |
| modules | 26, gemiddeld **128s per module** aan alleen opzetten en afbreken |

De duurste fixtures: `backup_serviceaccount` 466s, `tls_override` 283s, `repetitie` 245s,
`all_services` 242s, `flows` 215s. Elke module maakt zijn eigen project aan via de
browserwizard en verwijdert het daarna: namespace, Keycloak-realm, CNPG-cluster,
ArgoCD-applicatie, en dan de hele opruiming. Zesentwintig keer achter elkaar.

De langzaamste losse test is `test_sandbox_sleep_mode.py::test_sleep_then_wake_via_waker_page`
met 420s, en dat is echte wachttijd: een deployment in slaap laten vallen en weer wekken.

**Classificatie: geen fout, een eigenschap.** Maar wel de reden dat een releasecontrole op dit
cluster een halve dag kost, en de reden dat capaciteit zo snel knelt: er staan steeds meerdere
projecten tegelijk op het cluster omdat de opruiming van de vorige nog loopt.

**Voorstel, als iemand dit sneller wil:** de winst zit niet in de tests maar in het aantal
projectaanmaken. Een sessie-brede fixture die één project aanmaakt en dat de modules laten
delen waar ze niets destructiefs doen, haalt het grootste deel van die 55 minuten weg. Dat is
een eigen taak met een eigen afweging (isolatie tegen looptijd), geen releasebeslissing.

### 12. `task test-e2e` herschrijft 51 getrackte screenshots

Na de lokale e2e-suite stonden 51 bestanden onder
`operations-manager/python/tests/e2e/screenshots/lotc/` als gewijzigd in de werkboom. Dat zijn
getrackte PNG's die de visuele toetsen opnieuw wegschrijven, niet een gitignored artefactmap
(die bestaat apart, als `tests/e2e/artifacts/`).

**Classificatie: bestond al**, geen gedrag van deze release. Maar het betekent dat wie deze
suite draait een diff van 51 binaire bestanden overhoudt die niets met zijn wijziging te maken
heeft, en die per ongeluk mee kan committen. Hier teruggezet met `git checkout --`.

**Voorstel:** of de screenshots schrijven alleen bij een expliciete vlag (zoals een
`--update-snapshots`), of ze vergelijken in plaats van overschrijven. Zoals het nu is, is een
visuele toets geen toets maar een opname.

### 13. `PROJECT_REPO_PASSWORD` is nodig om de wizard te draaien en staat nergens opgeschreven

Dit is de reden dat de negen van 11a eerst als "bestond al" geclassificeerd stonden: niets in
de repo zegt dat deze variabele gezet moet zijn. Grep over `features/`, `docs/`, `workflow/`,
`instructions/`, `architecture/`, `Taskfile.yaml`, `.github/` en `pyproject.toml` geeft geen
enkele treffer. Buiten dit rapport staat hij in de appcode en het projectsjabloon, in de
rotatiescripts, in unittests en in twee uitrolconfiguratiebestanden, en in geen enkele prose.
De getrackte `.env` in `operations-manager/python` zet 31 instellingen en deze niet. De twee
plekken die hem van een waarde voorzien zijn die uitrolconfiguratie:
`bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/configmap.yaml:59`
(`plain:admin1234`) en de SOPS-secret van de odcn-overlay
(`.../overlays/odcn-production/operations-manager-env-secrets.yaml:24`). Een unittest zet hem
op precies de vormwaarde waarmee de pin hierboven groen werd
(`tests/test_create_project_api.py:50`, `plain:repo-secret`), maar die waarde is daar een
fixture en geen aanwijzing voor wie de e2e-suite draait. De rotatiescripts kennen hem als te
roteren veld (`scripts/secret_rotate.py:339` en `:341`); het commentaar "leeft in het
ZAD-envbestand; de image-default is weg (2026-09-28)" staat een paar regels hoger, op `:337`.
Dat is het dichtstbijzijnde dat op documentatie lijkt, en het zegt waar de productiewaarde
hoort, niet dat een lokale e2e-run hem nodig heeft.

Zonder hem lopen de AANMAAKpaden vast: de wizard valt om op het schema (11a), en
`generate_self_service_project_yaml` (`opi/utils/project_utils.py:360-365`), de bouwer achter
de wizardtaak en de API-create, weigert met een eigen HTTP 500 die zegt dat hij niet gezet is.
Een wijziging op een bestaand projectbestand gaat daar niet langs.

**Classificatie: omgeving**, maar het gat zit in de documentatie en niet in een machine. Elke
volgende releasecontrole loopt er opnieuw tegenaan.

**Voorstel:** hem zetten waar de lokale suite start en noemen in `workflow/prepare.md`, naast
de `playwright install`-stap die daar al staat. Een waarde die alleen een geldige vorm hoeft te
hebben (`plain:...`) is geen secret.

**Gerepareerd in `403a74fe7`.** `workflow/prepare.md` heeft een eigen kop voor deze variabele: wat er zonder hem omvalt, dat `task test-e2e` en `task test-e2e-random` zelf een default zetten, de kale pytest-regel om hem met de hand mee te geven, en waarom alleen de vorm telt. De meting staat onder 11a.

### 14. Een verwijderd project wedget de app-of-apps, en de volgende aanmaak valt daarop om (nieuw gemeten, rework r9)

Gevonden tijdens het nameten van de reparaties van bevinding 2 en 11b, en het is de
mechaniek achter bevinding 10.

De app-of-apps `user-applications` in `rig-system` staat op `automated` met `prune: true` en
`selfHeal: true`. Verwijdert OPI een project, dan krijgt de bijbehorende `AppProject` een
`deletionTimestamp` en houdt `resources-finalizer.argocd.argoproj.io` hem vast. Zolang die
verwijdering loopt blijft de sync van de app-of-apps hangen:

```
status.operationState.phase:   Running
status.operationState.message: waiting for deletion of argoproj.io/AppProject/zvlam-5gr-zvlam-5gr
status.sync.status:            OutOfSync
```

En zolang die sync hangt wordt de `Application` van een NIEUW project niet aangebracht, dus
loopt de aanmaaktaak van dat project in zijn eigen wachttijd dood:

```
opi.manager.argo_manager - ERROR - Timeout waiting for ArgoCD application 'invit-3k0-project'
to be created after 360s (gepushte commit: 5140207ed..., user-applications revisie: onbekend)
```

De test ziet daarvan alleen het staartstuk: `assert_aanmaak_geslaagd` wacht 240s op de
detaillink van de voortgangspagina, die er bij een mislukte aanmaak nooit komt, en
`opruimen_als_het_misgaat` haalt het project daarna weg.

**Twee keer gemeten, met een naam erbij:** `zvlam-5gr-zvlam-5gr` (deletionTimestamp
08:03:12) en `vlam5-xlb-vlam5-xlb` (08:11:26). De tweede was binnen 71s weg, dus de wedge is
voorbijgaand en geen blijvende blokkade. Maar een suite die module na module een project
aanmaakt en opruimt zet die verwijderingen achter elkaar, en dan staat de wedge lang genoeg
om de 360s van een gelijktijdige aanmaak op te eten.

**Wat het kostte:** in een gang met de drie modules achter elkaar
(`test_sandbox_all_services.py`, `test_sandbox_invite_ui.py`, `test_sandbox_vlam.py`) gaf dat
**11 van de 17 errors**. De twee modules die als eerste liepen kwamen door; alles na de eerste
opruiming viel om. Het bewijs is dezelfde fixture tegen dezelfde image: `vlam_project` gaf een
**ERROR na 246,9s** in die gang, en **slaagde in 47s** op een rustig cluster. Los gedraaid:
`test_sandbox_vlam.py` **7 passed**.

**Classificatie: omgeving, en niet nieuw in deze release.** Het is een eigenschap van de
app-of-apps met `prune` plus de ArgoCD-finalizer, niet van de appcode van deze release. Het
raakt de productie-uitrol niet, want daar wordt niet in reeksen aangemaakt en opgeruimd.

**Nog iets wat het lezen bemoeilijkt:** ArgoCD antwoordt op een `GET
/api/v1/applications/<naam>` voor een applicatie die niet bestaat met
`{"error":"permission denied","code":7}` en niet met een 404. Het log van OPI staat daardoor
tijdens die wachtlus vol `permission denied`, wat naar een rechtenprobleem wijst terwijl het
"bestaat nog niet" betekent.

**Voorstel, geen releaseblokkade:** laat de sandbox-suite tussen modules wachten tot
`user-applications` weer `Synced/Healthy` is in plaats van op de klok, of laat de aanmaaktaak
in zijn foutmelding de toestand van de app-of-apps meenemen. Die laatste is de goedkoopste:
de melding zegt nu "timeout na 360s" terwijl de oorzaak een wedge op een andere resource is,
en dat is een eigen kaartje.

### 15. Een e2e-test die nooit kon slagen, verborgen achter bevinding 2 (nieuw gemeten, rework r9)

`tests/e2e/test_sandbox_invite_ui.py::test_configure_via_api_writes_invite` roept
`read_api_key_with_retry` aan zonder hem te importeren:

```
E       NameError: name 'read_api_key_with_retry' is not defined
tests/e2e/test_sandbox_invite_ui.py:272
```

De helper bestaat wel, in `tests/e2e/helpers/lifecycle.py:461`; de importregel van die module
(`:34`) haalt alleen `RUNNABLE_IMAGE` en `naam_en_sleutel_na_de_wizard` binnen.

**Classificatie: bestond al, en was onmeetbaar.** `git diff 78074173c 8d577a0f2` op dit
testbestand is leeg, dus de kale aanroep stond identiek op de geteste commit, daar op regel
`:264`, met dezelfde importregel. De test kon dus nooit slagen. Hij is alleen nooit zo ver
gekomen: de module-fixture `invite_project` viel altijd eerder om op de keycloak-stap van
bevinding 2, dus deze vijf tests waren alle vijf een ERROR in de opzet en geen van hen is ooit
uitgevoerd. Het repareren van bevinding 2 legde hem bloot.

Dat is ook het punt dat deze bevinding maakt: een fixture die omvalt verbergt elke fout in de
tests erachter, en de acht errors van bevinding 2 telden daarom als EEN oorzaak terwijl er
twee onder zaten.

**Gerepareerd in `628b18f79`** (buiten de acht, want een NameError is kapot): de import is
aangevuld. Nagemeten op de sandbox, de module los op een rustig cluster: **5 passed in
133,77s**, waar dezelfde module ervoor 4 passed en 1 failed gaf.

## Overgeslagen tests, met de reden

Alle overgeslagen tests slaan zichzelf over op een ontbrekend gereedschap of een ontbrekende
sleutel. Geen enkele slaat over op iets dat deze release veranderde.

| stap | aantal | reden |
|---|---|---|
| 2, unittests | 9 | `meet de echte skopeo`: `tests/test_skopeo_connector.py::TestARealSkopeoFollowsTheRegistryInward`, vraagt de skopeo-binary |
| 2, unittests | 1 | `vraagt htpasswd` (`test_secret_edit.py:175`) |
| 2, unittests | 1 | `vraagt de echte AGE-sleutel` (`test_secret_rotate.py:190`) |
| 2 en 3 | 1 | `test_real_project_files_migrate_and_validate` zonder `RIG_PROJECTS_DIR` (`test_upgrade_safety_replay.py:318`). Slaat in beide stappen over: hij draagt geen `upgrade_safety`-marker en zit dus ook in de run van stap 2. Afgedekt door stap 3b, die de echte bestanden uit de repo kloont |
| 5 | 1 | "Invite key not configured in test app" |
| 7 | 15 | `zad-cli niet gevonden (zad of zadctl op het PATH, of ZAD_CLI...)`: `test_sandbox_zad_cli.py` (5), `test_sandbox_kloon.py` (5) en `test_sandbox_registry_pull.py` (3) in hun geheel, want die dragen de skip als module-level `pytestmark` met `skipif(cli_pad() is None)` (`test_sandbox_zad_cli.py:49`, `test_sandbox_kloon.py:57`, `test_sandbox_registry_pull.py:76`), plus een van `test_sandbox_foutmeldingen.py` (alleen `test_de_cli_maakt_van_de_envelop_een_leesbare_regel` roept `skip_zonder_cli`) en een van `test_sandbox_speelruimte.py` |

**Classificatie: omgeving.** Het plan noemde al dat een ontbrekende AGE-sleutel omgeving is en
geen regressie; datzelfde geldt voor skopeo, htpasswd en de zad-cli.

**Voorstel:** de 15 zad-cli-tests zijn de grootste groep en raken een onderdeel waar de release
wel wijzigingen in heeft. Wie ze wil meten, moet de zad-cli in de workercontainer hebben (of
`ZAD_CLI` zetten). Dat is een vraag voor DistributedClaude over de container, niet voor deze
repo.

## Wat niet in deze controle zat

- **De infra van deze release op een cluster.** Zie bevinding 8: dat kon niet zonder het
  gedeelde cluster te slopen. De Keycloak-image die productie krijgt is wel nagekeken op
  bestaan: de odcn-render komt uit op
  `rcr.rijksapps.nl/ghcr-rig/rijksictgilde/zad/keycloak-zad:2026.10.13.1948-72684cb4`, en de
  bron daarvan op ghcr.io antwoordt met een geldig manifest (HTTP 200). Of de spiegel
  `rcr.rijksapps.nl` hem al heeft, is van hieruit niet te zien.
- **Federatieve SSO.** Zie bevinding 7.
- **De vier punt14-tests.** Zie bevinding 3: ze passen niet op deze node en hun timeouts zijn
  bij elkaar 5,5 uur.
- **Een nieuwe OPI-image voor productie.** De odcn-overlay pint nog
  `operations-manager:2026.09.28.2215-a5fef305-dirty`, de image van de vorige release. Het
  bouwen en pinnen van de oktober-image is een eigen stap die na deze controle komt.

## Oordeel

**Releasebaar met kanttekeningen.**

De grond daarvoor, kort: van de 20 rode tests in deze controle is er **een** nieuw in deze
release, en die raakt geen draaiende code. Negen zijn omgeving: de wizardtests van bevinding
11a vallen om op een variabele die in deze container niet gezet is, en met die variabele erbij
zijn ze groen. De overige tien zijn gemeten als bestaand op de commit achter de huidige
productie-image (8 uit bevinding 2, 2 uit 11b). De vijf wisselvallige tests uit bevinding 10
staan niet in die 20: in de gang die de staptabel noteert waren ze groen.

De applicatie van deze release heeft op de sandbox gedraaid en deed wat ze moet doen: de pod
kwam op zonder restarts, de migraties liepen door, het portaal laadt, de inlog werkt van
`/auth/login` tot een gevuld dashboard, 77 sandbox-e2e-tests zijn groen en de reallife-suite is
groen. De 47 echte productieprojectbestanden komen alle door migrate en validate, en dat is de
toets die zegt dat geen bestaand project stilvalt op zijn volgende reconcile.

De kanttekeningen, in de orde waarin ik ze zou wegen:

1. **De infra van deze release heeft nog nooit gedraaid.** De sandbox toetste de app op infra
   van 18 dagen oud. De nieuwe ZAD-Keycloak en de mailrelay gaan ongetoetst naar productie,
   en de enige taak die dat had kunnen oplossen maakt het gedeelde cluster kapot (bevinding 8).
   Dit is de enige kanttekening die ik vóór de uitrol zou willen zien weggenomen, en de weg
   ernaartoe is of de `--exclude` op `sandbox:sync`, of een sandbox die van `task sandbox:setup`
   af op deze main is opgebouwd.
2. **Eén nieuwe rode test**, `test_sops_rotation_round`, op te lossen door vier regels uit
   `COVERAGE_EXCEPTIONS` in `scripts/sops_rotation.py` te halen (bevinding 1; de assertie loopt
   in een lus en meldt er maar een). Hij blokkeert de uitrol niet, maar wel de volgende
   SOPS-sleutelrotatie.
3. **Tien bestaande rode e2e-tests**, in twee groepen: de keycloak-template die verplicht werd
   zonder dat de helpers meegingen (8, bevinding 2) en twee VLAM-drifts (bevinding 11b).
   Daarnaast negen rode wizardtests die de omgeving van de run meten en niet de code
   (bevinding 11a). Deze release maakt geen van beide erger, maar samen maken ze de
   browsersuites nu onbruikbaar als poort: een volgende releasecontrole moet opnieuw uitzoeken
   welke roden al bestonden en welke de container misten.
4. **De browsersuites staan in geen enkele automatische poort** (`ci.yml:69` sluit `e2e` uit).
   Dat is de reden dat punt 3 kon ontstaan, en de goedkoopste structurele reparatie van de drie:
   de lokale suite heeft geen cluster nodig en duurt 18 minuten. Hij vraagt in CI wel
   `PROJECT_REPO_PASSWORD`, anders is die poort meteen rood op de negen van bevinding 11a. Dat
   die eis nergens staat opgeschreven is bevinding 13.
5. **`task test-e2e-sandbox` loopt zoals hij er staat niet af**: punt14 vult de node en mag
   5,5 uur (bevinding 3), en een vastloper raakt geen timeout (bevinding 5). De meting hierboven
   is gedaan zonder punt14 en zonder die ene test, en dat moet je weten om het getal te lezen.
6. **Federatieve SSO is op de sandbox niet gemeten**, omdat SSO daar bewust uit staat
   (bevinding 7). De inlogketen zelf is wel gemeten, in stap 4 en op het portaal.

Wat ik **niet** als kanttekening meeneem, omdat het gemeten omgeving is en geen eigenschap van
de release: de Suspended ArgoCD-applicatie (bevinding 4), de 28 overgeslagen tests, en de oude
resten op de sandbox (bevinding 9), waarvan ik er elf heb opgeruimd.
