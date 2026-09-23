# Uitkomst van de historie-scan, 22 september 2026

Een scanner op de huidige boom zegt niets over wat er in oude commits zit. Dit is de eenmalige
volledige scan over de historie van beide repo's die het plan van RC-221 vraagt, met een lijst van
wat er gevonden is.

**Dit is een meting en geen opruiming.** Wat hier staat bepaalt of er meer geroteerd moet worden
dan nu voorzien. De historie zelf blijft bestaan: zolang die er is, is elke oude versie te openen
met de sleutel waarvoor hij versleuteld was, en een sleutelwissel verandert daar niets aan. Wat
hier in staat moet dus als GELEKT behandeld worden, ongeacht of het uit de huidige boom is
verdwenen.

Herhaalbaar met:

```bash
scripts/scan-secrets.py --history                     # deze repo
scripts/scan-secrets.py --history --tree <andere repo> # de projects-repo
```

De huidige boom van RIG-Cluster is op het moment van schrijven schoon (0 meldingen over 2751
getrackte bestanden). De job `secret-scan` in `.github/workflows/security.yml` houdt dat zo.

**Wat hieronder staat is gemeten, niet opgelost.** RC-221 levert het GEREEDSCHAP voor de
sleutelwissel; de wissel zelf en het intrekken van wat hier boven water komt zijn handelingen op
een draaiend cluster en staan nog open. Waar een rij "te doen" zegt, is dat te doen.

## RIG-Cluster: 47.428 objecten, 16 vindplaatsen

### AGE-privesleutels: 6 verschillende, over 4 paden

| publieke helft | ooit in | status |
|---|---|---|
| `age1efv94g...s2gl4dd` | `tests/test_age_password_decryption.py` | **de platformsleutel**; dit is de aanleiding van RC-221 |
| `age1t3u9uz...ste7e9m` | `tests/test_age_password_decryption.py` | een OUDERE sleutel in datzelfde bestand, van voor de platformsleutel |
| `age10uegg2...sn9d8xj` | `tests/e2e/testserver.py` | testsleutel, niet in gebruik buiten de E2E-server |
| `age1xm9xhg...q9x5df2` | `tests/test_sops_skip_unchanged.py` | wegwerpsleutel voor de sops-round-trip-toetsen |
| `age1fdup3p...qd0xj0j` | `sops-sandbox/sops-key.txt` | de oefensleutel; opende alleen de twee demobestanden in diezelfde map |
| `age1x5t5rx...q5tr35y` | `operations-manager/python/sources/agekey.txt` | **niet eerder benoemd**; pad bestaat niet meer |

Wat hiervan te vinden is:

- **De platformsleutel stond op precies EEN pad, ooit.** Dat is de belangrijkste uitkomst van deze
  scan: hij is nooit ergens anders in deze repo terechtgekomen. Hij wordt vervangen -- het
  gereedschap en de volgorde staan in `features/sops-sleutel-vervangen.md`, de wissel zelf moet
  nog gebeuren -- en die vervanging is wat hem waardeloos maakt, niet het weghalen uit het
  bestand.
- **Er stond een oudere sleutel in datzelfde testbestand**, van voor de platformsleutel. Die is
  dus ook blootgesteld. Onbekend waar die ooit voor gebruikt is; het is niet de huidige sandbox- of
  developersleutel (die komen in geen enkele commit voor).
- **`operations-manager/python/sources/agekey.txt` is een vindplaats die niemand had benoemd.** Het
  pad bestaat niet meer. Ook deze sleutel moet als gelekt gelden; welke bestanden hij ooit opende
  is niet te zeggen zonder de bijbehorende cijfertekst, en die staat niet in deze repo.
- De drie andere zijn testsleutels. Ze openen niets buiten hun eigen testdata, en ze zijn er
  inmiddels uit: toetsen maken hun sleutel nu ter plekke.

De echte sandbox- en developersleutels (`security/sandbox-key.txt`,
`security/developer-key.txt`) komen in **geen enkele commit** voor. `security/` staat in
`.gitignore` en dat heeft gehouden.

### Andere vondsten

