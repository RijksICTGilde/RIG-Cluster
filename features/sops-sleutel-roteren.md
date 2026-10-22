# De SOPS-sleutel en een GitHub-token roteren

De GitHub-PAT verloopt en moet vervangen worden. We nemen die rotatie als aanleiding om tegelijk de AGE-sleutel te roteren en er een terugkerende handeling van te maken: dezelfde bestanden, dezelfde lus, dezelfde verificatie.

We roteren elk kwartaal preventief. Een aanleiding daarbuiten is bijvoorbeeld een collega die weggaat, of het vermoeden dat een sleutel bekend is geworden.

Waarom er gereedschap bij hoort: `sops rotate` ziet alleen SOPS-bestanden, en de platformsleutel zit op meer plekken dan dat. Het gereedschap zet elke vindplaats om, toont aan dat er niets anders is veranderd dan de encryptie, en toont aan dat de oude sleutel daarna niets meer opent.

Dit onderbouwt BIO2 v1.3 control 8.24: 8.24.01 vraagt registratie van waar welke cryptografie wordt toegepast, 8.24.02 vraagt dat die actueel wordt gehouden. Dat laatste ontbrak, want een AGE-sleutel kent geen verlooptijd.

## Wat de sleutel vasthoudt

```
platformsleutel  (security/key.txt = k8s secret `sops-age-key`, in de OPI-pod gemount
                  op /var/run/secrets/zad/sops-age/key)
   |
   +-- elk SOPS-bestand dat deze sleutel als recipient heeft, in bootstrap/ en infrastructure/
   |
   +-- de losse base64+age:-waarden op de lijst LOOSE_VALUE_FILES
   |      Niet alleen env-regels: dezelfde waarde staat ook als Python-literal. Daarom een
   |      lijst en geen patroon, en daarom een guard op die lijst (coverage_gaps).
   |
   +-- de projectbestanden in de projects-repo
   |      per project nog EEN veld: repositories[].password
   |
   +-- de ArgoCD repository-secrets in de argo-applicatierepo
          afgeleid uit repositories[].password, met het wachtwoord er PLAT in


project-KEK      (security/project-kek-<cluster>.txt = k8s secret `project-kek`, alleen in
                  de OPI-namespace van DAT cluster, gemount op
                  /var/run/secrets/zad/project-kek/key)
   |
   +-- config.age-private-key per projectbestand in de projects-repo
   |      |
   |      +-- de EIGEN sleutel van dat project, en daaronder al zijn andere geheimen
   |
   +-- projects/<project>/age-key.txt.age in de deployments-repo
          de back-up van diezelfde projectsleutel
```

De projectsleutels staan sinds RC-334 onder een eigen sleutel, de project-KEK. Dat is nodig omdat
de platformsleutel op meer plekken dan OPI staat (de CMP in de ArgoCD-repo-server, `rig-prd-ron`,
`local`-clusters, `security/key.txt` op werkplekken): wie hem had, opende daarmee elke
projectsleutel. De KEK is alleen in de OPI-pod. `repositories[].password` blijft op de
platformsleutel: ArgoCD leidt zijn repository-secrets ervan af.

