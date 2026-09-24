# De SOPS-sleutel en een GitHub-token roteren

De GitHub-PAT verloopt en moet vervangen worden. We nemen die rotatie als aanleiding om tegelijk de AGE-sleutel te roteren en er een terugkerende handeling van te maken: dezelfde bestanden, dezelfde lus, dezelfde verificatie.

We roteren elk kwartaal preventief. Een aanleiding daarbuiten is bijvoorbeeld een collega die weggaat, of het vermoeden dat een sleutel bekend is geworden.

Waarom er gereedschap bij hoort: `sops rotate` ziet alleen SOPS-bestanden, en de platformsleutel zit op meer plekken dan dat. Het gereedschap zet elke vindplaats om, toont aan dat er niets anders is veranderd dan de encryptie, en toont aan dat de oude sleutel daarna niets meer opent.

Dit onderbouwt BIO2 v1.3 control 8.24: 8.24.01 vraagt registratie van waar welke cryptografie wordt toegepast, 8.24.02 vraagt dat die actueel wordt gehouden. Dat laatste ontbrak, want een AGE-sleutel kent geen verlooptijd.

## Wat de sleutel vasthoudt

```
platformsleutel  (security/key.txt = k8s secret `sops-age-key` = SOPS_AGE_KEY_CONTENT)
   |
   +-- elk SOPS-bestand dat deze sleutel als recipient heeft, in bootstrap/ en infrastructure/
   |
   +-- de losse base64+age:-waarden op de lijst LOOSE_VALUE_FILES
   |      Niet alleen env-regels: dezelfde waarde staat ook als Python-literal. Daarom een
   |      lijst en geen patroon, en daarom een guard op die lijst (coverage_gaps).
   |
   +-- de projectbestanden in de projects-repo
   |      per project twee velden: config.age-private-key en repositories[].password
   |      |
   |      +-- de EIGEN sleutel van dat project, en daaronder al zijn andere geheimen
   |
   +-- de ArgoCD repository-secrets in de argo-applicatierepo
          afgeleid uit repositories[].password, met het wachtwoord er PLAT in
```

De aantallen staan bewust nergens: het gereedschap selecteert op recipient en op die lijst, en telt zelf. Een dry-run noemt wat hij gevonden heeft, en dat is het actuele getal.

