# De sleutel en het token vervangen, met een script dat omzet en aantoont dat de oude sleutel niets meer opent

De GitHub-PAT verloopt en moet vervangen worden. We nemen die rotatie als aanleiding om tegelijk
de AGE-sleutel te roteren en er een terugkerende handeling van te maken. Het is dezelfde ronde:
dezelfde bestanden, dezelfde lus, dezelfde verificatie.

De onderbouwing staat in BIO2 v1.3, control 8.24 (Gebruik van cryptografie):

* 8.24.01 vraagt een cryptografiebeleid waarin onder meer staat wie verantwoordelijk is voor het
  sleutelbeheer en "hoe geregistreerd wordt waar welke cryptografie toegepast wordt". De
  vindplaatsenlijst hieronder is die registratie; de dekkingsgrendel onder "De vorm is niet de
  vindplaats" houdt hem eerlijk, en in CI draait hij elke toetsronde mee omdat de runner `age` en
  `sops` installeert en `tests/test_rotation_ci_wiring.py` dat vasthoudt.
* 8.24.02 vraagt dat is vastgesteld waar cryptografische beheersmaatregelen worden ingezet, wie
  verantwoordelijk is "en hoe ze actueel worden gehouden". Dat laatste ontbrak: een AGE-sleutel
  kent geen verlooptijd, een token dwingt zijn eigen vervanging af.

Het gereedschap hoort erbij, omdat de platform-AGE-sleutel niet alleen in de SOPS-bestanden zit en
`sops rotate` alleen die ziet: dit zet elke vindplaats om, toont aan dat er niets anders is
veranderd, en toont aan dat de oude sleutel daarna niets meer opent.

Het ritme hieronder is een afspraak, het gereedschap is de mogelijkheid: het omzetten, de
vingerafdruk en de eindtoets zijn geautomatiseerd, dus een ronde hoeft niet op het kwartaal te
wachten. De rest is handwerk en blijft dat: de renderlus en de commitinspectie van VERIFY-1, de
push en de handmatige sync van APPLY, en de rooktest van VERIFY-2. Waarom die grens er zit en geen
achterstand is, staat onder "De oefenronde".

## Het ritme: preventief elk kwartaal, incidenteel bij aanleiding

Het kwartaalmoment is het moment dat de PAT toch vervangen moet worden. Een aanleiding daarbuiten
is een collega die weggaat, of het vermoeden dat een sleutel bekend is geworden.

Voor de EERSTE ronde gaan de sleutel en de PAT niet samen: daar gaat de sleutel voorop en de PAT
erachteraan; zie "Waarom de PAT-ronde in de EERSTE ronde achteraan staat". Zodra die ronde
aantoonbaar goed is gegaan, is de kwartaalronde de gecombineerde.

Tussen de rondes door KUNNEN PREPARE en VERIFY-1 maandelijks als oefening draaien, een ronde die
niets omzet. Die is nog niet gebouwd: er staat geen geplande workflow die hem draait. Zie "De
oefenronde" en "Wat hierna komt".

## Wat de sleutel vasthoudt

```
platformsleutel  (security/key.txt = k8s secret `sops-age-key` = SOPS_AGE_KEY_CONTENT)
   |
   +-- 21 SOPS-bestanden in bootstrap/ en infrastructure/
   |
   +-- 8 losse base64+age:-waarden, en NIET alleen in een env-regel
   |      de twee configmap.yaml (odcn-production en local) en
   |      operations-manager/python/.env, elk met GIT_PROJECTS_SERVER_PASSWORD
   |      en GIT_ARGO_APPLICATIONS_PASSWORD -- dat zijn er 6. De andere twee staan
   |      als PYTHON-literal: opi/core/config.py:238 (de default van
   |      PROJECT_REPO_PASSWORD) en scripts/migrate_project_to_production.py:66,
   |      dezelfde waarde. Zie "De vorm is niet de vindplaats" hieronder.
   |
   +-- projects/age-secret-github.txt
   |      een heel bestand dat EEN age-blok is, zonder sleutelregel om aan te haken.
   |      Staat er sinds de eerste commit en niets in de boom noemt hem, maar hij
   |      opent met de platformsleutel, dus hij gaat mee.
   |
   +-- de ArgoCD repository-secrets in zad-argo-user-applications
   |      SOPS-bestanden in een ANDERE repo. Gemeten: argo_manager.py:267 en :790
   |      schrijven ze met encrypt_to_sops_files_or_fail(..., SOPS_AGE_PUBLIC_KEY),
   |      dus op de PLATFORMsleutel, en de sops-plugin naast ArgoCD rendert ze met
   |      precies het secret dat stap 5 vervangt. Blijven ze achter, dan stopt het
   |      renderen op het moment dat de nieuwe sleutel in het cluster staat.
   |      --argo-applications <clone> neemt ze mee.
   |
   +-- config.age-private-key van ELK project
   |      daaronder hangt alles binnen dat project: Keycloak-wachtwoorden, api-key,
   |      user-env-vars, de bijlagen -- want die zijn met de PROJECTsleutel versleuteld
   |      en die staat hier versleuteld in
   |
   +-- repositories[].password van ELK project
   |      de GitHub-PAT. Gemeten: `git.py` gebruikt `decrypt_password_smart_auto_sync`,
   |      en die leest `get_global_private_key()` -- dus de PLATFORMsleutel en NIET de
   |      sleutel van het project.
   |
   +-- projects/simple-example.yaml in DEZE repo
          een vindplaats die het plan niet noemt. Gemeten door elk versleuteld
          veld in tests/ en projects/ met de platformsleutel te proberen: exact een
          treffer, een repositories[].password. Het is een projectbestand, dus de motor
          zet het om, maar het staat HIER: `rotate-project-keys.py` op een clone van
          zad-projects komt er nooit langs. `rotate-sops-key.py` doet het daarom mee,
          en de eindtoets loopt het na.
```

