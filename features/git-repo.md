# GitRepo: één beheerde repo per remote

**Status**: Implemented
**Date**: 2026-11-04
**Related**: [ProjectStore](project-store.md), [Git push-serialisatie per repo](git-push-serialization.md),
[Rollout-metingen](rollout-metingen.md), [Ondertekende GitOps-commits](ondertekende-gitops-commits.md)

## Wat het is

`opi/connectors/git_repo.py` is de enige weg waarlangs OPI met de **deployments-repo** en de
**argo-applications repo** praat. Eén `GitRepo` per (remote zonder credentials, branch),
procesbreed: een bare blobless kopie met historie die blijft staan, lezen per revisie, en
schrijven door een commit uit git-objecten te bouwen en die naar de branch te pushen.

Het is de git-helft van de [ProjectStore](project-store.md), generiek gemaakt. Dezelfde
mechaniek, dezelfde reden: een gedeelde werkboom met `git add -A` had vier bewezen paden
naar dataverlies, en een commit uit objecten kan per constructie geen pad bevatten dat de
schrijver niet noemde.

## Waarom

Tot RC-422 kreeg elke taak zijn eigen werkmap op deze twee repo's: op `main` een verse
`--depth 1`-kloon, na RC-418 een lokale kloon uit een warme kopie. Daarna `add -A`, commit,
en push met rebase. Dat betekende per ronde drie klonen van een repo met tienduizenden
commits (de deployments-repo: 6552 bestanden, ~60MB werkboom, 36.836 commits), terwijl een
taak alleen `{cluster}/{project}/` aanraakt. Op productie, over zeven dagen: 2494 klonen
tegen 1642 pushes.

## De lagen

| Laag | Wat het doet |
|---|---|
| `GitRepo` | De kopie op schijf, lezen per revisie, en `commit` / `write_dir`. Bewaart nooit credentials. |
| `GitRepoWorkspace` | De wegwerpmap van één taak: de scope erin, de generatie erop, en de write terug. |
| `get_git_repo(url, branch)` | De registry: lui aangemaakt, met een grens op schijfruimte. |

### De kopie

`TEMP_DIR/git-repos/<hash van remote#branch>`, een **bare** blobless kloon (`--bare
--single-branch --filter=blob:none`) met `gc.auto 0`. Geen werkboom, dus geen gedeelde
staat tussen taken.

