# De SOPS-sleutel vervangen

De platform-AGE-sleutel zit niet alleen in de SOPS-bestanden, en `sops rotate` ziet alleen die.
Dit is het gereedschap dat elke vindplaats omzet, aantoont dat er niets anders is veranderd, en
aantoont dat de oude sleutel daarna niets meer opent. De volgende keer is het een commando.

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

Gemeten over de 45 projectbestanden: precies deze twee velden gaan open met de platformsleutel
(45 + 45 = 90 velden). De 136 andere versleutelde velden hangen aan de sleutel van het project
zelf.

De sleutelwaarde staat nergens in git: `deployment.yaml` verwijst met `secretKeyRef` naar het
secret `sops-age-key`, en `sops-plugin.sh` leest datzelfde secret. Het is een bootstrapwaarde die
alleen in Kubernetes leeft.

## Het gereedschap

| script | doet |
|---|---|
| `scripts/rotate-sops-key.py` | de 21 SOPS-bestanden, de 8 losse waarden en `projects/` in DEZE repo, plus met `--argo-applications` de SOPS-bestanden in een clone van zad-argo-user-applications |
| `scripts/rotate-project-keys.py` | de projectbestanden in een clone van de projects-repo |
| `scripts/replace-git-pat.py` | dezelfde ronde, met de PAT er ook vervangen |
| `scripts/set-sops-key-secret.py` | het k8s-secret wisselen en de operations-manager herstarten |
| `scripts/scan-secrets.py` | de grendel: weigert een commit, een branch of een historie met een geheim |

De streepjesnamen hierboven zijn dunne ingangen; de logica staat in modules ernaast. Zie
`scripts/README.md`.

## Gebruik

```bash
# 1. sleutel B maken en de namen op hun plek zetten
age-keygen -o security/nieuw.txt
# --rename vraagt naar de twee paden: antwoord security/key.txt en security/nieuw.txt,
# en het script schuift ze naar old_key.txt en key.txt
scripts/rotate-sops-key.py --rename

# 2. deze repo EN de argo-applicatierepo: eerst kijken, dan doen
git clone <zad-argo-user-applications> /tmp/zad-argo
scripts/rotate-sops-key.py --argo-applications /tmp/zad-argo --dry-run
scripts/rotate-sops-key.py --argo-applications /tmp/zad-argo

# 3. de projectbestanden, op een VERSE clone
git clone <zad-projects> /tmp/zad-projects
scripts/rotate-project-keys.py --projects /tmp/zad-projects/projects --dry-run
scripts/rotate-project-keys.py --projects /tmp/zad-projects/projects

# 4. verifieren TERWIJL er nog niets gepusht is
scripts/rotate-sops-key.py --verify --argo-applications /tmp/zad-argo
git -C /tmp/zad-projects log --oneline | head
git -C /tmp/zad-projects diff --stat HEAD~45

# 5. pushen, alle drie de repos, en METEEN daarna het secret wisselen
scripts/set-sops-key-secret.py --dry-run
scripts/set-sops-key-secret.py

# 6. de eindtoets over alle vindplaatsen
scripts/rotate-sops-key.py --assert-old-key-dead --projects /tmp/zad-projects/projects --argo-applications /tmp/zad-argo

# 7. de PAT-vervanging: dezelfde ronde, een ingang verder -- en pas NU
git clone <zad-projects> /tmp/zad-projects-pat
scripts/replace-git-pat.py --projects /tmp/zad-projects-pat/projects --dry-run
scripts/replace-git-pat.py --projects /tmp/zad-projects-pat/projects

# 8. een dag later de eindtoets nog een keer, en dan pas mag de oude sleutel weg
scripts/rotate-sops-key.py --remove-old-key --projects /tmp/zad-projects/projects --argo-applications /tmp/zad-argo
```

**Waarom de PAT-ronde stap 7 is en niet stap 3.** Ze kunnen in een keer: de motor onder beide is
dezelfde lus en `replace-git-pat.py` zet de sleutel en het wachtwoord in een beweging om. Toch is
de geadviseerde volgorde de sleutel eerst en de PAT daarna, want een fout in de PAT-vervanging
sleept dan de sleutelrotatie niet mee en de twee zijn los terug te draaien. Is de sleutelronde
aantoonbaar goed gegaan, dan is de PAT-ronde een herhaling van iets dat al gewerkt heeft.