Gemeten over de 53 projectbestanden: precies deze twee velden gaan open met de platformsleutel
(53 + 53 = 106 velden). De 214 andere versleutelde waarden in diezelfde bestanden hangen aan de
sleutel van het project zelf.

**Het zijn er 53 en niet 45.** De projects-repo houdt een eerdere ronde in `projects/local-old/`:
8 bestanden met 16 platformvelden. Een platte `glob('*.yaml')` laat die staan, en de ronde meldt
dat niet, want de vingerafdruk waar hij tegen vergelijkt komt uit diezelfde selectie -- 90 tegen
90, CLEAN, terwijl de oude sleutel die 16 velden gewoon opent. De selectie is daarom `rglob`, en
de dekkingsgrendel uit "De vorm is niet de vindplaats" loopt ook over de clone: die meet op wat
git bijhoudt en niet op de selectie, dus een `.yml`, een bestand zonder extensie of een map die
er vandaag nog niet is komt er ook uit.

De sleutelwaarde staat nergens in git: `deployment.yaml` verwijst met `secretKeyRef` naar het
secret `sops-age-key`, en `sops-plugin.sh` leest datzelfde secret. Het is een bootstrapwaarde die
alleen in Kubernetes leeft.

## Het gereedschap

| script | doet |
|---|---|
| `scripts/rotate-sops-key.py` | de 21 SOPS-bestanden, de 9 losse waarden en `projects/` in DEZE repo, plus met `--argo-applications` de SOPS-bestanden in een clone van zad-argo-user-applications |
| `scripts/rotate-project-keys.py` | de projectbestanden in een clone van de projects-repo |
| `scripts/replace-git-pat.py` | dezelfde ronde, met de PAT er ook vervangen |
| `scripts/set-sops-key-secret.py` | het k8s-secret wisselen en de operations-manager herstarten |
| `scripts/scan-secrets.py` | de grendel: weigert een commit, een branch of een historie met een geheim |

De streepjesnamen hierboven zijn dunne ingangen; de logica staat in modules ernaast. Zie
`scripts/README.md`.

## Gebruik

**Dit document is de bron, de scripts zijn de uitvoering.** De stappen staan hier met de reden
erbij, zodat je ze ook met de hand kunt draaien; wijkt een script af van wat hier staat, dan is
dat een fout in het script. De losse handelingen eronder staan in
`docs/sops-en-age-met-de-hand.md`.

De regels hieronder draai je vanaf de repowortel en je plakt ze heel: het voorvoegsel hoort bij
het commando. Waarom, en waarom `scan-secrets.py` er anders uitziet, staat in `scripts/README.md`.

De ronde heeft vier fasen, en de grens die telt zit tussen VERIFY-1 en APPLY. Tot daar raakt niets
productie en blijft alles op de oude sleutel werken, dus afbreken kost niet meer dan een paar
weggegooide clones. Daarna is het een kort venster waarin de bestanden en het cluster allebei om
moeten.

### PREPARE -- herversleutelen, en niets versturen

```bash
# 1. sleutel B maken en de namen op hun plek zetten. --generate-new-key roept age-keygen zelf
#    aan; --rename vraagt naar de twee paden (antwoord security/key.txt en security/nieuw.txt)
#    en schuift ze daarna naar old_key.txt en key.txt
uv run --project operations-manager/python python scripts/rotate-sops-key.py --rename --generate-new-key
# met de hand: age-keygen -o security/nieuw.txt, en dan hetzelfde zonder --generate-new-key

# 2. deze repo EN de argo-applicatierepo: eerst kijken, dan doen
git clone <zad-argo-user-applications> /tmp/zad-argo
uv run --project operations-manager/python python scripts/rotate-sops-key.py --argo-applications /tmp/zad-argo --dry-run
uv run --project operations-manager/python python scripts/rotate-sops-key.py --argo-applications /tmp/zad-argo

# 3. de projectbestanden, op een VERSE clone. Dit is de EERSTE ronde, dus alleen de sleutel; de
#    PAT volgt in stap 7. In een kwartaalronde draai je deze twee regels met replace-git-pat.py
#    in plaats van rotate-project-keys.py -- dezelfde vlaggen, dezelfde clone -- en vervalt
#    stap 7. Zie "Waarom de PAT-ronde in de EERSTE ronde achteraan staat".
git clone <zad-projects> /tmp/zad-projects
uv run --project operations-manager/python python scripts/rotate-project-keys.py --projects /tmp/zad-projects/projects --dry-run
uv run --project operations-manager/python python scripts/rotate-project-keys.py --projects /tmp/zad-projects/projects
```

Stap 2 laat zijn wijzigingen in de werkboom staan, in deze repo en in `/tmp/zad-argo`: commit ze
daar allebei zelf, zonder push. Stap 3 commit wel, een commit per project, ook zonder push.

### VERIFY-1 -- het go/no-go moment, voor de push