De kopie is de **waarheid** waar een taak op bouwt, zoals bij de
[ProjectStore](project-store.md). `tip()` is een `rev-parse` op de lokale
`refs/heads/<branch>`, dus lezen en schrijven gaan het netwerk niet op. Een kapotte of
half bijgewerkte kopie wordt weggegooid en opnieuw opgebouwd; dat mag nooit een taak
kosten. Zie [Bijwerken](#bijwerken) voor wanneer er wel netwerk aan te pas komt.

De grens is er een op **ruimte** en niet op aantal (`GIT_REPOS_MAX_BYTES`, 1Gi): de kopieën
verschillen een orde in omvang, en `TEMP_DIR` is een emptyDir van 8Gi op productie. De minst
recent gebruikte verdwijnt eerst; een kopie waar een taak uit leest of in schrijft blijft
staan, zolang die operatie loopt.

### Netwerk alleen waar het hoort

Een blobless kopie mist de inhoud van bijna elk bestand. Git haalt een ontbrekende blob
uit zichzelf bij de remote op (een lazy fetch), ook midden in een commando dat lokaal
lijkt, en dan zonder credentials: die krijgen alleen clone, fetch, push en `ls-remote`
mee. Op de sandbox liet dat elke write falen na een externe wijziging van een bestand
buiten een projectmap, met `could not read Username`. Daarom:

- elk lokaal commando draait met `GIT_NO_LAZY_FETCH=1` (`OFFLINE_ENV`). Een ontbrekende
  blob is dan meteen een fout, nooit een stil verzoek zonder inlog;
- `write-tree --missing-ok` in `build_tree_commit`: de bestanden die een write niet
  aanraakt komen uit de tree van de ouder, dus hun blobs staan op de remote en hoeven niet
  lokaal te zijn;
- de push draait met `pack.window=0`: deltacompressie zou de oude versie van elk
  gewijzigd bestand als basis lezen, en die heeft de kopie niet. Een push draagt de
  manifesten van één project, dus de pack blijft klein;
- of een pad bestaat volgt uit de trees (`rev-parse <ref>:<pad>`), die een blobless kopie
  volledig heeft. Alleen lezen dat echt inhoud nodig heeft (`read_file`, `materialize`)
  haalt blobs op, met credentials. Lukt dat niet, dan is dat een fout en geen "het
  bestand staat er niet".

### Credentials

`GitRepo` bewaart er geen. Elke methode die het netwerk op gaat krijgt een
`GitCredentials` mee, die via `credential_env` een `http.<basis>.extraHeader` in de
omgeving zet. Nooit in de URL, nooit in `.git/config`, nooit in een procesargument
(BIO2 5.17, 8.12). De kopie op schijf staat op de URL **zonder** userinfo.

Alle projecten op dezelfde remote delen dus één kopie. Tot RC-423 deed elke lees- en
schrijfactie eerst een `ls-remote` met de credentials van de aanroeper, als
toegangscontrole per taak naar het patroon van de ArgoCD repo-server. Die controle hield
niets tegen: er is maar één partij met toegang tot deze twee repo's. Elke URL die bij een
kopie terechtkomt komt uit de instellingen van OPI. De argo-repo gebruikt het
platformtoken, en de `url` van een repository in een projectbestand wordt bij aanmaak uit
`PROJECT_REPO_URL` geschreven en is daarna `readonly_on_edit`, wat
`process_json_submission` aan de serverkant afdwingt. Het wachtwoord dat zo'n bestand
draagt is een kopie van datzelfde platformtoken. Elke aanroep toetste dus dezelfde
credentials.

Komen er ooit credentials **per project**, dan is die controle niet genoeg: dan moet de
kopie per credential-identiteit gesleuteld worden en niet alleen per (remote, branch).

### Lezen

- `read_file(path, ref)`, `list_tree(prefix, ref)`, `entries(prefix, ref)`,
  `changed_paths(old, new, prefix)`;
- `materialize(paths, ref, target_dir)` zet mappen op een revisie in een tempmap neer, via
  `git archive`, dus met modus en symlinks intact en met een promisor-fetch voor de blobs
  die een blobless kopie niet heeft. Het wijzigt niets in de kopie, dus twee lezers zitten
  elkaar niet in de weg;
- `path_unchanged_since(revision, path, ref)` voor de grendel op het projectniveau.

### Schrijven

**`commit(build_changes, ...)`** is de kern. `build_changes` krijgt een lezer op de actuele
tip en geeft terug welke paden moeten veranderen (`FileChange` met inhoud als bytes en een
modus `100644`, `100755` of `120000`, of `None` om te verwijderen). Daaruit wordt met een
eigen index een commit gebouwd, ondertekend zoals elke andere, en als **los object** naar
`refs/heads/<branch>` gepusht. De remote doet dan zelf de compare-and-swap: een commit die
niet op de laatste stand gebouwd is, wordt geweigerd.

Wordt hij geweigerd, dan wordt `build_changes` **opnieuw aangeroepen op de nieuwe tip**:
opnieuw lezen, opnieuw toepassen, begrensd door `MAX_WRITE_ATTEMPTS`. Dat is dezelfde lus
als `GitProjectStore.mutate`, en de domeinlaag beslist in `build_changes` zelf wat een
conflict is. Er is geen lokale ref om na een mislukte push terug te zetten, want er wordt
nooit een lokale branch verzet.

Niets te veranderen betekent geen commit en geen push.

**`write_dir(base_ref, scope_paths, source_dir, ...)`** zet daarop: de repo wordt onder
`scope_paths` gelijk aan wat er in `source_dir` staat, toegevoegd, gewijzigd of verwijderd
ten opzichte van `base_ref`. Buiten de scope kan er niets geraakt worden, want de commit
bevat geen pad dat niet onder een scope-pad valt.

Heeft de tip een pad in de scope sinds `base_ref` **anders** gewijzigd dan wij het
schrijven, dan is dat een `GitRepoConflictError` en doet de aanroeper zijn stap opnieuw.
Dezelfde inhoud is geen conflict.

De vergelijking van die gewijzigde paden gaat met `git diff --no-renames`. Met
rename-detectie verdwijnt het oude pad van een hernoeming uit de uitkomst, terwijl het wel
gewijzigd is, en heeft de inexacte detectie de blob-inhoud nodig: die staat op een blobless
kopie niet lokaal en zou bij de promisor-remote opgehaald moeten worden. De grendel werkt
daardoor zonder netwerk en zonder blobs.

**Guards op de bestandsset**, in `commit`, dus voor elke aanroeper: geen `*.to-sops.yaml` en
geen toegevoegde of gewijzigde `.cmp-env`. Op de set en niet op een walk of een `git
status`, zodat een onaangeroerd bestand van een ander project deze commit niet ophoudt en
een verwijdering juist wel mag.

### Bijwerken

Op het gewone pad doet een taak **geen enkele** `ls-remote` en geen `fetch`. Dat kan,
omdat de lokale ref altijd de stand is waarop gebouwd hoort te worden:

1. **Eigen writes zijn write-through.** Na een geslaagde push verzet `_push` de lokale ref
   naar die commit, die per definitie de remote tip is.
2. **Een verouderde kopie wordt door de push gevangen.** De remote weigert een commit die
   niet op zijn laatste stand gebouwd is; dan wordt er gefetcht en gaat de hele write
   opnieuw op de nieuwe stand.
3. Is de lokale ref intussen verschoven ten opzichte van de `parent` van deze poging, dan
   pushte een **andere taak in dit proces** en staat die tip hier al: dan wordt er niet
   gefetcht. Dat is het gewone geval bij gelijktijdige taken van verschillende projecten,
   want `project_write_lock` is per (repo, project).
4. **`sync(credentials)`** haalt wijzigingen van buiten op: één `ls-remote`, en alleen bij
   verschil een `fetch`, met een regel op INFO over wat er binnenkwam. Het equivalent van
   `GitProjectStore.reconcile`. Aangeroepen bij een refresh van een project of een
   deployment (`sync_platform_repos`, naast de reconcile van de store) en één keer per
   orphan sweep, die over verwijderen beslist en dat niet op een verouderde stand hoort te
   doen.

Er is **geen poll**. Bij de ProjectStore is die er zodat ingetrokken toegang binnen 300s
werkt; voor manifests is er geen zo'n reden. Het gevolg is bewust: een handmatige wijziging
in de deployments-repo blijft staan zolang niemand refresht, ook als er intussen een
rollout van een ander project draait, want die ziet dat bestand op de lokale stand
ongewijzigd. De eerstvolgende refresh van het project haalt hem op en zet hem terug naar
wat ZAD genereert.

### De werkmap van een taak

`GitRepoWorkspace` is wat de managers in handen hebben. Hij materialiseert de scope van
deze ronde in een eigen tempmap; de generatie schrijft, globt en verwijdert daar gewoon op
schijf, precies zoals in een werkboom. Dat moet ook: de SOPS-skip-als-ongewijzigd heeft de
vorige ciphertext op schijf nodig, de prunes globben de map, en de kustomization wordt uit
een glob gebouwd.

- `get_working_dir()` materialiseert bij eerste gebruik;
- `mutate_and_push(message, mutate, on_prepare=None, on_push=None)` doet de stap onder de
  lock per (repo, project) en schrijft hem weg. De eerste poging neemt de werkmap zoals hij
  is, want een eerdere stap van dezelfde taak kan er al iets in hebben gezet dat nog niet
  gecommit is (de PVC-hernoeming van `handle_service_removal`). Pas een echte botsing gooit
  de map weg en begint opnieuw. `on_prepare` omsluit het vullen van de werkmap en `on_push`
  omsluit alleen de write, zodat een aanroeper die twee netwerkstappen als eigen voortgangsstap
  kan melden (zie [voortgangspagina](task-progress-view.md));
- `remove_and_push(path, message)` voor een verwijdering: de beslissing valt binnen de lock,
  op de stand die de werkmap dan heeft;
- `last_write` is het resultaat van de laatste write via deze werkmap. `mutate_and_push`
  wist hem eerst, zodat hij altijd bij die stap hoort.

Twee repo-namen met dezelfde remote krijgen dezelfde werkmap: "infrastructure" is dezelfde
repo, branch en pad als de deployments-repo (`repositories[0]`), en dat is dus ook dezelfde
scope.

## Scope per repo

| Repo | Scope van een taak |
|---|---|
| deployments | `{repo_path}/{cluster}/{project}` (`generate_project_manifest_folder_path`), met de map per deployment, `_project` en `infrastructure` eronder |
| argo-applications | `{cluster}/{project}` en de map ernaast `{cluster}/{project}-infrastructure` |

De lezers over **alle** projecten gaan niet via een werkmap: `rendered_kinds` leest een
wegwerp-materialisatie van de hele `{cluster}/`-boom
(`ProjectManager.materialize_cluster_tree`), en de orphan sweep een `list_tree`.

## Wat er nog met GitConnector praat

`GitConnector` (`opi/connectors/git.py`) blijft voor twee dingen:

- de **ProjectStore**, die zijn eigen warme werkkopie van `zad-projects` houdt. Die gaat in
  een eigen taak over op `GitRepo`, als de domeinlaag in één keer goed kan;
- het **klonen van externe helm- en helmfile-bronnen**, elk in een eigen tempdir.

De plumbing die beide gebruiken staat op één plek in `git.py`: `run_git`,
`credential_env`, `build_tree_commit`, `strip_url_credentials` en de locks
(`push_lock_for`, `project_write_lock`).

`run_git` geeft optionele `timeout`. De push, de fetch en de ls-remote van `GitRepo` gebruiken
`GIT_PUSH_TIMEOUT_SECONDS` (60s) en de kloon `GIT_CLONE_TIMEOUT_SECONDS` (300s, want een
blobless bare kloon van de deployments-repo mag ruim duren); zonder grens is curl 300s de
enige limiet, en houdt dat ook de push-lock of de repo-lock bezet. Een
`GitCommandTimeoutError` is geen `GitPushConflictError`, dus geen retry; de fout gaat direct
naar de aanroeper en noemt de remote zonder credentials. Een afgeschoten push kan toch
geland zijn; volgende ronde leest de remote opnieuw.

## Storingen

Een korte storing van de git-server hoort geen taak te kosten, en een melding hoort te
zeggen of het aan het project of aan het platform ligt.

| Wat | Wordt herhaald? | Wat er met de kopie gebeurt |
|---|---|---|
| Netwerkstoring (DNS, geweigerde of verbroken verbinding, 5xx, TLS-handshake) | ja, `GIT_RETRY_BUDGET_SECONDS` (15s, pauzes 1, 2, 4, 8) | blijft staan |
| Time-out op een git-commando | nee | blijft staan; een halve kloon wordt opgeruimd |
| Weigering door de server (hook, beschermde branch) | nee | blijft staan |
| Geweigerde inloggegevens (401, 403, "authentication failed") | nee | blijft staan |
| Botsing (`non-fast-forward`, `fetch first`, `incorrect old value`, `cannot lock ref`) | ja, via de lus in `commit` | fetch, en de hele write opnieuw |
| Kapotte kopie (`index.lock`, ontbrekende objecten, een fetch die om een andere reden faalt) | n.v.t. | weggegooid en opnieuw gekloond |

De indeling gebeurt op de stderr van git (`is_git_network_failure`, `is_git_auth_failure`
in `git_repo.py`). `ls-remote`, `fetch` en `clone` lezen alleen, dus een herhaling kan op de
remote niets anders achterlaten. De push herhaalt BINNEN `push_lock_for`, die dus maximaal
het budget plus de time-outs bezet blijft: hij zet één commit zonder force op de branch, dus
een poging die toch landde geeft "Everything up-to-date" en een remote die ergens anders
staat weigert hem als non-fast-forward.

`"failed to push some refs"` alleen is GEEN botsing meer: git print die regel bij elke
weigering, ook bij een pre-receive hook. Dat kostte vijf pogingen met een fetch en een
melding die de verkeerde oorzaak noemde. Zo'n weigering is nu een `GitPushRejectedError`
met de reden van de server erin.

`cannot lock ref` komt ook als `[remote rejected]` binnen, maar is wel een botsing: de
server houdt de ref op een commit waar wij niet op gebouwd hebben, en opnieuw lezen en
opnieuw schrijven helpt dan wel.

Een mislukte fetch bouwt de kopie alleen opnieuw op als de kopie zelf het probleem is. Bij
een onbereikbare remote of een geweigerde inlog zou die kloon tegen dezelfde muur lopen en
was de warme kopie ook kwijt.

Elke melding begint met één zin over wat er gebeurde, bij welke repo en branch, en of het
aan het platform ligt; de stderr van git volgt na `Detail:`. "Dit ligt aan het platform"
staat er alleen bij een netwerkstoring, een geweigerde inlog en een time-out: een git-fout
die niet in te delen is kan net zo goed komen van wat het project liet schrijven.
Bijvoorbeeld:

```
De git-server van https://git.example.nl/rig-admin/zad-deployments#main is niet bereikbaar,
ook niet na herhalen gedurende 15s. Dit ligt aan het platform, niet aan het project; probeer
het later opnieuw. Detail: fatal: unable to access ...: Could not resolve host: git.example.nl
```

`self.url` bevat geen credentials, en een URL met userinfo die git zelf in zijn stderr zet
verliest die in `_detail`; `redact_text` van de taakservice kent die vorm niet.

De INFO-regel van een gelande commit noemt de duur en het aantal pogingen, en
`retry_on_connect_error` logt elke herhaling op WARNING. Er is geen aparte metric: het
histogram `rollout_phase_seconds` meet `git_prepare` en `git_push` al, ook als ze falen.

Faalt `sync_platform_repos()` in een refresh, dan faalt de stap "Project opzoeken en
wijzigingen uit git ophalen" met diezelfde melding, in plaats van open te blijven staan
terwijl de taak al gefaald was.

## Warme kopieen bij het opstarten

`warm_platform_repos()` zet bij het opstarten van OPI de bare kopie van de deployments- en
de argo-repo klaar, op de achtergrond. Een kloon van de deployments-repo kost minuten;
zonder dit betaalt de eerste taak die schrijft daarvoor. Een mislukte warm-up blokkeert het
opstarten niet en laat het niet falen, maar logt alleen. De logregel
`Bare git-kopie ... klaargezet` noemt hoe lang de kloon duurde.

## Tests

`tests/test_git_repo.py`, tegen echte git-repo's op schijf, want wat hier mis kan gaan is
git-gedrag: dat een write alleen zijn scope raakt, dat een verwijdering landt, dat modus,
symlink en binaire inhoud er ongeschonden door komen, dat twee projecten gelijktijdig
beide landen, dat een echte botsing de conflictfout geeft, dat dezelfde inhoud dat niet is,
dat een ronde zonder wijziging niets pusht, dat er geen credential in de kopie staat, dat
een tweede lezing op dezelfde SHA geen fetch doet, dat een onbruikbare kopie zich
herstelt, dat een bestandsnaam met niet-ASCII bytes een write overleeft, en dat een kopie
die in gebruik is de opruiming overleeft.

Over storingen, met een `run_git` die een stderr en een exitcode naspeelt: dat een korte
storing geen taak kost, dat een blijvende storing het platform en de stderr noemt, dat een
onbereikbare remote de warme kopie laat staan en geen kloon probeert, dat een weigering van
de server en een geweigerde inlog niet herhaald worden, dat `cannot lock ref` ondanks
`[remote rejected]` wel een botsing is, dat een credential in de stderr niet
in de melding komt, dat een kloon zonder antwoord geen halve kopie achterlaat, dat een kloon
en een `ls-remote` dezelfde herhaling krijgen als push en fetch, en dat een kapotte kopie
(een fetch-fout die geen netwerk of inlog is) nog steeds opnieuw wordt opgebouwd.

Over het bijwerken, met een telling van de git-subcommando's: dat twee writes na elkaar
geen netwerk doen, dat een externe commit door de afgewezen push gevangen wordt met precies
één fetch, dat een gelijktijdige eigen write zonder fetch landt, dat `sync` alleen fetcht
als de remote verder is, en dat de eerste `tip()` kloont en niet fetcht.
`tests/test_refresh_reconciles_first.py` dekt dat een refresh beide manifest-repo's synct
voordat hij het project opzoekt.

`tests/test_git_repo_authenticated_remote.py` draait tegen een remote over HTTP die inloggen
eist (`git http-backend` achter Basic auth, `tests/git_http_remote.py`), met een echt
blobless kopie. Een remote als pad op schijf vraagt nooit om credentials, en git negeert
`--filter` bij een lokale kloon, dus daar ontbreekt nooit een blob en valt een verzoek
zonder inlog niet op. Getoetst: dat een externe commit op een bestand in de root de
volgende write niet breekt, dat een externe commit in de eigen projectmap blijft staan,
dat een commit op een verse kopie de onaangeroerde buren houdt en alleen de push zelf naar
de remote stuurt, dat `read_file` en `materialize` op een verse kopie de inhoud brengen,
en dat `read_file` een fout geeft als de inhoud niet op te halen is.

De sectie "Time-out op het netwerk" laat alleen het ene netwerkcommando hangen (een
nep-subprocess voor `push`, `fetch` of `ls-remote`, de rest echt git) en toetst dat het
proces wordt afgeschoten, dat de melding de remote noemt, dat er geen nieuwe poging komt en
dat de push-lock daarna vrij is. De fetch-grens hangt aan `fetch()` en die van de ls-remote
aan `sync()`, want dat zijn sinds RC-423 de paden die het netwerk op gaan.

`test_commit_leest_opnieuw_op_de_nieuwe_tip_na_een_afgewezen_push` bootst de mutate-lus van
de ProjectStore na bovenop `GitRepo.commit`: lezen, aanpassen, en een externe write er
precies tussen.