Buiten scope: de sleutels per project (issue #183) en de zad-deployments-repo, die op de projectsleutel staat.

## Het gereedschap

| script | doet |
|---|---|
| `scripts/rotate-sops-key.py` | de SOPS-bestanden op deze recipient, de losse waarden en `projects/` in DEZE repo, plus met `--argo-applications` de SOPS-bestanden in een clone van de argo-applicatierepo |
| `scripts/rotate-project-keys.py` | de projectbestanden in een clone van de projects-repo |
| `scripts/replace-git-pat.py` | de PAT-ronde: dezelfde projectbestanden, plus de losse waarden die de token DRAGEN en de ArgoCD repository-secrets |
| `scripts/set-sops-key-secret.py` | het k8s-secret wisselen en de operations-manager herstarten |
| `scripts/scan-secrets.py` | de guard: weigert een commit, een branch of een historie met een geheim |

De streepjesnamen zijn dunne ingangen; de logica staat in modules ernaast, zie `scripts/README.md`. Losse handelingen eronder staan in `docs/sops-en-age-met-de-hand.md`.

## Twee situaties

Bepaal eerst welke van de twee je doet. Ze lopen door dezelfde fasen; alleen stap 3 en de eindtoetsen verschillen.

**A. Alleen de sleutel.** De token blijft zoals hij is. Stap 3 draait `rotate-project-keys.py`, de eindtoetsen krijgen geen tokenvlaggen, en stap 7 vervalt.

**B. De sleutel en de token samen.** Stap 3 draait `replace-git-pat.py`, die de token vervangt én omsleutelt, en de eindtoetsen krijgen `--pat-new-file` en `--pat-current-file` mee. Stap 7 vervalt, want de vervanging is dan al gebeurd.

Doe je A en moet de token later alsnog om, dan is dat stap 7: een tokenronde op zichzelf, met dezelfde eindtoets erachter.

Waarom die vlaggen alleen bij B horen: `--pat-current-file` eist dat geen enkel veld nog naar de oude token ontsleutelt. In A is die token niet vervangen, dus dat zou terecht overal rood melden.

## Gebruik

Draai alles vanuit de repo-root. De ronde heeft vier fasen, en de grens die telt zit tussen VERIFY-1 en APPLY: tot daar raakt niets productie en blijft alles op de oude sleutel werken.

### PREPARE -- herversleutelen, en niets versturen

```bash
# 1. sleutel B maken en de namen op hun plek zetten
uv run --project operations-manager/python python scripts/rotate-sops-key.py --rename --generate-new-key

# 2. deze repo EN de argo-applicatierepo: eerst kijken, dan doen
uv run --project operations-manager/python python scripts/rotate-sops-key.py --argo-applications /tmp/zad-argo --dry-run
uv run --project operations-manager/python python scripts/rotate-sops-key.py --argo-applications /tmp/zad-argo

# 3. de projectbestanden, op een VERSE clone. Dit is situatie A. In situatie B draai je hier
#    replace-git-pat.py in plaats van rotate-project-keys.py, met --argo-applications erbij,
#    en krijgen de eindtoetsen van stap 4 en 6 er --pat-new-file en --pat-current-file bij:
#    de vervanging is dan al gebeurd, dus zonder die twee meten ze de token niet.
uv run --project operations-manager/python python scripts/rotate-project-keys.py --projects /tmp/zad-projects/projects --dry-run
uv run --project operations-manager/python python scripts/rotate-project-keys.py --projects /tmp/zad-projects/projects
```

### VERIFY-1 -- het go/no-go moment, voor de push

```bash
# 4. alle drie de repo's nalopen TERWIJL er nog niets gepusht is
uv run --project operations-manager/python python scripts/rotate-sops-key.py --assert-old-key-dead --projects /tmp/zad-projects/projects --argo-applications /tmp/zad-argo
git -C /tmp/zad-projects log --oneline | head
git -C /tmp/zad-projects diff --stat origin/HEAD

# en de argo-clone moet een VOLLEDIGE render geven, met dezelfde vlaggen en dezelfde
# mappenkeuze als de plugin gebruikt
export SOPS_AGE_KEY="$(sed -n '3p' security/key.txt)"
find /tmp/zad-argo -mindepth 2 -name kustomization.yaml -exec dirname {} \; | sort -u |
  while read -r folder; do
    kustomize build --enable-alpha-plugins --enable-exec --enable-helm "$folder" > /dev/null ||
      echo "RENDER FAALT: $folder"
  done
```

Vanaf hieronder worden de wijzigingen daadwerkelijk doorgevoerd!

### APPLY -- het korte venster

```bash
# 5. pushen, alle drie de repo's, en METEEN daarna het secret wisselen
uv run --project operations-manager/python python scripts/set-sops-key-secret.py --dry-run
uv run --project operations-manager/python python scripts/set-sops-key-secret.py

# en ArgoCD de eerste render met de nieuwe sleutel laten doen terwijl je kijkt
kubectl annotate application production-infrastructure -n rig-system argocd.argoproj.io/refresh=hard --overwrite
kubectl annotate application user-applications -n rig-system argocd.argoproj.io/refresh=hard --overwrite
kubectl annotate application ron-infrastructure -n rig-system argocd.argoproj.io/refresh=hard --overwrite
```

**Lees de dry-run van stap 5 regel voor regel: het zijn er MEER dan een.** `sops-age-key` is geen secret in een vaste namespace maar een naam die in veel namespaces voorkomt, want de plugin leest hem uit de namespace waar de applicatie naartoe deployt. Op productie dragen er twee de platformsleutel: `rig-prd-operations` en `rig-prd-ron`. Alle andere dragen een eigen projectsleutel en moeten met rust blijven. Het script bepaalt dat verschil door te meten welke de OUDE publieke sleutel dragen; die twee lijsten zijn je controle. Het contract erachter staat in `instructions/sops-sleutel-in-het-cluster.md`.

De handmatige sync is er omdat de plugin het secret bij ELKE render leest: de eerste render na de wissel is het bewijs dat het goed staat.

### VERIFY-2 -- werkt alles nog

```bash
# 6. de rooktest en de eindtoets
kubectl -n rig-system get applications -o wide
kubectl -n rig-prd-operations rollout status deployment/operations-manager
uv run --project operations-manager/python python scripts/rotate-sops-key.py --assert-old-key-dead --projects /tmp/zad-projects/projects --argo-applications /tmp/zad-argo
```

### Daarna -- de tokenronde, alleen na situatie A

```bash
# 7. alleen na situatie A, als de token alsnog om moet. Verse clone, want er kan sinds stap 3
#    gepusht zijn. --argo-applications is verplicht: het repo-wachtwoord staat daar PLAT in
#    het sops-bestand, dus zonder die vlag blijft ArgoCD op de ingetrokken token staan.
uv run --project operations-manager/python python scripts/replace-git-pat.py --projects /tmp/zad-projects/projects --argo-applications /tmp/zad-argo --dry-run
uv run --project operations-manager/python python scripts/replace-git-pat.py --projects /tmp/zad-projects/projects --argo-applications /tmp/zad-argo

# 7b. de eindtoets MET beide tokens erbij
uv run --project operations-manager/python python scripts/rotate-sops-key.py --assert-old-key-dead --projects /tmp/zad-projects/projects --argo-applications /tmp/zad-argo --pat-new-file security/pat_new.txt --pat-current-file security/pat_current.txt

# 8. een dag later nog een keer, en dan pas mag de oude sleutel weg
uv run --project operations-manager/python python scripts/rotate-sops-key.py --remove-old-key --projects /tmp/zad-projects/projects --argo-applications /tmp/zad-argo --pat-new-file security/pat_new.txt --pat-current-file security/pat_current.txt
```

Daarna pas de twee tokenbestanden opruimen. En trek de oude token in bij GitHub; zolang dat niet gebeurd is, is de rotatie niet af.

## Wat je moet weten voordat je typt

**Vervangen is voorwaardelijk.** De PAT-ronde krijgt twee tokenbestanden: `security/pat_current.txt` is de token die vervangen wordt, `security/pat_new.txt` die ervoor in de plaats komt. Alleen waar de huidige waarde gelijk is aan de eerste wordt vervangen; al het andere wordt alleen omgesleuteld en gemeld. Dat is geen theorie: in de argo-secrets staan waarden die van de rest afwijken, en een onvoorwaardelijke ronde schrijft die zonder een woord over.

**De eindtoets zonder tokenvlaggen gaat alleen over de SLEUTEL.** Een veld kan keurig op de nieuwe sleutel staan en de ingetrokken token bevatten. `--pat-current-file` is wat bewijst dat de oude token nergens meer staat.

**`sops rotate`, niet `updatekeys`.** `updatekeys` wisselt alleen de recipients en laat de datasleutel staan, dus wie de oude datasleutel heeft leest het bestand daarna nog steeds.

**Selectie gaat op recipient, nooit op naam.** Daarom vindt een andere `--old-key` vanzelf de bestanden van die sleutel, en daarom raakt een productieronde de sandboxbestanden niet.

**De vingerafdruk is het bewijs.** Per veld de sha256 van de PLATTE tekst, vóór en na. Daarmee toon je aan dat alleen de sleutel veranderde en niet de inhoud. Hij staat in `security/`, buiten git, en bevat geen geheim.

```bash
uv run --project operations-manager/python python scripts/rotate-sops-key.py --verify --argo-applications /tmp/zad-argo
```

**Op de lijst met losse waarden zit een guard.** Een getrackt bestand met echte ciphertext dat nergens wordt bereikt stopt het gereedschap. Dat is er niet voor niets: drie waarden zaten eerder buiten elke ronde en de eindtoets meldde CLEAN over ze heen.

**Rotatie is een re-bootstrap.** De platformsleutel komt niet uit git, en dat kan ook niet: het is de sleutel waarmee git ontsleuteld wordt. Hij wordt bij het aanzetten van een cluster neergezet en die plaatsing is idempotent; de wissel is diezelfde plaatsing met een ander sleutelbestand.

## De guard

Drie lagen, zodat een geheim niet in git komt: de pre-commit hook (lokaal, met `--no-verify` te omzeilen, en dat is geaccepteerd), de security-workflow (bindend, scant de hele boom van de branch) en GitHub push protection (op de server).

Een AGE-kandidaat telt alleen als `age-keygen` hem als geldige sleutel accepteert. Zonder die controle zou elke placeholder in de testsuite een bevinding zijn, en een alarm met bekende bevindingen erin is een alarm waar mensen omheen leren lopen. Daarom staat er ook geen vaste sleutel meer in een toets: wie er een nodig heeft maakt er een.

De scan meldt wat hij NIET gelezen heeft, met de reden. Een melding die zwijgt over overgeslagen bestanden leest als groen terwijl er niet gekeken is.

## Wat hierna komt

- **De sleutels per project** (#183): die staan los van deze ronde en hebben hun eigen hiërarchie.
- **De oefenronde**: PREPARE en VERIFY-1 maandelijks op een wegwerpsleutel, met het resultaat weggegooid, zodat de ingreep geoefend blijft zonder dat er iets productie raakt. Een voorstel; er draait nog geen workflow voor.
- **De kubectl-aanroepen** via een library of de API in plaats van de commandline (#182).
