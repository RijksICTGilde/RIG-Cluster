# PVC marked-for-deletion blokkeert de render, en niemand ziet waarom

## Status: plan, nog niet geïmplementeerd

Eén incident, vier bevindingen. A is de oorzaak, B is waarom het uren puzzelen was,
C maakt de forensiek onnodig zwaar, D is context die een aanname onderuit haalt.

## Samenvatting

* **A.** Wordt een `persistent-storage` service verwijderd, dan wordt het PVC-manifest
  hernoemd naar `*.marked-for-deletion.yaml` zodat ArgoCD het volume in leven houdt.
  Komt daarna dezelfde PVC terug, dan schrijft OPI `X.yaml` er naast en staan er twee
  bestanden met dezelfde resource-identiteit in `kustomization.yaml`. Kustomize weigert
  dan de hele deployment te renderen.
* **B.** Die renderfout ontstaat in de ArgoCD CMP en komt nergens terug: niet in de logs
  van OPI, niet in de deploy-taak, niet op de projectpagina. De deploy loopt 300 seconden
  door en eindigt met "Time-out, controleer de logs van het component", voor een component
  dat nooit gerenderd is. De echte melding staat alleen in de ArgoCD UI.
* **C.** De modal-editor herschikt bij elke save de veldvolgorde binnen componenten
  (`reference`, `image`, `services`, `resources` wisselen van plek). Dat blaast de diff op
  tot veel meer dan de feitelijke wijziging, en maakt precies dit soort forensiek lastig.
* **D.** De aanname "na 7 dagen wordt het marked-bestand alsnog opgeruimd" gaat niet op:
  de reconciliation-job wordt nergens periodiek aangeroepen.

