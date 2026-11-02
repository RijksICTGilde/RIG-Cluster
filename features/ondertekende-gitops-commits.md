# ArgoCD synct alleen ondertekende commits

Op een AppProject met `signatureKeys` hoort ArgoCD een revisie te weigeren waarvan de commit niet door een vertrouwde sleutel is ondertekend. OPI ondertekent zijn commits (zie `features/opi-ondertekent-zijn-commits.md`). De eis wordt afgedwongen: zie de meting hieronder.

## Status: ArgoCD dwingt de eis af

Gemeten op de sandbox, 19-10-2026, met `signatureKeys` op het AppProject van een project, de
sleutel vertrouwd in `argocd-gpg-keys-cm` en repo-server, controller en server allemaal op
`argocd-rig:v3.5.1`:

| commit op `main` van de deployments-repo | uitkomst |
|---|---|
| ongetekend | geen sync. `OutOfSync`, en een conditie `ComparisonError`: `GIT/GPG: Failed verifying revision <sha> by '<auteur>': unsigned (key_id=)`. De wijziging landt niet in het cluster. |
| ondertekend met de sleutel uit `argocd-gpg-keys-cm` | `Synced`, en de wijziging staat in het cluster |

De vorige meting (18-10-2026) faalde omdat sandbox repo-server op 3.3.10 pinde terwijl productie
op 3.5.1 draaide; de controller migreert `signatureKeys` naar `SourceIntegrity` zonder dat de
CRD dat veld kent. Het verifiëren zit in repo-server, die moet de controller-versie draaien.

Op productie is `signatureKeys` nog niet ingeschakeld: er staan geen AppProjects met de eis.
Read-only nagemeten op 21-10-2026: productie en staging draaien controller en repo-server
allebei op `argocd-rig:v3.5.1-rig2` zonder eigen pin op `spec.repo`, dus in dezelfde opbouw
waarin de meting hierboven afdwong. Productie dwingt `signatureKeys` dus af zodra de
ondertekeningssleutel er staat en de AppProjects de eis dragen. De volgorde waarin dat kan
staat op GitHub-issue #238: eerst de andere schrijvers naar deze repo's laten ondertekenen,
dan de beheerderssleutel als tweede vertrouwde sleutel erbij, en na de OPI-uitrol elk project
een keer verwerken, zodat elke HEAD ondertekend is voordat de eis gaat bijten.

## Hoe het werkt

Drie dingen moeten kloppen, per cluster:

| | waar | door wie |
|---|---|---|
| OPI ondertekent | Secret `operations-manager-git-signing-key` | `task sandbox:create-git-signing-key-secret`, op productie een beheerder |
| ArgoCD vertrouwt die sleutel | ConfigMap `argocd-gpg-keys-cm`, met de key ID als sleutelnaam | `task argocd:trust-git-signing-key` |
| ArgoCD eist de handtekening | `spec.signatureKeys` op het AppProject | dezelfde taak (app-of-apps) en OPI zelf (per project) |

De vorm van `keyID` is de **long key ID** (16 hex), niet de fingerprint: ArgoCD valideert het
veld als key ID en weigert een AppProject met een hele fingerprint. `GitSigningIdentity.key_id`
in `opi/utils/git_signing.py` leidt hem af uit de fingerprint die OPI bij het opstarten inleest.

**Het AppProject per project** rendert OPI uit
`operations-manager/python/manifests/argocd-appproject.yaml.jinja`. Zonder ingelezen sleutel
rendert de template geen `signatureKeys`, zodat een cluster waar OPI niet ondertekent blijft
werken. Het landt bij de eerstvolgende verwerking van dat project.

**Het AppProject van het app-of-apps** (`zad-app-of-apps`) staat in de bootstrap-overlay, en
zijn sleutel niet. De vertrouwde sleutel is per cluster een andere en komt uit het cluster:
de sandbox genereert hem tijdens `task sandbox:setup`, op productie zet een beheerder hem
neer, en `security/` staat niet in git. Daarom zetten `argocd-gpg-keys-cm` en de
`signatureKeys` van dat AppProject met een taak die beide uit het secret in het cluster
afleidt, in plaats van met een sleutel die plat in de overlay staat. De taak hangt achter
`task bootstrap-argo-system`: die apply zet `signatureKeys` terug naar wat in git staat, dus
zonder dat zou de controle bij elke bootstrap stil wegvallen.

