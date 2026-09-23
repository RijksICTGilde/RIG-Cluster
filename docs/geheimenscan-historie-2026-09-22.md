# Uitkomst van de historie-scan, 22 september 2026

Een scanner op de huidige boom zegt niets over wat er in oude commits zit. Dit is de eenmalige
volledige scan over de historie van beide repo's die het plan van RC-221 vraagt.

**Dit is een meting en geen opruiming.** Wat hier staat bepaalt of er meer geroteerd moet worden
dan nu voorzien. De historie zelf blijft bestaan: zolang die er is, is elke oude versie te openen
met de sleutel waarvoor hij versleuteld was, en een sleutelwissel verandert daar niets aan. Wat
hier in staat moet dus als GELEKT behandeld worden, ongeacht of het uit de huidige boom is
verdwenen.

**De wijzers staan er niet bij, en dat is een besluit.** Deze repo gaat naar GitHub, en zolang
de vondsten hieronder niet ingetrokken zijn is een lijst met de exacte paden, de identifiers en
de intrekcommando's een kaart naar iets wat nog geldig is. Die lijst staat daarom op het interne
kanaal (`rc221-geheimenscan-historie-vindplaatsen.md`) en op de PR van RC-221; hier staat wat er
gemeten is en wat eruit volgt. Is alles ingetrokken, dan mag hij alsnog hierbij.

Herhaalbaar met:

```bash
python3 scripts/scan-secrets.py --history                     # deze repo
python3 scripts/scan-secrets.py --history --tree <andere repo> # de projects-repo
```

De huidige boom van RIG-Cluster is op het moment van schrijven schoon (0 meldingen over 2751
getrackte bestanden). De job `secret-scan` in `.github/workflows/security.yml` houdt dat zo.

**Wat hieronder staat is gemeten, niet opgelost.** RC-221 levert het GEREEDSCHAP voor de
sleutelwissel; de wissel zelf en het intrekken van wat hier boven water komt zijn handelingen op
een draaiend cluster en staan nog open.

## RIG-Cluster: 47.428 objecten, 16 vindplaatsen

### AGE-privesleutels: 6 verschillende, over 4 paden

- **De platformsleutel stond op precies EEN pad, ooit.** Dat is de belangrijkste uitkomst van deze
  scan: hij is nooit ergens anders in deze repo terechtgekomen. Hij wordt vervangen (het
  gereedschap en de volgorde staan in `features/sops-sleutel-vervangen.md`), en die vervanging
  is wat hem waardeloos maakt, niet het weghalen uit het bestand.
- **Er stond een oudere sleutel in datzelfde testbestand**, van voor de platformsleutel. Die is
  dus ook blootgesteld. Onbekend waar die ooit voor gebruikt is; het is niet de huidige sandbox- of
  developersleutel (die komen in geen enkele commit voor).
- **Een van de zes stond op een pad dat niemand had benoemd**, en dat pad bestaat niet meer. Ook
  die sleutel moet als gelekt gelden; welke bestanden hij ooit opende is niet te zeggen zonder de
  bijbehorende cijfertekst, en die staat niet in deze repo.
- De drie andere zijn testsleutels. Ze openen niets buiten hun eigen testdata, en ze zijn er
  inmiddels uit: toetsen maken hun sleutel nu ter plekke.

De echte sandbox- en developersleutels (`security/sandbox-key.txt`,
`security/developer-key.txt`) komen in **geen enkele commit** voor. `security/` staat in
`.gitignore` en dat heeft gehouden.

### Andere vondsten

- **Een ArgoCD-projecttoken zonder `exp`**, dus niet-verlopend. De waarde is inmiddels uit de
  huidige boom weggehaald, maar staat nog in de historie. **Dit is de vondst die ingetrokken moet
  worden**, en de reden dat de vindplaatsenlijst nog niet in deze repo staat.
- **Een PEM-privesleutel** van de git-server uit de begindagen. Het pad bestaat niet meer; staat
  die sleutel nog ergens als authorized_key, dan moet hij daar weg.
- **Een JWT in een sandbox-sessiebestand.** Het pad bestaat niet meer en sandboxen zijn wegwerp,
  dus lage prioriteit; wel een reden om zulke bestanden in `.gitignore` te zetten.

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

Dat is van voor de schemawijziging die het veld naar `age-private-key` hernoemde en het ging
versleutelen.

**Wat dit betekent.** Zo'n platte projectsleutel opent alles wat met diezelfde projectsleutel
versleuteld was in dat project op dat moment: de api-key, de Keycloak-wachtwoorden, de
`user-env-vars`, de bijlagen. Die cijfertekst staat in dezelfde commits, dus wie de historie heeft,
heeft de inhoud.

**Wat de reikwijdte begrenst.** Nagemeten: **geen enkel van de 11 projecten bestaat nog**. Ze
komen niet voor in de huidige 45 projectbestanden. De namen lezen als een reeks wegwerpprojecten
uit een testronde. Er valt dus niets meer te roteren voor deze projecten; wat er wel uit volgt is
dat de onderliggende waarden die ze deelden met iets dat nog leeft (bijvoorbeeld een
registry-wachtwoord of een PAT die over projecten heen gebruikt werd) niet meer geheim zijn.

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