```bash
# 4. alle drie de repo's nalopen TERWIJL er nog niets gepusht is
uv run --project operations-manager/python python scripts/rotate-sops-key.py --assert-old-key-dead --projects /tmp/zad-projects/projects --argo-applications /tmp/zad-argo
git -C /tmp/zad-projects log --oneline | head
git -C /tmp/zad-projects diff --stat origin/HEAD

# en: de argo-clone moet een VOLLEDIGE render geven en niet een halve. Dit is wat de plugin
# doet: dezelfde vlaggen, en dezelfde mappenkeuze als KUSTOMIZE_FOLDERS=subfolders
export SOPS_AGE_KEY="$(sed -n '3p' security/key.txt)"
find /tmp/zad-argo -mindepth 2 -name kustomization.yaml -exec dirname {} \; | sort -u |
  while read -r folder; do
    kustomize build --enable-alpha-plugins --enable-exec --enable-helm "$folder" > /dev/null ||
      echo "RENDER FAALT: $folder"
  done
unset SOPS_AGE_KEY
```

Dezelfde eindtoets als na de cutover, over alle drie de repo's. Hij praat niet met het cluster,
dus hij mag hier al; wat hij eist staat onder "De eindtoets".

De kustomize-lus dekt iets anders dan de vingerafdruk: een bestand kan prima ontsleutelen en de
render toch laten stranden of leeglopen. De vlaggen en de mappenkeuze komen uit
`bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml`; `kustomize` en `ksops` moeten op je
PATH staan, zoals in het plugin-image (`images/cmp-kustomize-sops/Dockerfile`).

Gaat hier iets rood, dan gooi je de clones weg en begin je opnieuw.

### APPLY -- het korte venster

```bash
# 5. pushen, alle drie de repo's, en METEEN daarna het secret wisselen
uv run --project operations-manager/python python scripts/set-sops-key-secret.py --dry-run
uv run --project operations-manager/python python scripts/set-sops-key-secret.py

# en ArgoCD de eerste render met de nieuwe sleutel laten doen terwijl je kijkt
kubectl annotate application production-infrastructure -n rig-system argocd.argoproj.io/refresh=hard --overwrite
kubectl annotate application user-applications -n rig-system argocd.argoproj.io/refresh=hard --overwrite
```

`set-sops-key-secret.py` doet zelf de herstart, en die kost **geen nieuwe image**. De sleutel komt
via `env.valueFrom.secretKeyRef` bij OPI binnen
(`bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml:117`), op de vaste naam
`sops-age-key`; er staat geen `secretGenerator` in `bootstrap/`, dus ook geen hash-achtervoegsel
dat het manifest zou veranderen. Een `kubectl rollout restart deployment/operations-manager` is
genoeg, en dat is precies wat het script draait, met `rollout status` erachter.

De handmatige sync is er omdat de plugin het secret bij ELKE render leest: de eerste render na de
wissel is het bewijs dat het goed staat. Forceer hem dus nu, terwijl je meekijkt, in plaats van
hem bij de eerstvolgende willekeurige sync tegen te komen.

### VERIFY-2 -- werkt alles nog

```bash
# 6. de rooktest en de eindtoets
kubectl -n rig-system get applications -o wide
kubectl -n rig-prd-operations rollout status deployment/operations-manager
uv run --project operations-manager/python python scripts/rotate-sops-key.py --assert-old-key-dead --projects /tmp/zad-projects/projects --argo-applications /tmp/zad-argo
```

Drie dingen moeten kloppen, en ze raken elk een andere lezer van de sleutel: **ArgoCD rendert**
(elke Application Synced en Healthy, geen `ComparisonError`), **OPI leest een sops-bestand** (open
een projectdetailpagina in het portaal, want die ontsleutelt `config.age-private-key`), en **een
project haalt zijn repository op** (draai een deployment-actie op een project, want die leest
`repositories[].password`).

De eindtoets erachter is de harde: de oude sleutel opent niets meer.

### Daarna

```bash
# 7. de PAT-vervanging van de EERSTE ronde: dezelfde ronde, een ingang verder -- en pas NU.
#    Een kwartaalronde deed dit al in stap 3 en slaat deze stap over. Verse clone, want er
#    kan sinds stap 3 gepusht zijn, maar op DEZELFDE plek: de opname noemt elk veld bij zijn pad
rm -rf /tmp/zad-projects && git clone <zad-projects> /tmp/zad-projects
uv run --project operations-manager/python python scripts/replace-git-pat.py --projects /tmp/zad-projects/projects --dry-run
uv run --project operations-manager/python python scripts/replace-git-pat.py --projects /tmp/zad-projects/projects

# 8. een dag later de eindtoets nog een keer, en dan pas mag de oude sleutel weg
uv run --project operations-manager/python python scripts/rotate-sops-key.py --remove-old-key --projects /tmp/zad-projects/projects --argo-applications /tmp/zad-argo
```

**Waarom de PAT-ronde in de EERSTE ronde achteraan staat, ook al is hij de aanleiding.** Ze kunnen
in een keer: de motor onder beide is dezelfde lus en `replace-git-pat.py` zet de sleutel en het
wachtwoord in een beweging om. Toch is de geadviseerde volgorde voor die eerste ronde de sleutel
eerst en de PAT daarna, want een fout in de PAT-vervanging sleept dan de sleutelrotatie niet mee
en de twee zijn los terug te draaien. Is die ronde aantoonbaar goed gegaan, dan is de PAT-ronde
een herhaling van iets dat al gewerkt heeft, en draait een kwartaalronde ze samen zoals stap 3
beschrijft.