`wrap_project_key` en `unwrap_project_key` in `opi/utils/age.py` zijn de naad voor alle in- en
uitpakwerk. Dat is voor een latere externe sleutelbeheerder (OpenBao Transit, issue #219):
die vervangt alleen hun binnenkant. `unwrap_project_key` probeert de KEK en valt terug op de
platformsleutel, zodat onomgezette projectbestanden leesbaar blijven. Zonder KEK werkt
alles als voor RC-334, dus de sandbox hoeft niets.

De KEK is per cluster een andere sleutel, met zijn eigen bestand
`security/project-kek-<cluster>.txt`. Staging draait op dezelfde OpenShift-cluster als productie
(`features/zad-staging.md`) en is er voor een pentest, dus een gedeelde KEK zou de staging-OPI
elk productieproject laten uitpakken. Het gereedschap leest het cluster uit `--cluster`, of uit `CLUSTER_TYPE` die
`task select-cluster` zet.

De omzetting naar de KEK gaat in twee stappen, in deze orde: eerst
`set-sops-key-secret.py --target project-kek --cluster <cluster>`, daarna
`rewrap-project-keys.py` over beide clones. Het script eindigt met een controle dat geen
projectbestand en geen back-up nog met de platformsleutel opengaat.

### Waar de KEK bewaard wordt, en wat verlies betekent

Na de omzetting is de KEK de ENIGE sleutel die de projectsleutels opent, en daarmee elk geheim
van elk project. Hij staat in het cluster alleen als het secret `project-kek`, en bewust niet in
git. Raakt hij kwijt zonder kopie, dan is elke `config.age-private-key` onleesbaar en daarmee
elk projectgeheim eronder: wachtwoorden, API-keys, user-env-vars. Opnieuw genereren helpt niet,
want een nieuwe KEK opent de oude ciphertext niet.

Daarom: de KEK staat als kopie in de **password manager van de beheerders**, buiten ZAD om, met
**minstens twee mensen** die erbij kunnen. Alleen de waarde van het KEK-bestand van dat cluster hoort
daar, met de datum en het publieke deel in de aantekening zodat een kopie te herkennen is zonder
hem te openen. Die kopie staat er VOOR `rewrap-project-keys.py` draait, niet erna: tussen de
eerste omgezette commit en het maken van de kopie is er anders een venster waarin verlies van de
pod de projectsleutels meeneemt.

Terugzetten uit die kopie:

1. Schrijf de waarde uit de password manager in `security/project-kek-<cluster>.txt`, en zet de
   rechten smal: `chmod 600`. Het gereedschap weigert een sleutelbestand dat ruimer staat.
2. `uv run --project operations-manager/python python scripts/set-sops-key-secret.py --target project-kek --cluster <cluster>`.
   Dat plaatst het secret `project-kek` opnieuw in de OPI-namespace van DAT cluster, uit
   `CLUSTER_CONFIG`, en herstart OPI daar, want de sleutel wordt eenmalig bij het opstarten
   gelezen.
3. Toets dat OPI de projecten weer opent: de detailpagina van een project toont zijn
   componenten, en `kubectl logs deployment/operations-manager` meldt geen
   `Could not unwrap the project private key`.

Tot stap 2 gedaan is, valt in- en uitpakken terug op de platformsleutel: onomgezette projecten
blijven leesbaar, omgezette niet, en een NIEUW project krijgt zijn sleutel onder de
platformsleutel. Dat laatste is de reden om een verdwenen KEK niet te laten staan.

### De KEK zelf roteren

Dezelfde aanleidingen als bij de platformsleutel: elk kwartaal, of eerder als iemand weggaat of
de sleutel bekend kan zijn. Hiervoor is geen apart gereedschap nodig, want `rewrap-project-keys.py`
pakt om van de sleutel in `--platform-key` naar de sleutel in `--kek`, en dat mag de vorige KEK
zijn in plaats van de platformsleutel:

1. Genereer een nieuwe KEK: `age-keygen -o security/project-kek-<cluster>-nieuw.txt`.
2. Draai de ronde eerst als dry-run en daarna echt, met de OUDE KEK als bron, over beide clones:
   `uv run --project operations-manager/python python scripts/rewrap-project-keys.py --cluster <cluster> --projects <clone>/projects --deployments <clone-deployments> --platform-key security/project-kek-<cluster>.txt --kek security/project-kek-<cluster>-nieuw.txt`.
   De eindcontrole zegt dan dat geen projectbestand en geen back-up nog met de OUDE KEK opengaat.
   De fingerprint hasht de platte tekst, dus hij hoeft niet weg: een omverpakking laat hem gelijk.
3. Plaats de nieuwe KEK met `set-sops-key-secret.py --target project-kek --cluster <cluster>`
   en push daarna de twee clones.
4. Vervang de kopie in de password manager door de nieuwe, en gooi de oude pas weg als stap 3
   gemeten is.

Eén ding kan de bestaande ronde niet, en dat is bewust niet bijgebouwd: `unwrap_project_key`
probeert de KEK en valt terug op de PLATFORMSLEUTEL, niet op een tweede KEK. Tussen het plaatsen
van de nieuwe KEK en de push van de omgezette bestanden opent OPI dus de projectsleutels die nog
onder de oude KEK staan niet. Houd die twee stappen daarom bij elkaar, en doe de rotatie op een
moment dat een korte onderbreking mag. Een tweede sleutel in de kandidatenlijst zou dat venster
dichten, maar zet ook een afgedankte KEK weer in gebruik zolang hij er staat.

De aantallen staan bewust nergens: het gereedschap selecteert op recipient en op die lijst, en telt zelf. Een dry-run noemt wat hij gevonden heeft, en dat is het actuele getal.

Buiten scope: het vervangen van een projectsleutel zelf (issue #183) en de rest van de
zad-deployments-repo, die op de projectsleutel staat.

## Het gereedschap

| script | doet |
|---|---|
| `scripts/rotate-sops-key.py` | de SOPS-bestanden op deze recipient, de losse waarden en `projects/` in DEZE repo, plus met `--argo-applications` de SOPS-bestanden in een clone van de argo-applicatierepo |
| `scripts/rotate-project-keys.py` | de projectbestanden in een clone van de projects-repo |
| `scripts/replace-git-pat.py` | de PAT-ronde: dezelfde projectbestanden, plus de losse waarden die de token DRAGEN en de ArgoCD repository-secrets |
| `scripts/set-sops-key-secret.py` | het k8s-secret wisselen en de operations-manager herstarten; met `--target project-kek` de KEK van het gekozen cluster plaatsen in de OPI-namespace van dat cluster |
| `scripts/rewrap-project-keys.py` | `config.age-private-key` en de `age-key.txt.age`-back-ups van de platformsleutel naar de project-KEK |
| `scripts/scan-secrets.py` | de guard: weigert een commit, een branch of een historie met een geheim |

De streepjesnamen zijn dunne ingangen; de logica staat in modules ernaast, zie `scripts/README.md`. Losse handelingen eronder staan in `docs/sops-en-age-met-de-hand.md`.

Op de productie-overlays staat een tweede laag: `test_productie_overlay_geen_platte_secrets.py` weigert een `kind: Secret` onder `**/overlays/odcn*/` dat niet SOPS-versleuteld is. De secret-scan kan dat niet vangen (die zoekt sleutelVORMEN, en korte wachtwoorden hebben geen vorm), dus deze toets kijkt naar de documentsoort en draait in CI.

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

# en de argo-clone moet een VOLLEDIGE render geven, met dezelfde mappenkeuze als de plugin
# gebruikt. De vlaggen zijn hier wel nodig: met de hand draait ksops, de plugin niet meer
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
# de namen en de namespace verschillen per cluster, dus zet ze hier een keer
NS=rig-prd-operations; INFRA=production-infrastructure   # productie
# NS=rig-system;       INFRA=sandbox-infrastructure      # sandbox

# 5. pushen, alle drie de repo's. De commit in de argo-clone moet ONDERTEKEND zijn, zie
#    hieronder. Dan de configmap van OPI, en dan pas het secret: die volgorde luistert
SOPS_AGE_KEY="$(sed -n '3p' security/key.txt)" kustomize build --enable-alpha-plugins --enable-exec \
  bootstrap/rig-system/kustomize/overlays/odcn-production --load-restrictor LoadRestrictionsNone |
  yq 'select(.kind == "ConfigMap" and .metadata.name == "operations-manager-config")' |
  kubectl apply -n $NS -f -

uv run --project operations-manager/python python scripts/set-sops-key-secret.py --dry-run
uv run --project operations-manager/python python scripts/set-sops-key-secret.py

# en ArgoCD de eerste render met de nieuwe sleutel laten doen terwijl je kijkt
kubectl annotate application $INFRA -n $NS argocd.argoproj.io/refresh=hard --overwrite
kubectl annotate application user-applications -n $NS argocd.argoproj.io/refresh=hard --overwrite
kubectl annotate application ron-infrastructure -n $NS argocd.argoproj.io/refresh=hard --overwrite
```

**Namespaces en namen verschillen per cluster, en de bronbestanden helpen je daar niet mee.** De overlay van productie zet met een namespace-transformer alles naar `rig-prd-operations`, de sandbox doet dat niet. Wat in `bootstrap/rig-system/...` `rig-system` heet, is dus de sandboxnaam die op productie hernoemd wordt.

| | sandbox | productie |
|---|---|---|
| de Applications staan in | `rig-system` | `rig-prd-operations` |
| de infra-app heet | `sandbox-infrastructure` | `production-infrastructure` |
| OPI draait in | `rig-system` | `rig-prd-operations` |
| de mailrelay deployt naar | `rig-ron` | `rig-prd-ron` |


**Lees de dry-run van stap 5 regel voor regel: het zijn er MEER dan een.** `sops-age-key` is geen secret in een vaste namespace maar een naam die in veel namespaces voorkomt, want de plugin leest hem uit de namespace waar de applicatie naartoe deployt. Op productie draagt er sinds RC-351 een de platformsleutel: `rig-prd-operations`. Alle andere dragen een eigen projectsleutel en moeten met rust blijven. Draait er nog een cluster met de oude kopie in `rig-prd-ron`, dan komt die in de dry-run ook langs; die mag na de rotatie weg. Het script bepaalt dat verschil door te meten welke de OUDE publieke sleutel dragen; die twee lijsten zijn je controle. Het contract erachter staat in `instructions/sops-sleutel-in-het-cluster.md`.

De handmatige sync is er omdat de tenant-sidecar het secret bij ELKE render leest: de eerste render na de wissel is het bewijs dat het goed staat.

**De drie platform-apps hierboven renderen niet uit dat secret maar uit een mount** (de `cmp-platform`-sidecar, sinds RC-351). Een gemount Secret komt pas in de container aan nadat kubelet hem heeft ververst, dus een hard refresh direct na de wissel kan daar nog op de OUDE sleutel draaien. Herstart de repo-server voordat je die drie weegt:

```bash
kubectl -n $NS rollout restart deployment/argocd-repo-server
kubectl -n $NS rollout status deployment/argocd-repo-server
```

**Onderteken de commit in de argo-clone met een sleutel die ArgoCD vertrouwt.** Het AppProject van het app-of-apps eist een handtekening (`features/ondertekende-gitops-commits.md`); zodra ArgoCD die eis afdwingt, blokkeert een ongetekende commit op `main` van die repo de sync van ALLE project-Applications tot er een ondertekende commit op staat. Commit met `commit.gpgsign` aan en een `user.signingkey` die je eerst met `task argocd:trust-public-key KEY_FILE=<je publieke sleutel>` hebt laten vertrouwen; `git log --format='%G?' -1` is de controle voor je pusht.

**Waarom de configmap vóór het secret moet, en waarom hij altijd mee moet.** De twee git-wachtwoorden staan in `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/configmap.yaml` als `base64+age:`-waarden, en OPI ontsleutelt ze TIJDENS HET DRAAIEN met de sleutel uit `sops-age-key` (`opi/connectors/git.py:134`). Die map wordt door niemand gesynct; hij komt het cluster in via `kubectl apply`.

Het gaat dus niet om de platte inhoud maar om de ciphertext die in het cluster staat:

```
nu            sleutel A + configmap op A     werkt
alleen secret sleutel B + configmap op A     KAPOT, OPI kan zijn git-wachtwoord niet lezen
na de apply   sleutel B + configmap op B     werkt
```

Daarom hoort deze stap bij ELKE rotatie en niet alleen bij een tokenronde, en daarom staat hij vóór `set-sops-key-secret.py`: dat script herstart OPI zelf, en die herstart moet de nieuwe configmap al aantreffen.

Alleen die ene configmap, niet de hele overlay: die draagt ook de ArgoCD-CR, de drie Applications en de OPI-deployment, en dat zou een uitrol zijn vermomd als rotatiestap.

### VERIFY-2 -- werkt alles nog

```bash
# 6. de rooktest en de eindtoets
kubectl -n $NS get applications -o wide
kubectl -n $NS rollout status deployment/operations-manager
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

Vier lagen beschermen tegen geheimen in git: de pre-commit hook (lokaal), de job `secret-scan` in de security-workflow (bindend voor de branch), `test_this_repository_is_clean_right_now` in `tests/test_secret_scan.py` (dezelfde boomscan, buiten GitHub), en GitHub push protection (op de server).

De boomscan ziet alleen de huidige boom. De geschiedenis scant de job `secret-scan-history` wekelijks op een volledige clone (`fetch-depth: 0`), omdat die minuten duurt. De baseline in `scripts/secret-scan-baseline.json` trekt ingetrokken treffers af. Zonder die lijst zou de job elke week rood zijn, en een alarmbalk die altijd rood is, niemand opent. Voor de details: `scripts/README.md`.

Een AGE-kandidaat telt alleen als `age-keygen` hem als geldige sleutel accepteert. Zonder die controle zou elke placeholder in de testsuite een bevinding zijn, en een alarm met bekende bevindingen erin is een alarm waar mensen omheen leren lopen. Daarom staat er ook geen vaste sleutel meer in een toets: wie er een nodig heeft maakt er een.

De scan meldt wat hij NIET gelezen heeft, met de reden. Een melding die zwijgt over overgeslagen bestanden leest als groen terwijl er niet gekeken is.

## Wat hierna komt

- **De sleutels per project** (#183): die staan los van deze ronde en hebben hun eigen hiërarchie.
- **De oefenronde**: PREPARE en VERIFY-1 maandelijks op een wegwerpsleutel, met het resultaat weggegooid, zodat de ingreep geoefend blijft zonder dat er iets productie raakt. Een voorstel; er draait nog geen workflow voor.
- **De kubectl-aanroepen** via een library of de API in plaats van de commandline (#182).
