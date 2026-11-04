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

Bijwerken gaat zoals de ArgoCD repo-server het doet: eerst `ls-remote` met de credentials
van de **aanroeper** (dat is de toegangscontrole per taak, en het levert de SHA), dan
`cat-file -e`, en alleen als die revisie er niet staat een `fetch`. Een kapotte of half
bijgewerkte kopie wordt weggegooid en opnieuw opgebouwd; dat mag nooit een taak kosten.

De grens is er een op **ruimte** en niet op aantal (`GIT_REPOS_MAX_BYTES`, 1Gi): de kopieën
verschillen een orde in omvang, en `TEMP_DIR` is een emptyDir van 8Gi op productie. De minst
recent gebruikte verdwijnt eerst; een kopie waar een taak uit leest of in schrijft blijft
staan, zolang die operatie loopt.

### Credentials

`GitRepo` bewaart er geen. Elke methode die het netwerk op gaat krijgt een
`GitCredentials` mee, die via `credential_env` een `http.<basis>.extraHeader` in de
omgeving zet. Nooit in de URL, nooit in `.git/config`, nooit in een procesargument
(BIO2 5.17, 8.12). De kopie op schijf staat op de URL **zonder** userinfo.

Alle projecten op dezelfde remote delen dus één kopie, en wie leest moet zelf toegang
hebben: een ingetrokken of verkeerd token faalt op de `ls-remote` voordat er iets uit de
kopie komt.

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

### De werkmap van een taak

`GitRepoWorkspace` is wat de managers in handen hebben. Hij materialiseert de scope van
deze ronde in een eigen tempmap; de generatie schrijft, globt en verwijdert daar gewoon op
schijf, precies zoals in een werkboom. Dat moet ook: de SOPS-skip-als-ongewijzigd heeft de
vorige ciphertext op schijf nodig, de prunes globben de map, en de kustomization wordt uit
een glob gebouwd.

- `get_working_dir()` materialiseert bij eerste gebruik;
- `mutate_and_push(message, mutate)` doet de stap onder de lock per (repo, project) en
  schrijft hem weg. De eerste poging neemt de werkmap zoals hij is, want een eerdere stap
  van dezelfde taak kan er al iets in hebben gezet dat nog niet gecommit is (de
  PVC-hernoeming van `handle_service_removal`). Pas een echte botsing gooit de map weg en
  begint opnieuw;
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

## Tests

`tests/test_git_repo.py`, tegen echte git-repo's op schijf, want wat hier mis kan gaan is
git-gedrag: dat een write alleen zijn scope raakt, dat een verwijdering landt, dat modus,
symlink en binaire inhoud er ongeschonden door komen, dat twee projecten gelijktijdig
beide landen, dat een echte botsing de conflictfout geeft, dat dezelfde inhoud dat niet is,
dat een ronde zonder wijziging niets pusht, dat er geen credential in de kopie staat, dat
een tweede lezing op dezelfde SHA geen fetch doet, dat een onbruikbare kopie zich
herstelt, dat een bestandsnaam met niet-ASCII bytes een write overleeft, en dat een kopie
die in gebruik is de opruiming overleeft.

`test_commit_leest_opnieuw_op_de_nieuwe_tip_na_een_afgewezen_push` bootst de mutate-lus van
de ProjectStore na bovenop `GitRepo.commit`: lezen, aanpassen, en een externe write er
precies tussen.