Twee dingen die daaruit volgen:

* de losse PAT-ronde neemt nog steeds **beide** sleutels aan, ook al staat er na stap 3 geen veld
  op de oude. Ze mogen niet dezelfde zijn, dus `security/old_key.txt` moet nog bestaan -- vandaar
  dat het weghalen daarvan stap 8 is en niet stap 7;
* een tweede PAT-ronde doet niets: een wachtwoord dat het meegegeven token al draagt blijft staan,
  ciphertext en al.

**Randvoorwaarde, en die is hard:** de nieuwe PAT moet al geldig zijn op GitHub voordat het eerste
bestand wordt geschreven, met de oude er nog naast (zie "Geen dubbele recipients" voor waarom dat
bij een PAT wel kan en bij de sleutel niet). Anders verliest een project zijn repositorytoegang
zodra zijn bestand om is en de rest nog niet. Trek de oude pas in nadat de clone gepusht is en een
project aantoonbaar zijn repository haalt.

Geen enkel script neemt een sleutel als argument. Ze vragen naar het PAD, met een default, en
lezen uit `security/` -- die map staat in `.gitignore`. Een sleutel op de commandoregel belandt in
de shellgeschiedenis, in de procestabel en in elk logboek dat het commando meeschrijft. Bij
`--generate-new-key` draait die vraag om: het pad moet er juist NIET zijn. Een bestaand bestand is
een weigering en geen overschrijving, want de default van die vraag is `security/key.txt` -- de
sleutel die op dat moment alles nog opent, en waarvan geen kopie bestaat.

Droogloop is de veilige stand: zonder `--ja` wordt er geen byte geschreven voordat je op
"Run this?" ja hebt gezegd, en `--dry-run` stelt die vraag niet eens.

## De oefenronde: automatiseer de oefening, niet de ingreep

PREPARE en VERIFY-1 zijn precies de twee fasen waarin niets deze machine verlaat. Ze zetten om en
ze controleren, maar er wordt niet gepusht en het clustersecret blijft staan. Afbreken kost dus
niets buiten deze machine, maar wel wat er op staat: de clones weggooien, de wijzigingen die stap
2 in deze werkboom en in `/tmp/zad-argo` laat staan terugdraaien, en de hernoeming van stap 1
omkeren zodat `security/key.txt` weer de sleutel is die alles opent. In CI is dat een verse
checkout en dus gratis: die twee fasen kunnen daar maandelijks draaien op een wegwerpsleutel, met
het resultaat weggegooid.

Maandelijks is met opzet VAKER dan de rotatie zelf. Een kwartaalronde mag nooit de eerste keer
zijn dat iemand merkt dat er iets stuk is.

Dat is geen rotatie maar een meting, en hij bewijst twee dingen die je anders pas ontdekt op het
moment dat je ze nodig hebt:

* **het gereedschap werkt nog.** Een verschoven API of een `sops` die zich anders gedraagt valt
  hier om, in een ronde die niets kapot kan maken, en niet halverwege een echte;
* **er is geen vindplaats bijgekomen die niemand heeft aangemeld.** Dat is de stille fout: iemand
  zet een nieuwe versleutelde waarde neer, de inventaris weet er niet van, en de eerstvolgende
  rotatie laat hem op de oude sleutel staan. De grendel onder "De vorm is niet de vindplaats"
  vangt dat binnen deze repo; een oefenronde vangt ook de andere twee.

APPLY blijft mensenwerk, en dat is een grens en geen achterstand. Een half omgezet platform om
vier uur 's ochtends, met niemand die het alarm leest, kost meer dan de handeling die je ermee
zou besparen.

## De vingerafdruk is het echte product

De eis is dat je achteraf kunt aantonen dat er niets is veranderd behalve de sleutel. "Het script
gaf geen fout" is dat niet.

```
VOOR   per veld: ontsleutel -> sha256 van de PLATTE waarde -> fingerprint.json
NA     per veld: ontsleutel met de nieuwe sleutel -> sha256 -> vergelijk
```

Dat werkt omdat de ciphertext bij elke omzetting verandert en de platte inhoud niet. Een
vergelijking van de versleutelde vorm zegt dus niets; de hash van de inhoud zegt alles. In het
bestand staat nooit een geheim: alleen een pad, een veldnaam en een hash. Het komt in
`security/`, dus buiten versiebeheer.

Wat het afdekt: elk veld is nog leesbaar, elke waarde is ongewijzigd, en er is niets kwijt (het
aantal velden voor en na moet gelijk zijn -- een veld dat stil verdwijnt tijdens een YAML-ronde
valt hier door de mand). Bij een PAT-vervanging hoort de hash juist te verschillen, en dan alleen
bij de wachtwoordvelden; het script noemt ze vooraf.

`--verify` werkt los en vraagt de oude sleutel niet op, dus je kunt maanden later nog nagaan of
alles nog klopt -- ook als de clones van toen allang weg zijn:

```bash
git clone <zad-argo-user-applications> /tmp/zad-argo
uv run --project operations-manager/python python scripts/rotate-sops-key.py --verify --argo-applications /tmp/zad-argo
```

De clone hoort erbij: de vingerafdruk van stap 2 dekt deze repo EN de argo-applicatierepo, dus
zonder `--argo-applications` mist hij elk veld daaruit en meldt hij dat als "field disappeared".

