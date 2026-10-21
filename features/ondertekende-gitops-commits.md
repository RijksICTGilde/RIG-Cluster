# ArgoCD synct alleen ondertekende commits

Op een AppProject met `signatureKeys` hoort ArgoCD een revisie te weigeren waarvan de commit niet door een vertrouwde sleutel is ondertekend. OPI ondertekent zijn commits (zie `features/opi-ondertekent-zijn-commits.md`). De eis staat, maar ArgoCD dwingt hem nog niet af: zie de status hieronder.

## Status: de controle staat klaar, maar ArgoCD dwingt hem nog niet af

Gemeten op de sandbox, 18-10-2026, met `signatureKeys` op het AppProject van een project en
de sleutel vertrouwd: een ONGETEKENDE commit op `main` van de deployments-repo synct gewoon
door en het gewijzigde label verschijnt in het cluster. De Application blijft Synced/Healthy
en krijgt geen condition.

Wat dat in de weg staat zit niet in deze code maar in de ArgoCD van dit cluster:

- De application-controller is `ghcr.io/minbzk/base-images/argocd-rig:v3.5.1-rig1`, en die
  logt bij elke reconcile `Creating project SourceIntegrity from legacy SignatureKeys
  specified in <appproject> AppProject. Migrate them to SourceIntegrity.` ArgoCD 3.5 heeft
  `signatureKeys` vervangen door `spec.sourceIntegrity`.
- Dat veld bestaat niet in de CRD die argocd-operator v0.14 installeert: een patch erop
  geeft `Warning: unknown field "spec.sourceIntegrity"` en wordt weggesnoeid. Migreren kan
  dus niet.
- De controller heeft ook geen GPG-keyring: alleen de repo-server (hier gepind op upstream
  `v3.3.10`) heeft de volumes `gpg-keys` en `gpg-keyring`. Die repo-server importeert de
  vertrouwde sleutel wel en draait `git-verify-wrapper.sh` op de revisie, maar het oordeel
  komt niet in de status terecht.

Wat deze feature nu dus wel doet: de sleutel staat vertrouwd in het cluster, de AppProjects
dragen de eis, en de dag dat de ArgoCD-CRD `sourceIntegrity` kent is het een veldnaam die om
moet, geen nieuw ontwerp. Wat eraan vast zit, hoort in een vervolgtaak: de
argocd-operator-versie (en daarmee de CRD) bijwerken, `signatureKeys` migreren naar
`sourceIntegrity`, de repo-server-pin tegen de controller-versie leggen, en daarna de meting
hieronder opnieuw doen.

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

Zodra ArgoCD de eis afdwingt (zie de status hierboven) valt de sync van de geraakte
Applications stil met een condition die de revisie noemt. Zo komt het weer goed:

- Weet je wie die commit maakte en hoort hij er te zijn, laat OPI dan het project opnieuw
  verwerken: zijn volgende commit is ondertekend en de sync loopt weer.
- Hoort hij er niet, dan is dit precies wat de controle moet doen: de commit is niet van OPI.
  Haal hem van `main` af.
- `git log --format='%h %G? %s' -3` in de betreffende repo zegt per commit of hij
  ondertekend is (`N` is niet ondertekend, `G` is goed);
  `kubectl get configmap argocd-gpg-keys-cm -n <namespace> -o yaml` welke sleutels vertrouwd
  zijn, en `task git-signing-key:public` welke sleutel het cluster gebruikt. Wat de
  repo-server van een revisie vond staat in zijn log, bij `git-verify-wrapper.sh <sha>`.

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
- **Een ongetekende commit van voor de sleutel.** De commits van OPI in de sandbox-repo's
  waren op 18-10-2026 nog niet ondertekend (`git log --format='%G?'` geeft `N`): die pod
  draaide zonder sleutel. Een repo waarvan de HEAD van voor de sleutel komt, blokkeert dus
  tot de eerstvolgende OPI-commit.
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
- `features/argocd-appprojects.md`: de AppProjects waar `signatureKeys` op staat
- `gpg` en `kubectl` lokaal voor de taken hierboven