| wat | waar | te doen |
|---|---|---|
| ArgoCD-projecttoken, **zonder `exp`** dus niet-verlopend | `HOW.md` en `archive/HOW.md` regel 161 | **intrekken.** `iss: argocd`, `sub: proj:default:automation-service`, `jti: 944c67e0-5d82-4890-b817-7c14de23cf79`, uitgegeven 2025-07-01T09:44:41Z. Intrekken met `argocd proj role delete-token default automation-service <jti>`; dat staat nog open. De waarde is inmiddels uit de huidige boom weggehaald, maar staat nog in de historie |
| PEM-privesleutel | `keys/git-server-key` | pad bestaat niet meer. Was de sleutel van de git-server uit de begindagen; als die sleutel nog ergens als authorized_key staat, moet hij eruit |
| JWT in een sessiebestand | `operations-manager/python/scripts/.sandbox-sessie.json` | een sandboxsessie, pad bestaat niet meer. Sandboxen zijn wegwerp, dus lage prioriteit; wel een reden om zulke bestanden in `.gitignore` te zetten |

## De projects-repo: 93.927 objecten, 11 vindplaatsen

Gemeten op `rig-cluster-projects` (de kopie met de 45 oudere projectbestanden die ook de testset
van RC-221 is).

**12 verschillende projectsleutels stonden in PLATTE TEKST in de historie**, in 11 projectbestanden,
onder het oude veld `sops-private-key`:

```yaml
config:
  sops-public-key: age1sh3cq0d...
  sops-private-key: AGE-SECRET-KEY-...      # plat, niet versleuteld
  api-key: |-
    -----BEGIN AGE ENCRYPTED FILE-----      # deze WEL versleuteld
```

De bestanden: `don1`, `here`, `vrij1`, `vrij2`, `vrij4` tot en met `vrij10`. Dat is van voor de
schemawijziging die het veld naar `age-private-key` hernoemde en het ging versleutelen.

**Wat dit betekent.** Zo'n platte projectsleutel opent alles wat met diezelfde projectsleutel
versleuteld was in dat project op dat moment: de api-key, de Keycloak-wachtwoorden, de
`user-env-vars`, de bijlagen. Die cijfertekst staat in dezelfde commits, dus wie de historie heeft,
heeft de inhoud.

**Wat de reikwijdte begrenst.** Nagemeten: **geen enkel van de 11 projecten bestaat nog**. Ze
komen niet voor in de huidige 45 projectbestanden. De namen (`vrij1` tot `vrij10`) lezen als een
reeks wegwerpprojecten uit een testronde. Er valt dus niets meer te roteren voor deze projecten;
wat er wel uit volgt is dat de onderliggende waarden die ze deelden met iets dat nog leeft
(bijvoorbeeld een registry-wachtwoord of een PAT die over projecten heen gebruikt werd) niet meer
geheim zijn.

**Wat nog na te gaan is,** en dit is de reden dat deze scan een meting heet en geen afronding:

1. Of een van die 11 projecten een geheim deelde met een project dat nog leeft. Dat is per project
   na te gaan door de platte inhoud uit de historie te ontsleutelen en te vergelijken -- met de
   sleutel die er toch al naast staat.
2. De productie-`zad-projects` is hier NIET gescand: dit is de kopie op de interne Forgejo. De
   scan hoort nog een keer over de echte repo te lopen, met hetzelfde commando.
3. Of de huidige 45 projectbestanden ooit ook zo'n platte fase hebben gehad. In de gescande
   historie staan ze er niet bij, maar deze kopie begint niet bij de eerste commit van de
   productierepo.

## Wat hier NIET uit volgt

- De historie herschrijven. Zolang de historie bestaat is elke oude versie met de bijbehorende
  sleutel te openen, en dat geldt voor beide repo's. Een sleutelwissel verandert dat niet. Of de
  historie herschreven moet worden hoort een bewuste beslissing te zijn en geen aanname -- en het
  is geen vervanging voor het intrekken van wat eruit komt.
- De onderliggende wachtwoorden van de 21 SOPS-secrets. Her-versleutelen maakt niet onbekend wat
  gelezen kon worden. Dat is een eigen ronde.