Heb je de projectenclone nog, geef hem dan mee: `--projects <clone>/projects` vergelijkt daar de
opname van `rotate-project-keys.py` naast, en de CLEAN-regel telt die velden dan mee. Zonder die
vlag gaat het over deze repo en de argo-clone. De vlag eist wel een opname om tegen te vergelijken
(`--projects-fingerprint`, standaard `security/projects-fingerprint.json`); is die er niet, dan
weigert hij in plaats van CLEAN te zeggen over een clone die hij alleen maar heeft opengemaakt.

Een ronde over de projectbestanden mag stranden op een onleesbaar bestand: de rest wordt wel
gedaan en het script gaat rood. Repareer dat bestand en draai dezelfde ronde nog een keer.
`rotate-project-keys.py` meet zijn vingerafdruk over ALLE projectbestanden en niet over wat die
ene ronde omzette, dus de telling van de eindtoets klopt ongeacht in hoeveel rondes het lukte.

Een opname die er al staat wordt eerst VERGELEKEN en dan pas vervangen, op de velden die in
allebei voorkomen. Dat een veld erbij komt of afvalt mag: een gerepareerd bestand dat nu wel
leesbaar is, een project dat sindsdien weg is, of de argo-clone die er de vorige keer niet bij
zat. Verschilt een veld dat beide opnames kennen van inhoud, dan stopt de ronde en blijft de oude
opname staan -- dat is het bewijs.

De PAT-ronde werkt diezelfde projectopname bij en niet een tweede ernaast, en daarom draait stap 7
op dezelfde clonePLEK als stap 3. De wachtwoordvelden horen daar juist te verschillen en staan op
de lijst "vervangen"; `config.age-private-key` moet ook in die ronde gelijk blijven.

Wat het NIET garandeert: of de waarden zelf nog geldig zijn bij de tegenpartij. Of GitHub die PAT
nog accepteert valt hier niet mee te toetsen. Daarvoor is de rooktest na de cutover.

## De eindtoets: de rotatie is pas klaar als de oude sleutel niets meer opent

`--assert-old-key-dead` loopt over elke vindplaats en eist per veld:

- **ontsleutelen met de oude sleutel faalt.** Een enkele treffer betekent dat er iets is
  overgeslagen, en die noemt hij bij naam.
- **ontsleutelen met de nieuwe sleutel slaagt.** Anders is er iets omgezet naar een sleutel die
  niemand heeft.
- **de telling klopt** met de SOM van beide vingerafdrukken (deze repo + de projecten). De
  tweede komt van `rotate-project-keys.py`: `--projects-fingerprint` wijst standaard naar
  `security/projects-fingerprint.json`, precies waar dat script hem schrijft.

Zonder `--projects` loopt hij de vierde vindplaats niet na, en zonder `--argo-applications` de
vijfde niet, en allebei zegt hij dat. `--remove-old-key` weigert daarom zonder beide: er kan dan
een project op de oude sleutel staan, of een ArgoCD repository-secret dat na de wissel niet meer
rendert. Het weghalen van de sleutel is het punt waarna niets meer te repareren valt, dus een
schoon oordeel over vier van de vijf plaatsen is geen reden om hem weg te gooien.

Boven op die vijf loopt hij twee dingen na die over de DEKKING gaan en niet over een veld: een
bestand met cijfertekst dat nergens wordt omgezet is een FAIL en geen stilte, en de
uitzonderingslijst wordt met de oude sleutel nagemeten. Waarom allebei: zie hieronder.

`--remove-old-key` haalt `security/old_key.txt` weg. `--verify` blijft daarna werken: die stand
meet met de nieuwe sleutel en vraagt de oude niet op.

## `sops rotate`, niet `updatekeys`

SOPS versleutelt de inhoud met een data key en versleutelt alleen die data key per recipient.

- `sops updatekeys` wisselt de recipients en laat de data key staan. Gemeten op een echt bestand:
  de versleutelde waarde bleef byte-voor-byte gelijk.
- `sops rotate` maakt een nieuwe data key. Diezelfde waarde werd een andere.

Wie de oude sleutel ooit had, heeft de data key uit een oude kopie kunnen halen, en die opent een
met `updatekeys` bijgewerkt bestand nog steeds. Het gereedschap gebruikt dus uitsluitend
`rotate -i --add-age B --rm-age A`.

## Geen dubbele recipients

Elk veld gaat van A naar B en A verdwijnt meteen. Het alternatief is overwogen: je kunt een
age-blob voor twee sleutels tegelijk versleutelen, zodat oud en nieuw allebei werken en er geen
omschakelmoment is.

Dat is afgewezen. Het hele punt van een rotatie is dat A daarna niets meer opent; een overlapfase
houdt A juist geldig en voegt een LOSSE ronde toe om hem er later weer af te halen. Precies zo'n
losse ronde is wat niets afdwingt -- zie 8.24.02 hierboven.

Bij de PAT is het precies andersom, en dat is geen inconsistentie: GitHub kent gewoon twee geldige
tokens naast elkaar, dus daar kost een overlap niets en voorkomt hij dat een project zijn
repositorytoegang verliest halverwege de ronde.

## Selectie op sleutel, nooit op naam

Twee keer dezelfde vondst, en het is de belangrijkste regel in dit gereedschap:

- **De SOPS-bestanden** worden geselecteerd op de `recipient` in hun metadata. Een
  uitzonderingslijst op paden loopt stil achter zodra er een bestand bijkomt, en er zijn meer
  sleutels in omloop (sandbox, developer).