Twee dingen die daaruit volgen:

* de ronde neemt nog steeds **beide** sleutels aan, ook al staat er na stap 3 geen enkel veld meer
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
de shellgeschiedenis, in de procestabel en in elk logboek dat het commando meeschrijft.

Droogloop is de veilige stand: zonder `--ja` wordt er geen byte geschreven voordat je op
"Run this?" ja hebt gezegd, en `--dry-run` stelt die vraag niet eens.

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

`--verify` werkt los, dus je kunt maanden later nog nagaan of alles nog klopt.

Een ronde over de projectbestanden mag stranden op een onleesbaar bestand: de rest wordt wel
gedaan en het script gaat rood. Repareer dat bestand en draai dezelfde ronde nog een keer.
`rotate-project-keys.py` meet zijn vingerafdruk over ALLE projectbestanden en niet over wat die
ene ronde omzette, dus de telling van de eindtoets klopt ongeacht in hoeveel rondes het lukte.

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

Dat is afgewezen. Dit is een sleutelwissel na blootstelling, dus het doel is dat A zo snel
mogelijk niets meer opent. Een overlapfase houdt A juist geldig, voegt een extra ronde toe om hem
er later weer af te halen, en die ronde kan vergeten worden -- dan heb je alle moeite gedaan en is
de oude sleutel nog steeds bruikbaar.

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
elk getrackt bestand met ECHTE cijfertekst
   -> wordt omgezet door een van de vindplaatsen
   OF staat op COVERAGE_EXCEPTIONS met een reden
   anders: FAIL, en de ronde begint niet eens
```

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

De grendel heeft geen sleutel nodig, dus hij draait op elke droogloop en in de toetsen. De
nameting van de uitzonderingen heeft er wel een, en die hoort bij de eindtoets.

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
op de 45 bestanden:

- alle 45 dragen de sleutel als meerregelige YAML block scalar, waar de inspringing en de
  chomping-indicator exact moeten kloppen;
- in datzelfde bestand staat `repositories[].password` op EEN regel, 34 keer als `base64+age:` en
  11 keer als armored blok -- twee vormen door elkaar;
- 3 van de 45 hebben commentaar, dat een naieve YAML-ronde weggooit.

Tijdens het schrijven van het plan is de omzetting een keer met tekstvervanging geprobeerd:
**56 van de 90 velden bleven stil op de oude sleutel staan**, zonder foutmelding. Raak de
projectbestanden dus nooit met sed, awk of `str.replace` aan. Het gereedschap laadt en schrijft
via `opi.utils.yaml_util`, de canonieke round-trip-schrijver, en zet een meerregelige waarde terug
als `LiteralScalarString`.

## Validatie: waarschuwen, niet blokkeren

Gemeten: **44 van de 45 projectbestanden voldoen niet aan het huidige schema (2.8)**. Ze zijn
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

`scripts/scan-secrets.py --history` loopt elke blob die ooit in de repo heeft bestaan na. Dat is
een meting en geen opruiming: wat eruit komt bepaalt of er meer geroteerd moet worden. De
uitkomst van de eenmalige scan over beide repo's staat in
`docs/geheimenscan-historie-2026-09-22.md`.

## Sleutels in toetsen: geen vaste, maar een gemaakte

Tot deze taak stonden er vier geldige AGE-sleutels in de boom, waarvan een de productiesleutel.
Dat die andere drie onschuldig waren is precies de reden dat de vierde niet opviel: een sleutel in
een testbestand was hier normaal.

Nu maakt een toets die een sleutel nodig heeft er zelf een, via de fixture `age_keypair` of de
factory `make_age_keypair` in `tests/conftest.py` (beide leunen op `generate_sops_key_pair`, wat
OPI ook gebruikt voor een projectsleutel). Ook de oefenmap `sops-sandbox/` is weg; de werkwijze
die daar in `steps.md` stond staat nu in `docs/sops-en-age-met-de-hand.md`.

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
- **Laag 3 uitzoeken:** of GitHub push protection een eigen patroon voor `AGE-SECRET-KEY-`
  toestaat op dit abonnement.