Handmatig opgelost is het al: de `*.marked-for-deletion.yaml` weggegooid. De rij in de
`marked_for_deletion` tabel van productie staat er nog, zie
[Productie-remediatie](#productie-remediatie).

## Bevinding A: dubbele PVC-resource na herstel

### Wat kustomize precies zegt

Nagebouwd met kustomize v5.4.3, twee bestanden met dezelfde PVC:

```
Error: accumulating resources: accumulation err='merging resources from
'b.marked-for-deletion.yaml': may not add resource with an already registered id:
PersistentVolumeClaim.v1.[noGrp]/web-data.rig-prd-x': must build at directory: ...
```

### Hoe marked-for-deletion werkt

Bij het verwijderen van de service (`PVCManager.handle_service_removal`,
`opi/manager/pvc_manager.py:333`):

1. `<component>-<storage>-pvc.yaml` wordt hernoemd naar
   `<component>-<storage>-pvc.marked-for-deletion.yaml` (`pvc_manager.py:440-448`).
2. Er komt een rij in `marked_for_deletion` met `resource_type="pvc"` en `resource_name`
   = de **bestandsnaam** van de marked-versie, plus metadata met `deployment_path`,
   `namespace`, `component`, `storage_name`, `original_filename` (`pvc_manager.py:451-465`).
3. Het bestand blijft bewust in de kustomize-map staan: `collect_manifest_files` pakt elk
   `*.yaml` op (`opi/generation/manifests.py:274-279`) en `create_kustomization_files`
   filtert alleen zichzelf en `decrypt-sops.yaml` weg (`manifests.py:386-394`). Zo blijft de
   PVC voor ArgoCD een gewenste resource en wordt hij niet geprunet. Dat is het hele idee.
4. De prune van verouderde componentmanifesten slaat marked-bestanden expliciet over
   (`opi/manager/project_manager.py:276-281`), anders sneuvelt het volume alsnog direct.

Opruimen gebeurt via `_purge_pvc` (`opi/jobs/reconciliation.py:711`): bestand weg,
`kustomization.yaml` opnieuw genereren, committen, ArgoCD prunet de PVC.

### Waarom herstel de tweeling laat staan

`create_pvc_manifests_for_component` (`pvc_manager.py:134`) schrijft het manifest altijd
onder de generatie-naam (`pvc_manager.py:317-328`) en weet niets van een marked-tweeling.
Geen enkele andere plek ruimt hem op:

* `delete_old_pvc_manifests` (`pvc_manager.py:29`) ruimt alleen oudere **generaties** op en
  matcht op regexen die de marked-suffix niet dekken (`pvc_manager.py:76-77`). Bij een
  generatiewissel blijft de marked-versie van generatie 0 dus ook liggen.
* `_select_obsolete_component_manifests` slaat marked-bestanden bewust over (zie boven).
* De reconciliation-stap "resource is terug in de expected set, dus unmark"
  (`reconciliation.py:365-381`) kan pvc-marks niet zien: `_build_expected_resources` heeft
  alleen keys voor `postgresql_database`, `postgresql_user`, `minio_bucket`, `minio_user`,
  `minio_policy`, `backup_data` (`reconciliation.py:111-118`). Geen `pvc`.

Gunstig voor de fix: de bestandsnaam is 1-op-1 gekoppeld aan de resource-identiteit
(`<component>-<storage>-pvc[-vN].yaml` bevat exact PVC
`<deployment>-<component>-<storage>[-vN]`). Een conflict kan dus alleen bestaan tussen
`X.yaml` en `X.marked-for-deletion.yaml`, en matchen op exacte basisnaam is zowel
voldoende als veilig.

## Bevinding B: CMP- en renderfouten zijn onzichtbaar

Bij een manifest-generatiefout zet ArgoCD `status.sync.status` op `Unknown` en plaatst de
melding in `status.conditions[]` met `type: ComparisonError`. Er start geen sync-operatie,
dus er is ook geen `operationState.phase == "Failed"`. Precies dat veld is wat OPI leest.

### B1. Tijdens de deploy

`ArgoManager.wait_for_application_synced` (`opi/manager/argo_manager.py:988`) beoordeelt
terminale toestanden op:

* `status.operationState.phase in ("Failed", "Error")` (`argo_manager.py:1073-1081`)
* `status.health.status == "Degraded"` (`argo_manager.py:1096-1099`)

`status.conditions` wordt niet gelezen. De app valt dus in de "nog niet klaar, opnieuw
pollen"-tak (`argo_manager.py:1101-1107`) tot de 300s-timeout in
`project_manager.py:2741-2774`, met als eindresultaat "Time-out na 300s ... Controleer de
logs van het component". In de logs staat alleen `sync=Unknown, health=Missing` op
DEBUG-niveau. De infrastructuur-variant logt condities wel, maar uitsluitend als health
`Degraded` is (`argo_manager.py:943-950`), dus ook daar glipt een ComparisonError door.

OPI doet daarnaast nergens een eigen render-validatie: er staat geen enkele
`kustomize build` in de codebase, dus een kapotte kustomization wordt gewoon gecommit en
gepusht zonder enig signaal.

### B2. In de projectstatus

`gather_deployment_errors` leest `status.conditions[]` al wel
(`opi/services/deployment_diagnostics.py:203-207`), en het statuskaartje rendert die lijst
onafhankelijk van health (`opi/templates/project-details/_argocd-deployment-card.html.j2:160-172`).
Maar de aanroep zit achter een health-guard: `if app_health != "Healthy"`
(`opi/web/router.py:1493-1502`). Een deployment die al draaide blijft door de mislukte
vergelijking op zijn laatste bekende health staan, dus die guard kan de fout wegfilteren
op precies het moment dat je hem nodig hebt. Wat je dan ziet is een `sync`-badge met
`Unknown` in neutraal grijs (`_argocd-deployment-card.html.j2:104-114`), wat leest als
"nog even geduld".

Het dashboard is nog blinder: dat leest per deployment alleen `health.status` en negeert
sync en condities volledig (`opi/web/router.py:876-905`), dus het project blijft daar
groen.

### B3. Is de CMP-fout überhaupt te achterhalen?

Ja, en dat is het goede nieuws: de melding wordt niet weggegooid, wij vragen er alleen
nergens om. Drie kanalen, van bruikbaar naar bedroevend.

**1. Het Application-object zelf.** `status.conditions[]` met `type: ComparisonError` en een
message in de vorm `Failed to load target state: failed to generate manifests in
'<pad>': rpc error: code = Unknown desc = ... exit status 1: <stderr van de plugin>`. De
plugin-stderr zit daar dus in. Ons CMP-script draait met `set -e` en `set -o pipefail` en
stuurt kustomize-stdout naar een bestand (`bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml:297`),
dus de kustomize-fout gaat naar stderr en komt via de CMP-server terug. Wij lezen dat veld
al in `gather_deployment_errors` (`deployment_diagnostics.py:203-207`), alleen achter een
health-guard, en in de deploy-wachtlus helemaal niet. Dit is dus vooral een kwestie van
kijken (stappen 3 en 5).

**2. Actief opvragen: `GET /api/v1/applications/{app}/manifests?revision=<sha>`.** Dat is de
API achter `argocd app manifests`. Bij een generatiefout geeft die de fout terug in plaats
van manifesten, en dat kan direct na onze eigen push, op de exacte commit die we net
gepusht hebben. Geen wachten tot de controller toevallig reconcilieert, geen afhankelijkheid
van health of van een conditie die er nog niet staat. Dit is het kanaal dat "niemand wist
wat er aan de hand was" structureel oplost, zie stap 4.

**3. De sidecar-logs.** `kubectl logs -n <ns> deploy/argocd-repo-server -c cmp-server`. Hier
staat de ruwe stderr, inclusief alle `DEBUG:`-regels die het script zelf produceert
(`.cmp-env` dump, helm dependency output, per-folder debug). Dit is het kanaal waar je nu
op aangewezen bent, en het is precies zo onbruikbaar als je zegt.

Aandachtspunt bij kanaal 1 en 2, en de reden dat kanaal 3 nu de praktijk is: ArgoCD kapt
lange plugin-fouten af, en ons script schrijft veel DEBUG naar dezelfde stderr. De echte
kustomize-regel kan daardoor verdwijnen in of achter de ruis. Daarom stap 6: de foutregel
expliciet als laatste en als enige nadruk op stderr zetten. Hoeveel er precies overblijft,
weten we pas na de sandbox-reproductie (stap 0); ik wil dat meten, niet aannemen.

## Bevinding C: veldvolgorde-churn bij elke modal-save

De dumper is niet de schuldige: `_create_yaml_writer` gebruikt ruamel round-trip zonder
`sort_keys` (`opi/utils/yaml_util.py:18-26`), dus de volgorde uit het bestand blijft staan
zolang niemand sleutels weghaalt en opnieuw toevoegt. Dat laatste gebeurt in de
sequence-merge van de editables-processor:

1. `_process_sequence_json` bepaalt welke velden de sectie beheert, verwijdert die met
   `_prune_paths` uit een kopie van het originele item, en merget de ingezonden waarden
   er daarna met `_deep_merge` weer over (`opi/forms/editables/processor.py:537-553`).
   In Python (en in een ruamel `CommentedMap`) plaatst een `pop` gevolgd door een nieuwe
   assignment de sleutel **achteraan**. Resultaat: de niet-beheerde sleutels houden hun
   plek, alle beheerde sleutels schuiven naar het eind, in de volgorde van het formulier.
   Elke sectie beheert een andere set, dus elke save roteert een andere groep velden.
2. `_process_nested_sequence_json` vervangt de hele geneste lijst door de ingezonden versie
   (`processor.py:679`), dus daar bepaalt de formuliervolgorde de uitkomst.
3. Bij `virtualize` wordt de virtuele sleutel uit het item gepopt en de echte
   (bijvoorbeeld `services`) later opnieuw gezet (`processor.py:556-567`, `688-699`), wat
   die sleutel eveneens naar achteren verplaatst.

Groepen (`_process_group_json`) hebben dit niet: die schrijven via `_write_field` in een
bestaande dict, en een assignment op een bestaande sleutel behoudt de positie.
`merge_service_lists` is ook onschuldig: die respecteert de bestaande lijstvolgorde
(`opi/forms/wizard/services_merge.py:66-84`).

## Bevinding D: de reconciliation-job draait nooit

`DELETION_GRACE_PERIOD_DAYS = 7` bestaat (`opi/core/config.py:288`), maar `reconcile()` en
`cleanup_project()` worden alleen aangeroepen door de admin-endpoints
`POST /api/v2/admin/reconciliation/trigger` en `POST /api/v2/admin/cleanup/trigger`
(`opi/api/admin_router.py:95-140`). Er is wel een scheduler-patroon in de lifespan (backup,
resource-tuning, logwatcher, `opi/server.py:162-239`), maar geen reconciliation-scheduler.

Praktisch: elk marked PVC-manifest blijft permanent staan, dus het conflictvenster van
bevinding A is niet 7 dagen maar oneindig, en oude marks lopen op. Ik stel voor dit als
losse beslissing te behandelen (wel of geen scheduler, en met welke default) en niet
stilletjes mee te nemen.

## Plan

### 1. A: marked-tweeling opruimen bij herstel

Private helper in `pvc_manager.py`, naast de bestaande suffix-constante:

```python
async def _clear_marked_twin(self, full_output_dir: str, manifest_filename: str, cluster: str) -> None
```

* Pad `<full_output_dir>/<basename>.marked-for-deletion.yaml`; bestaat niet, dan niets doen.
* Bestaat wel: `os.remove()` plus `logger.info` met beide bestandsnamen en de reden
  (PVC opnieuw aangemaakt, uitgestelde verwijdering vervalt).
* Daarna best-effort `MarkedForDeletionService.unmark_resource("pvc", marked_filename, cluster)`,
  met dezelfde DB-pool-guard als `project_manager.py:2497-2505`: geen pool, dan
  `logger.warning` en door. Het bestand weghalen repareert de render en is de harde eis, de
  rij is administratie.
* Aanroepen vanuit `create_pvc_manifests_for_component`, zodra `pvc_manifest_name` bekend is
  en vóór `create_manifest_file` (`pvc_manager.py:317-326`).

Het live volume blijft intact: ArgoCD adopteert de bestaande PVC weer via het
teruggeplaatste manifest, er wordt niets geprunet.

**Verify:** unit-test die een marked-tweeling neerzet, `create_pvc_manifests_for_component`
draait en aantoont dat `X.yaml` bestaat, `X.marked-for-deletion.yaml` weg is en
`unmark_resource` met de marked bestandsnaam plus juiste cluster is aangeroepen. Plus een
test dat een marked-versie van een **andere** generatie blijft staan.

### 2. B: dubbele resource-identiteit blokkeren vóór de commit

Check in `create_kustomization_files`, na het exclude-filter (`manifests.py:393-394`) en
vóór het schrijven:

* Parse elk bestand uit `regular_files` dat op schijf staat (SOPS-bestanden overslaan, die
  zijn versleuteld en gaan via de generator).
* Bouw per YAML-document de sleutel `(apiVersion, kind, metadata.namespace, metadata.name)`.
* Bij een dubbele sleutel: `RuntimeError` met beide bestandsnamen, de resource-identiteit en
  de map. Dat is de melding die nu ontbreekt, en hij valt vóór commit en push.

Dit vangt de hele klasse "kustomize weigert wegens dubbele id" af, niet alleen het
marked-geval. Helm- en helmfile-deployments blijven buiten bereik (die renderen pas in de
CMP), daarvoor zijn stap 3 en 4 het net.

**Verify:** test met twee bestanden met dezelfde PVC-identiteit, verwacht `RuntimeError` met
beide namen erin; test dat dezelfde naam met een ander `kind` of andere namespace geen fout
geeft.

### 3. B1: ArgoCD-condities lezen tijdens de deploy

In `wait_for_application_synced`, binnen de `status_is_fresh`-tak (`argo_manager.py:1071`):

* Lees `status.conditions`; bij `type` in `("ComparisonError", "InvalidSpecError", "SyncError",
  "UnknownError")` de `message` op ERROR loggen en een `RuntimeError` gooien met die message.
  Die komt via `project_manager.py:2775-2779` in `sync_failures` en in de progress-subtask,
  dus de gebruiker leest de echte oorzaak in plaats van een timeout.
* Zelfde check in `wait_for_infrastructure_ready` (`argo_manager.py:918`), nu los van de
  `Degraded`-voorwaarde.
* De `refreshed_after`-guard blijft leidend: condities alleen beoordelen als de status ná
  onze refresh is gereconcilieerd, anders krijgen we valse fouten van een vorige ronde.
* Goedkope extra: `refresh_application` heeft de volledige response al in handen
  (`opi/connectors/argo.py:497-503`) en kan een aanwezige ComparisonError direct op WARNING
  loggen. Dan staat de melding in de OPI-logs op het moment dat hij ontstaat.

**Verify:** unit-test met gemockte `get_application_status` die `sync=Unknown`,
`health=Missing` en een `ComparisonError`-conditie teruggeeft; verwacht een `RuntimeError`
met de kustomize-melding erin en géén 300s wachtlus. Plus een test dat een stale conditie
(`reconciledAt <= refreshed_after`) niet terminaal is.

### 4. B3: de renderfout actief ophalen na de push

Nieuwe methode op `ArgoConnector`:

```python
async def get_application_manifests(self, app_name: str, revision: str | None = None) -> tuple[bool, str]
```

`GET {base}/api/v1/applications/{app}/manifests` met optioneel `?revision=<sha>`. Bij 200 is
de render gelukt (we gooien de manifesten weg, alleen het slagen telt); bij een foutstatus
geven we de body terug, want daar staat de generatiefout inclusief plugin-stderr in.

Aanroepen direct na de commit+push van de deployment-manifesten, vóór de wachtlus, met de
sha die we net gepusht hebben. Mislukt de render, dan faalt de deploy meteen met die
melding in de taakfout, in plaats van na 300 seconden met een timeout. Dit is ook het enige
kanaal dat helm- en helmfile-deployments dekt, want die renderen pas in de CMP.

Twee dingen om bij de implementatie te bewaken: de call moet niet blokkeren op een revisie
die de repo-server nog niet gezien heeft (eerst refresh, of retry op "revision not found"),
en een mislukte call mag de deploy niet tegenhouden als de oorzaak niet de render is
(netwerkfout, 401): dan alleen loggen en doorgaan naar de wachtlus.

**Verify:** unit-test met gemockte HTTP-response (200 en een 500 met een kustomize-fout in de
body); plus in de sandbox één keer echt uitvoeren tegen een kapotte revisie en de body
vastleggen.

### 5. B2: renderfout zichtbaar maken in de deployment-status

* Haal het ophalen van app-level condities uit de health-guard in
  `_fetch_argocd_deployment_status` (`opi/web/router.py:1493-1502`). Condities en
  `operationState` zitten al in `status_data`, dus dit kost geen extra API-call; de dure
  bronnen (resource tree, namespace events) blijven achter de guard staan. Praktisch:
  `gather_deployment_errors` splitsen in een goedkoop deel op `status_data` (altijd) en een
  duur deel (alleen bij niet-Healthy), of in de router een aparte
  `conditions_to_errors(status_data)` aanroepen en samenvoegen.
* Geef een ComparisonError een eigen, leesbare regel in plaats van de ruwe conditienaam:
  resource "Configuratiefout (kustomize/CMP)" met de melding eronder, via
  `_enrich_argocd_error` in `opi/services/event_interpreter.py`.
* Maak `sync == Unknown` in het statuskaartje zichtbaar rood in plaats van neutraal grijs
  (`_argocd-deployment-card.html.j2:104-114`), zodat "kan niet vergelijken" niet leest als
  "nog even geduld".
* Nog te beslissen: het dashboard (`opi/web/router.py:876-905`) leest alleen health. Dat kan
  meeliften door een error-conditie als "Degraded" te laten meewegen. Kleine wijziging, maar
  raakt de projecttegel van iedereen. Ik doe dit alleen als je het wil.

**Verify:** sandbox-reproductie (stap 0 hieronder) plus een test op
`_fetch_argocd_deployment_status` met een `Healthy` health én een ComparisonError-conditie:
de fout moet in `errors` terechtkomen.

**Stap 0, eerst uitvoeren:** in de sandbox een duplicaat forceren in één deployment-map en
vastleggen: `kubectl get application <app> -o json` (welke waarden staan er in
`health.status`, `sync.status`, `conditions[]`, en hoeveel van de plugin-stderr blijft er
over), de response van het manifests-endpoint uit stap 4, en de bijbehorende
`cmp-server`-logregels. Daarna terugdraaien. Dit is de meting die stap 3, 4, 5 en 6
onderbouwt, in plaats van dat we het gedrag aannemen.

### 6. B3: de foutregel als laatste op stderr in het CMP-script

In `bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml` de kustomize-aanroep
(`:297`) omhullen zodat een mislukking eindigt met één duidelijke, laatste stderr-regel:
prefix als `KUSTOMIZE BUILD FAILED in <folder>:` gevolgd door de stderr van kustomize zelf.
Nu is de fout één regel tussen tientallen `DEBUG:`-regels (`.cmp-env`-dump, helm dependency
output, per-folder debug), en juist bij afkapping door ArgoCD is het de laatste output die
overblijft.

Tegelijk de `DEBUG:`-ruis terugbrengen tot wat je bij een fout echt nodig hebt. De
`.cmp-env`-dump verdient extra aandacht: die kan configuratiewaarden in de logs zetten.

Dit raakt de ArgoCD-installatie, dus het gaat via git naar ArgoCD en niet met de hand, en de
sandbox is de plek om het te verifiëren.

**Verify:** in de sandbox met de kapotte revisie uit stap 0: de conditie-message eindigt met
de `KUSTOMIZE BUILD FAILED`-regel en de kustomize-melding is leesbaar aanwezig.

### 7. C: veldvolgorde stabiel houden

Kleine helper in `opi/forms/editables/processor.py`:

```python
def _reorder_like(original: dict[str, Any], merged: dict[str, Any]) -> dict[str, Any]
```

Sleutels uit `original` eerst, in hun oorspronkelijke volgorde, daarna de echt nieuwe
sleutels in de volgorde waarin ze binnenkwamen. Recursief voor geneste dicts. Toepassen op:

* de merge-uitkomst per item in `_process_sequence_json` (`processor.py:546-553`),
* de vervanging in `_process_nested_sequence_json` (`processor.py:679`), met het
  pre-edit item als referentie,
* na het herzetten van een gevirtualiseerde sleutel (`processor.py:556-567`, `688-699`).

Werkt op een `CommentedMap` alleen als je in de bestaande map herordent in plaats van een
nieuwe dict te bouwen (anders verlies je commentaar en anchors): sleutels in de gewenste
volgorde één keer `move_to_end`-achtig herplaatsen. Dat wordt de kern van de
implementatie, en verdient een expliciete test op een round-trip geladen bestand.

**Verify:** test die een project-YAML met een bekende sleutelvolgorde laadt, één veld via
de processor wijzigt, opnieuw dumpt en de volledige dump vergelijkt: exact één gewijzigde
regel. Plus een end-to-end variant via de modal-flow op een component met
`reference`/`image`/`services`/`resources`.

### 8. Regressietest op het samenspel

Eén test die het incident naspeelt: service verwijderen (manifest hernoemd, mark gezet),
daarna dezelfde storage opnieuw aanmaken, en aantonen dat de map exact één PVC-manifest
houdt en dat de duplicaat-check niet afgaat.

**Verify:** `uv run pytest tests/test_marked_for_deletion.py tests/test_argo_manager.py
tests/test_component_manifest_prune.py tests/forms/ -q` plus de nieuwe tests groen, daarna
`uv run ruff check . --fix`, `uv run ruff format .`, `uv run pyright`.

### 9. Documentatie

`features/yaml-diff-driven-deletion.md` bijwerken met het herstelgedrag (marked-tweeling
vervalt zodra dezelfde resource terugkomt), zodat de levensloop van een marked PVC ergens
volledig staat.

## Productie-remediatie

Het bestand is met de hand weggehaald, de rij in `marked_for_deletion` staat er nog. Effect
is beperkt maar niet nul: de admin-lijst meldt een live volume als "marked". Zou de
reconciliation ooit draaien, dan ziet `_purge_pvc` dat het bestand ontbreekt en ruimt de rij
zelf op (`reconciliation.py:774-781`), dus er gaat niets stuk. Door bevinding D gebeurt dat
alleen niet vanzelf.

Opruimen kan nu al zonder deploy, via de bestaande admin-API:

```bash
# 1. Mark zoeken (resource_type=pvc, resource_name is de marked bestandsnaam)
curl -s -H "X-API-Key: $ADMIN_API_KEY" \
  "https://<opi-host>/api/v2/admin/marked-for-deletion?project_name=<project>" | jq

# 2. Mark verwijderen zonder de resource te raken
curl -X DELETE -H "X-API-Key: $ADMIN_API_KEY" \
  "https://<opi-host>/api/v2/admin/marked-for-deletion/<mark_id>"
```

Aanrader bij dezelfde gelegenheid: de lijst zonder `project_name` opvragen en kijken of er
meer pvc-marks zijn waarvan het bestand of de deployment al niet meer bestaat. Er is nog
nooit iets automatisch opgeruimd.

## Afgewogen en niet gekozen

* **Lokaal `kustomize build` als validatie.** Zou de meeste zekerheid geven, maar de map
  bevat op dat moment nog `.to-sops.yaml`-bestanden en de echte render leunt op de
  SOPS-CMP-sidecar en op helm/helmfile. Een lokale build valideert dus een andere boom dan
  ArgoCD bouwt, met vals-negatieven en vals-positieven. Stap 2 dekt de fout af die
  daadwerkelijk voorkomt, stap 3 en 4 vangen de rest op waar hij ontstaat.
* **`pvc` toevoegen aan `_build_expected_resources`.** Dan zou de unmark-tak van de
  reconciliation pvc-marks herkennen. Vergt het reconstrueren van marked bestandsnamen uit
  de project-YAML in een functie die verder met echte resource-namen werkt, en lost bevinding
  A niet op zolang de job niet draait (bevinding D). Stap 1 grijpt op het juiste moment in.
* **De duplicaat stil oplossen** door de marked-versie niet in `resources` op te nemen als
  de gewone variant bestaat. Verbergt dat administratie en schijf uit elkaar lopen en laat
  het bestand in git achter. Falen met een duidelijke melding, plus opruimen bij de bron,
  is beter.
* **Een vaste canonieke sleutelvolgorde in de dumper** (bevinding C via `dump_yaml_to_string`).
  Zou álle schrijvers normaliseren, ook de migraties en de auto-tuner, maar kost per project
  één grote eenmalige herordening-diff en een sleutelvolgorde-tabel die onderhouden moet
  worden. De volgorde uit het bestand respecteren is kleiner en geeft hetzelfde resultaat:
  diff = de wijziging.