- **Het k8s-secret `sops-age-key` staat in MEER dan een namespace.** Gemeten op het
  sandboxcluster: twaalf. Twee ervan dragen de platformsleutel (`rig-system` en `rig-ron`, want de
  CMP leest het secret in de namespace van de applicatie die hij rendert), en de andere tien elk
  een ANDERE, want OPI schrijft per project een eigen SOPS-sleutel in de projectnamespace. Elk
  `sops-age-key` overschrijven zou tien projecten hun eigen sleutel afnemen.

`set-sops-key-secret.py` leest daarom elk secret, leidt de publieke helft af, en raakt alleen de
namespaces aan die de OUDE platformsleutel dragen. De rest komt in de uitvoer te staan als "left
alone", met zijn eigen publieke sleutel ernaast.

## De vorm is niet de vindplaats, en de lijst is gegrendeld

Buiten een SOPS-bestand draagt cijfertekst zijn recipient niet in de tekst. Selecteren op sleutel
kan daar dus niet, en daar is de werklijst dan ook wat hij nergens anders is: een lijst paden. Die
liep achter. Een review mat de boom met de oude sleutel in plaats van deze lijst te lezen, en vond
drie gecommitte waarden die buiten ELKE vindplaats stonden -- zodat `--assert-old-key-dead` CLEAN
meldde terwijl de oude sleutel ze gewoon opende:

| wat | waarom het niet opviel |
|---|---|
| `opi/core/config.py:238` | een quoted PYTHON-literal. Het patroon was `^KEY=base64+age:...$`, en dat is een env-regel |
| `scripts/migrate_project_to_production.py:66` | dezelfde waarde als dict-entry, `"password": "base64+age:..."` |
| `projects/age-secret-github.txt` | een heel bestand dat EEN age-blok is: geen sleutelregel om een naam aan te ontlenen, geen regel om op te vervangen |

`config.py` is daarvan de harde: `PROJECT_REPO_PASSWORD` is geen voorbeeld. Hij wordt gelezen in
`opi/utils/project_utils.py` en ingevuld in `opi/configs/project-template.yaml`, en alleen
`sandboxed-local` zet hem over (`plain:admin1234`). `odcn-production` en `local` vallen terug op
deze default, dus na stap 5 kan productie die waarde niet meer ontsleutelen en faalt de eerste
projectaanmaak.

Het gereedschap kent nu beide vormen -- een `base64+age:`-waarde waar hij ook op zijn regel staat,
en een bestand dat zelf het blok is -- maar dat repareert de drie gevallen, niet de klasse. De
grendel daarvoor:

```
elk getrackt bestand met ECHTE cijfertekst, in ELKE boom die de ronde loopt
   -> wordt omgezet door een van de vindplaatsen
   OF staat op COVERAGE_EXCEPTIONS met een reden
   anders: FAIL, en de ronde begint niet eens
```

"In elke boom" is geen detail. De veegactie liep over deze repo en over de projectenclone, en
over de argo-clone niet -- terwijl `--remove-old-key` die clone juist EIST en er alleen
SOPS-bestanden in loopt. Een getrackt bestand daar met een losse waarde werd dus door geen
vindplaats bereikt en door geen vingerafdruk geteld, en de eindtoets zei CLEAN. Dat het in de
echte argo-repo goed gaat leunt op wat `argo_manager.py` daar schrijft, en dat is een bewering
over andere code: precies wat deze grendel er is om na te meten in plaats van na te vertellen.

Twee dingen maken dat bruikbaar in plaats van een lijst die verslapt:

**"Echte" cijfertekst, niet de vorm.** De armor wordt uitgepakt en de AGE-header gelezen: een echt
bestand begint met `age-encryption.org/v1` en draagt een recipient-stanza en een MAC-regel.
Gemeten op deze boom: **34 bestanden** dragen echte cijfertekst, **43** dragen alleen de vorm --
plaatshouders in toetsen (`base64+age:AAAA`) en ingekorte blokken in feature-documentatie. Zonder
de kop-controle zouden die negen er als uitzondering bij moeten, naast de zes echte. Dat is
dezelfde afweging als bij de scanner, die een AGE-kandidaat pas meldt als `age-keygen` hem
accepteert: een uitzonderingslijst vol regels waar niemand iets mee kan is hoe een grendel stil
verslapt.

**De uitzonderingen worden nagemeten.** Elke regel op die lijst is een met de hand geschreven
bewering dat die cijfertekst aan een andere sleutel hangt, en met de hand geschreven dekking is nu
juist wat misging. `--assert-old-key-dead` opent ze daarom ook met de OUDE sleutel en noemt er een
die antwoordt. Ze tellen niet mee in het veldenaantal: ze zijn nooit omgezet, dus ze staan niet in
de vingerafdruk, en meetellen zou de telling met precies het aantal excuses laten afwijken.

De grendel heeft zelf geen sleutel nodig, dus hij draait op elke droogloop. Zijn toetsen hebben er
wel een nodig: die staan in de rotatiemodules en slaan zonder `age` op je PATH in een keer over.
De nameting van de uitzonderingen heeft ook een sleutel nodig, en die hoort bij de eindtoets.

## Wie de sleutel wanneer leest

