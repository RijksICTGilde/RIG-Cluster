# Git push-serialisatie per repo

`GitConnector.push_changes` serialiseert de hele push-poging (fetch + rebase + push) per
`(repo, branch)`, process-breed. Dit voorkomt dat gelijktijdige taken die naar dezelfde gedeelde
git-repo pushen op elkaar botsen.

## Het probleem

De project-store maakt schrijven naar `zad-projects` veilig. Maar bij het verwerken van een project
schrijft OPI gegenereerde manifests naar twee **andere** repos:

- `zad-deployments` (Kubernetes-manifests)
- `zad-argo-user-applications` (ArgoCD Application-manifests)

Die vallen buiten de store en gebruikten per operatie een verse `GitConnector` die rechtstreeks naar
`main` pushte, zonder onderlinge serialisatie. Als meerdere taken hetzelfde project tegelijk
verwerkten (bijvoorbeeld drie gelijktijdige project-brede `update_component`-patches), pushten ze
allemaal tegelijk naar `zad-deployments/main`. Git weigert de tweede push (non-fast-forward), de
code rebaset en probeert het opnieuw - maar onder load blijven de taken elkaars rebase ongeldig
maken tot de retries op zijn:

```
Failed to push changes to main ... zad-deployments.git
! [remote rejected] main -> main (incorrect old value provided) ... after 5 attempts
```

## De oplossing

Een process-brede `asyncio.Lock` **per (repo, branch)**, alleen vastgehouden rond de push-poging in
`push_changes`. Manifest-generatie blijft parallel; alleen de push serialiseert.

Waarom het werkt: nu maken meerdere taken elkaars rebase kapot doordat ze tegelijk pushen. Met de
lock wacht een taak op de vorige, krijgt dan één nette non-fast-forward, rebaset op de winnaar en
pusht - zonder concurrent die het opnieuw ongeldig maakt. Het thrash-gedrag verdwijnt.

- De lock omsluit de héle retry-lus (fetch + rebase + push), niet alleen de push-aanroep, zodat de
  rebase-en-push atomair is ten opzichte van andere pushers in dit proces.
- Gekeyed op een **credential-vrije** genormaliseerde repo-URL + branch, zodat verschillende
  per-operatie-connectors naar dezelfde repo dezelfde lock delen en de sleutel nooit een secret
  bevat.
- Intra-proces, consistent met de `asyncio.Lock` van de project-store en het model van één replica
  per cluster.

## Reikwijdte en afweging

Dit is **aanpak A**: de push serialiseren om de botsing weg te nemen. `allow_rebase=True` blijft op
het deployments/argo-pad, dus bij een non-fast-forward wordt nog steeds ge-rebaset (text-merge). Voor
**gegenereerde** manifests is dat laag risico: elke herverwerking regenereert de volledige set
deterministisch uit hetzelfde projectbestand, dus het convergeert.

De project-store (`zad-projects`) blijft `allow_rebase=False` gebruiken met zijn eigen
compare-and-swap; de push-lock is daar orthogonaal aan en verandert die semantiek niet. Een
toekomstige **aanpak B** zou `zad-deployments`/argo hun eigen store-achtige object geven (warme
working copy + CAS-commit-uit-git-objecten), waarmee ook het laatste text-merge-risico verdwijnt.

## Per project, op de nieuwste stand (RC-265)

De push-lock neemt de botsing op de ref weg, maar niet die op de INHOUD. Elke delete- en
create-taak herschrijft `odcn-production/<project>/kustomization.yaml` in
`zad-argo-user-applications`, en elke taak werkt in een kloon van bij de start van die taak.
De mutatie en de herbouw van die kustomization gingen dus uit van een stand die een andere
taak intussen kan hebben gewijzigd. Op 15 oktober 2026 strandden drie van acht
gelijktijdige deletes in `nd-j7s` op `GitPushConflictError` (`pr-66`, `pr-69`, `pr-75`), en
om 18:45 ook een create: de rebase kon de twee versies van die ene herschreven file niet
samenvoegen.

`GitConnector.mutate_and_push(message, mutate)` neemt de mutatie daarom als FUNCTIE aan en
past hem toe binnen een lock per `(repo, project)`:

1. `reset_to_remote()`, zodat de beslissing en de herbouw op de laatste stand vallen;
2. `mutate()`, de semantische wijziging (bestand weg of erbij, kustomization herbouwd);
3. `commit_and_push()`.

Dat kan omdat een OPI-instantie de enige schrijver is voor zijn eigen clustermap (het
Distributed Model). Wat NA de push hoort (een ArgoCD-refresh, een wacht) staat buiten de
mutatie, anders houdt de lock andere taken op voor iets dat niets meer schrijft. De
rebase-herhaling in `push_changes` blijft voor botsingen tussen verschillende projecten;
beide locks gebruiken dezelfde credential-vrije sleutelvorm (`_repo_lock_key`).

Dit is dus aanpak B voor de argo-repo, en de goedkope helft ervan: geen warme working copy,
maar wel een commit die aantoonbaar op de laatste stand is gebouwd. `zad-deployments` loopt
nog op aanpak A.

## Uitzondering

De eenmalige bootstrap-push voor een lege repo (`push -u origin <branch>` bij initiële setup) loopt
niet via `push_changes` en is geen concurrency-pad; die blijft ongewijzigd.

## Tests

`tests/test_git_push_lock.py`: de lock-sleutel is credential-vrij en stabiel; vier gelijktijdige
pushes naar dezelfde ref lopen niet-overlappend (piek-concurrency 1); pushes naar verschillende
repos lopen wel parallel.

`tests/test_argo_applications_gelijktijdig_schrijven.py`: tegen een echte bare repo op schijf,
want het bewijs zit in git. Acht gelijktijdige deletes in een project landen alle acht, de
kustomization noemt precies de overgebleven bestanden, een create en een delete tegelijk gaan
beide door, en elke taak ziet de verwijdering van de vorige. Zonder `mutate_and_push` falen
alle drie op `GitPushConflictError`.

## Zie ook

- De project-store (`opi/services/project_store.py`) - dezelfde serialisatie-aanpak voor
  `zad-projects`.