```bash
task argocd:trust-git-signing-key           # OPI's eigen sleutel, uit het secret
task argocd:trust-public-key KEY_FILE=...   # een andere sleutel, bijvoorbeeld van een beheerder
```

Beide taken zijn idempotent: ze mergen de sleutel in de ConfigMap en voegen de key ID toe aan
`signatureKeys` zonder de sleutels die er al staan weg te gooien. Zonder signing-key-secret
is er voor de eerste niets te vertrouwen: Task slaat de taak dan over (een `status`, dus
zonder melding) en `task bootstrap-argo-system` loopt door. Wil je weten of de sleutel er
staat, dan toont `argocd gpg list` wat ArgoCD vertrouwt.

## Een sleutel vervangen

In deze volgorde, zodat er geen moment is waarop de sync op een handtekening valt:

1. de nieuwe sleutel erbij: `task argocd:trust-public-key KEY_FILE=<nieuwe publieke sleutel>`
2. OPI op de nieuwe sleutel zetten (het secret vervangen, OPI herstart leest hem in)
3. wachten tot elk project opnieuw verwerkt is, zodat elk AppProject per project de nieuwe
   key ID draagt
4. de oude eruit: de key ID uit `signatureKeys` en zijn sleutel uit `argocd-gpg-keys-cm` halen

## Als een sync op de handtekening valt

De Application blijft `OutOfSync` en draagt een conditie `ComparisonError` met de melding
`GIT/GPG: Failed verifying revision <sha> by '<auteur>': unsigned (key_id=)`. Health blijft
op de laatste goede reconciliatie staan, dus health zegt niets; de conditie is het enige
signaal. In het portaal staat die conditie op de statuskaart van de deployment als **Commit
niet ondertekend** met de revisie eronder, en de uitleg noemt de key ID die ArgoCD hier wel
accepteert, zodat zichtbaar is of de commit met de verkeerde sleutel ondertekend is. Een
deploy die erop valt stopt meteen met dezelfde melding in plaats van op zijn time-out te
wachten (`features/argocd-render-error-surfacing.md`).

Zo komt het weer goed:

- **Is de commit van OPI** (of gewenst): laat het project opnieuw verwerken. De volgende OPI-commit is ondertekend.
- **Hoort niet:** haal hem van `main` af.
- **Moet de commit blijven en is hij niet van OPI** (een handmatige ingreep, bijvoorbeeld de
  rotatie van de SOPS-sleutel), onderteken hem dan opnieuw met een sleutel die het cluster
  vertrouwt. Dat kan alleen door de commit te herschrijven, dus met een force-push, en dat
  hoort bij een beheerder:

  ```bash
  task argocd:trust-public-key KEY_FILE=<publieke beheerderssleutel>   # eenmalig
  git commit --amend -S --no-edit            # de HEAD zelf
  git rebase --exec 'git commit --amend -S --no-edit' <sha>~1          # of een reeks
  git push --force-with-lease origin main
  ```

  Dit werkt alleen voor de argo-repo: de taak zet de sleutel op `zad-app-of-apps`, en het
  AppProject van een project rendert OPI met alleen zijn eigen key ID. Een commit in de
  deployments-repo blijft dus geweigerd, ook met een beheerderssleutel ondertekend. Draai de
  wijziging liever terug en laat OPI hem opnieuw maken: force-push op een repo waar ArgoCD op
  staat tast elke Application ervan aan.

Debug:
- `git log --format='%h %G? %s'`: ondertekenstatus per commit (`N`/`G`/`E`)
- `kubectl get configmap argocd-gpg-keys-cm -n <ns> -o yaml`: vertrouwde sleutels
- `task git-signing-key:public`: sleutel van het cluster
- repo-server log: `git-verify-wrapper.sh <sha>` en het oordeel per revision