| consument | leest | bij een wissel |
|---|---|---|
| sops-plugin naast ArgoCD | `kubectl get secret` bij **elke render** | pakt de nieuwe vanzelf, geen herstart |
| operations-manager | env-var uit datzelfde secret | **pod moet herstarten** |
| ontwikkelaar lokaal | `security/key.txt` | bestand vervangen |

Het venster tussen "de bestanden staan op B" en "het secret is gewisseld" is klein: er draait geen
scheduler die uit zichzelf projecten verwerkt (`ENABLE_GIT_MONITOR` staat op `False`), dus OPI
leest pas als iemand iets doet. Houd het kort door pas te pushen als alles lokaal geverifieerd is,
en meteen daarna het secret te wisselen.

Terugdraaien als dat misgaat: zet het secret terug naar A en draai de commits terug. Dat is meer
werk dan bij een overlapfase, en dat is de prijs van de keuze hierboven.

## Waarom Python en niet shell

De SOPS-kant zou prima in shell kunnen. De projectbestanden niet, en dat gaf de doorslag. Gemeten
op de 53 bestanden:

- alle 53 dragen de sleutel als meerregelige YAML block scalar, waar de inspringing en de
  chomping-indicator exact moeten kloppen;
- in datzelfde bestand staat `repositories[].password` op EEN regel, 34 keer als `base64+age:` en
  19 keer als armored blok -- twee vormen door elkaar;
- 5 van de 53 hebben commentaar, dat een naieve YAML-ronde weggooit.

Tijdens het schrijven van het plan is de omzetting een keer met tekstvervanging geprobeerd, toen
nog over de platte 45: **56 van de 90 velden bleven stil op de oude sleutel staan**, zonder
foutmelding. Raak de projectbestanden dus nooit met sed, awk of `str.replace` aan. Het
gereedschap laadt en schrijft via `opi.utils.yaml_util`, de canonieke round-trip-schrijver, en
zet een meerregelige waarde terug als `LiteralScalarString`.

## Validatie: waarschuwen, niet blokkeren

Gemeten: **52 van de 53 projectbestanden voldoen niet aan het huidige schema (2.8)**. Ze zijn
ouder en worden pas bij lezen door OPI gemigreerd. Weigeren op die bestaande afwijking zou de hele
rotatie blokkeren op iets dat niets met de sleutel te maken heeft.

Dus: dezelfde twee validaties die `ProjectStore` voor elke schrijf draait, in dezelfde orde, voor
EN na de omzetting. Een bestand dat al rood stond gaat door met een melding; een bestand dat rood
WORDT door de omzetting wordt overgeslagen. `find_plaintext_secret_violations` blijft
onvoorwaardelijk hard -- dat is de fout waarbij een rotatie een geheim in platte vorm in git zou
achterlaten.

Een bestand met een onleesbare waarde breekt de ronde niet af. Anders staat de helft van de
projecten op de oude sleutel en de helft op de nieuwe, zonder dat ergens staat welke helft welke
is. Het wordt overgeslagen met de veldnaam in de melding, niet half weggeschreven, en de exitcode
is 1.

## De grendel: drie lagen

Er had nooit een commit met een geheim gemaakt kunnen worden. De valkuil is dat de voor de hand
liggende oplossing, een pre-commit hook, in dit project juist niet werkt: `--no-verify` is hier
staande praktijk, omdat de pre-commit alles stasht wat unstaged is en daarmee andere sessies in
dezelfde checkout omvergooit. Een grendel die met een vlag te omzeilen is, en die iedereen
dagelijks omzeilt, is geen grendel.

1. **Lokaal en vriendelijk.** De pre-commit hook (`secret-scan` in `.pre-commit-config.yaml`)
   scant de gestageerde bestanden. Vangt het eerlijke ongeluk, kost niets, blijft omzeilbaar.
2. **CI, bindend.** De job `secret-scan` in `.github/workflows/security.yml`, bij elke push naar
   main en elke pull request. Scant de hele BOOM van de branch en niet alleen de diff, zodat iets
   dat via een omweg binnenkomt alsnog opvalt. Dit is de laag die telt, want CI kent geen
   `--no-verify`.
3. **Op de server.** GitHub push protection op de publieke repo. Let op de grens: de
   standaardpatronen dekken bekende tokenvormen zoals een GitHub-PAT, maar `AGE-SECRET-KEY-` zit
   daar niet bij. Dat vraagt een eigen patroon, en of dat beschikbaar is hangt af van het
   abonnement. Nog uit te zoeken.

### Een AGE-kandidaat telt alleen als hij GELDIG is

De boom draagt ongeveer twintig plaatshouders (`AGE-SECRET-KEY-1TEST`, `-FAKE`, `-1PLAINTEXT`) die
geen geheim zijn en niet weg kunnen zonder die toetsen slechter te maken. Een scanner die op het
voorvoegsel alarmeert geeft dus meldingen die niemand hoeft op te lossen, en een alarm waar
iedereen omheen leert leven is geen alarm.

Daarom telt een AGE-kandidaat pas als `age-keygen -y` hem accepteert. Om dezelfde reden wordt een
JWT-kandidaat pas een melding als zijn header echt naar JSON met een `alg` decodeert, en een
PEM-blok pas als er ook een body onder de kopregel staat -- dat laatste haalde zes meldingen weg
die allemaal kopregels in testdata waren.

**Eerst de boom schoon, dan de grendel.** In die volgorde, anders bouw je een alarm dat vanaf dag
een rood staat.

### Wat er gescand wordt