## Wie er nog naar deze repo's schrijft

Gemeten op 18-10-2026 (`grep -n "git commit\|git push" scripts/*.py Taskfile.yaml`):

| schrijver | repo | gevolg |
|---|---|---|
| OPI | argo-repo, deployments-repo | ondertekend, dus geen |
| `scripts/project_rotation.py` (sleutelrotatie, PAT-ronde) | zad-projects | geen: die repo synct ArgoCD niet |
| `scripts/secret_rotate.py`, push-stap | de repo van de Application die het secret bezit | al zijn componenten zijn platformsecrets in deze repo, en de platform-apps hebben geen `signatureKeys` |
| de rotatie van de SOPS-sleutel, met de hand | argo-repo | **die commit moet ondertekend zijn**, zie `features/sops-sleutel-roteren.md` |
| `task sandbox:sync` | de infrastructuur-repo in Forgejo | geen: platform-app |
| `task sandbox:init-forgejo` | alle vier de repo's, de initiële commit | zie hieronder |

De scripts gebruiken de gewone git-signing-configuratie (`user.signingkey`, `gpg.format`,
`commit.gpgsign`): een commit van een beheerder die dat aan heeft staan is ondertekend zonder
dat er iets aan de scripts verandert. Vertrouw die sleutel met
`task argocd:trust-public-key`. Waar zo'n beheerderssleutel hoort te wonen is nog open; een
bestand op disk is wat het vandaag zou zijn, en een hardwaretoken is beter (de sleutel
verlaat het apparaat niet, en er is geen sleutel nodig om de sleutel te beschermen).

## Grenzen

- **De platform-apps** (`zad-platform`) krijgen geen `signatureKeys`. Op die repo committen
  mensen, en een merge via de GitHub-UI draagt de sleutel van GitHub. Dat vraagt een eigen
  afweging.
- **Op een vers cluster** is de HEAD van de argo-repo de initiële commit van
  `task sandbox:init-forgejo`, en die is niet ondertekend. `user-applications` valt daar dan
  op de handtekening tot OPI zijn eerste commit doet, dus tot het eerste project verwerkt is.
- **Een ongetekende commit van voor de sleutel.** Een OPI-pod die zonder het secret draaide
  liet ongetekende commits achter (op de sandbox op 18-10-2026 de hele historie). Een repo
  waarvan de HEAD van voor de sleutel komt, blokkeert dus tot de eerstvolgende OPI-commit.
- **De repo-server moet dezelfde ArgoCD-versie draaien als de controller.** Het verifiëren
  gebeurt in de repo-server; staat die een minor achter, dan accepteert de controller de
  revisie zonder dat er iets in de status zegt dat de controle niet liep. Dat is wat de
  meting van 18-10-2026 misleidde. `tests/test_argocd_repo_server_volgt_de_controller.py`
  maakt een overlay die de repo-server een eigen image of versie geeft rood.
- **De repo van een project kan zijn broncode-repo zijn.** De `sourceRepos` van het AppProject
  per project komen uit `project_data["repositories"]` (`argo_manager.py:528`), en dat is op
  productie geen Forgejo-`zad-deployments` maar de GitHub-repo van het project zelf. Daar
  committen mensen, en hun commits zijn niet door OPI ondertekend. Dit is de eerste plek waar
  de afdwinging gaat bijten, en het vraagt dezelfde afweging als de platform-apps hierboven.
- **Wie schrijfrecht heeft op de repo's** en branch protection daarop is een repo-instelling,
  geen code.
- **Na de ApplicationSet-overgang** dekt `signatureKeys` op het app-of-apps de
  Application-laag niet meer. Zie `features/futures/argocd-applicationset-migration.md`.

## Afhankelijkheden

- `features/opi-ondertekent-zijn-commits.md`: de sleutel en het ondertekenen zelf
- `features/argocd-render-error-surfacing.md`: de weg waarlangs de conditie bij de gebruiker komt
- `features/argocd-appprojects.md`: de AppProjects waar `signatureKeys` op staat
- `gpg` en `kubectl` lokaal voor de taken hierboven