AGE-privesleutels, GitHub-PAT's (`ghp_`, `github_pat_`, `gho_`/`ghu_`/`ghs_`/`ghr_`),
privesleutels in PEM-vorm met een body, JWT's, Slack-tokens en AWS-access-keys. De
`age:`/`base64+age:`-vorm die ZAD zelf schrijft is GEEN melding -- gecommitte ciphertext is
precies het punt van AGE -- maar met `--inventory` wel op te vragen, als inventaris van de plekken
die een rotatie moet raken.

En dat alles ook in base64. Een Kubernetes-secret codeert elke waarde, dus een manifest met de
platformsleutel erin draagt geen `AGE-SECRET-KEY-` die een tekstscan ziet: gemeten stond dezelfde
sleutel in een `.py` op exit 1 en in een `kind: Secret` onder `data.key` op CLEAN. Dat is geen
bedachte vorm maar de moedertaal van deze repo, en `docs/sops-en-age-met-de-hand.md` laat je er in
stap 2 een maken -- dat doc schrijft zijn sleutel en zijn manifest nu allebei naar `security/`,
juist omdat in de wortel geen van beide genegeerd is. De historie draagt er ook een:
`sops-sandbox/sops-secret-for-in-namespace.yaml` hield de oefensleutel base64 vast, en tot deze
ronde viel alleen het platte `sops-key.txt` ernaast op.

Daarom wordt elke regel twee keer gelezen: zoals hij staat, en met elke base64-reeks erop
uitgepakt. Eén laag diep, want een geheim onder twee rondes base64 is geen vorm die hier ontstaat.
Een melding uit de uitgepakte lezing houdt het regelnummer van de GECODEERDE regel, want dat is de
regel die uit de commit moet. De ciphertextinventaris draait alleen op de platte lezing: een
base64-reeks die naar ciphertext uitpakt is diezelfde ciphertext, en zou anders dubbel tellen.

`python3 scripts/scan-secrets.py --history` loopt elke blob die ooit in de repo heeft bestaan na. Dat is
een meting en geen opruiming: wat eruit komt bepaalt of er meer geroteerd moet worden.

**Die uitkomst gaat nooit naar git, ook niet samengevat**, en `scan-secrets.py` kent daarom geen
vlag die hem wegschrijft. Waarom, en waar de bevindingen dan wel horen: `scripts/README.md`.

Let op het onderscheid, want twee dingen heten hier "vindplaats" en maar een ervan is een
probleem:

| welke vindplaats | hoort in git? |
|---|---|
| WAAR CRYPTOGRAFIE WORDT TOEGEPAST: de inventaris die de ronde omzet, hierboven | **ja** -- BIO2 8.24.01 vraagt letterlijk om die registratie |
| WAAR GEHEIMEN IN DE HISTORIE STAAN: de uitkomst van `--history` | **nee** -- intern kanaal |

## Sleutels in toetsen: geen vaste, maar een gemaakte

Een vaste sleutel in een toets en een sleutel die roteert gaan niet samen. Hij gaat niet mee in de
ronde -- een toets houdt zijn eigen kopie vast -- en daarmee pint hij precies de waarde vast die
moet kunnen wisselen. Bij de eerstvolgende ronde is zo'n toets ofwel rood zonder dat er iets stuk
is, ofwel groen op een sleutel die nergens meer geldig is, en dat tweede is het vervelendste van
de twee.

Tot deze taak stonden er vier AGE-sleutels in de boom die `age-keygen` als echte sleutel
accepteert: de platformsleutel, twee die niets buiten hun eigen toetsdata openden, en de
oefensleutel van `sops-sandbox/`. Die drie maakten de echte onzichtbaar, want een sleutel in de
boom was hier normaal. Nu maakt een toets die een sleutel nodig heeft er zelf een, via de
fixture `age_keypair` of de factory `make_age_keypair` in `tests/conftest.py` (beide leunen op
`generate_sops_key_pair`, wat OPI ook gebruikt voor een projectsleutel). Ook de oefenmap
`sops-sandbox/` is weg; de werkwijze die daar in `steps.md` stond staat nu in
`docs/sops-en-age-met-de-hand.md`.

## Wat hierna komt

- **De onderliggende wachtwoorden roteren** van de 21 secrets. Her-versleutelen maakt niet
  onbekend wat gelezen kon worden.
- **De sleutel splitsen** in een infradeel en een projectdeel, zodat de renderer naast ArgoCD niet
  langer elk projectgeheim kan openen. Nu opent de CMP-plugin die de infrastructuur rendert
  dezelfde sleutel als die waarmee `config.age-private-key` van elk project versleuteld is, en dat
  is meer dan hij nodig heeft. Het bredere plan hiervoor staat niet op deze branch.
- **De git-historie zelf.** Zolang die bestaat is elke oude versie met een oude sleutel te openen.
  Dat geldt voor deze repo en voor de projects-repo. Een sleutelwissel verandert dat niet, en dat
  hoort een bewuste beslissing te zijn en geen aanname.
- **De oefenronde in CI bouwen:** een maandelijkse workflow die PREPARE en VERIFY-1 op een
  wegwerpsleutel draait en het resultaat weggooit. Vandaag draait geen enkele geplande workflow
  de ronde; zie "De oefenronde" voor waarom juist die twee fasen dat kunnen.
- **Laag 3 uitzoeken:** of GitHub push protection een eigen patroon voor `AGE-SECRET-KEY-`
  toestaat op dit abonnement.
