# Productiebestanden naar een sandbox halen

Voor een grondige test wil je de echte projectbestanden gebruiken, niet verzonnen voorbeelden. Ze kunnen **niet een-op-een**: hun geheimen zijn versleuteld met de productiesleutel, hun cluster is `odcn-production`, hun domeinen staan op `rijksapps.nl` en hun resources zijn getuned voor productie-images. Dit is de weg, met de valkuilen die er in de praktijk in zaten.

Geschreven op 7 augustus 2026, na het converteren van 47 bestanden voor de sandbox op de server.

## Welke weg je neemt

Er zijn twee doelen en ze vragen een andere aanpak.

**De sandbox op je eigen machine** (`kind-rig-sandbox` lokaal), met de sleutel uit `security/sandbox-key.txt`:

```bash
task sandbox:import-project -- <project> [<project>...]
```

Die taak converteert **en** pusht naar de sandbox-Forgejo. Zonder argumenten toont hij de beschikbare projecten. Dit is de makkelijke weg; gebruik hem als je lokaal test.

**Een sandbox die zijn eigen sleutel heeft gemaakt** (bijvoorbeeld op de server): dan moet je de publieke sleutel van dat cluster opgeven, zodat de private helft daar blijft en jij hem niet hoeft te kennen.

## De weg voor een sandbox met een eigen sleutel

### 1. De publieke sleutel van dat cluster ophalen

```bash
ssh claude@<server> "kubectl --context kind-rig-sandbox -n rig-system \
  get secret sops-age-key -o jsonpath='{.data.key}' | base64 -d | grep -i 'public key'"
# -> # public key: age1...
```

Alleen de regel met de publieke sleutel; de rest van dat bestand is de private helft en die hoef je niet te zien.

### 2. De projectenlijst opbouwen

```bash
SRC=~/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github/projects
ls $SRC/*.yaml | xargs -n1 basename | sed 's/.yaml$//' | tr '\n' ' ' > /tmp/proj.txt
```

**Let op het `*.yaml`.** Een kale `ls $SRC` pakt ook mappen mee (er staat een `local-old/` tussen), en dan faalt de conversie op een projectnaam die geen bestand is. Dat kost je een run.

### 3. Converteren

```bash
cd operations-manager/python
uv run python -m scripts.migrate_project_to_sandbox \
  --source-dir $SRC \
  --output-dir /tmp/sandbox-projects \
  --sandbox-public-key age1... \
  $(cat /tmp/proj.txt)
```

De namen moeten als losse argumenten binnenkomen. Een variabele die je met `$(...)` vult en daarna aanhaalt, komt als **één** argument aan en dan zoekt het script naar een bestand met alle namen aan elkaar. Gebruik `$(cat ...)` zoals hierboven.

Verwachte uitkomst: `Done: 47/47 projects migrated`.

## Wat de conversie doet

| | van | naar |
|---|---|---|
| cluster | `odcn-production` | `sandboxed-local` |
| domeinen | `rijksapps.nl` | `sandbox.rijksapp.dev` |
| repositories | productie-Forgejo | `forgejo.sandbox.rijksapp.dev` |
| beheerder | de echte gebruikers | `admin@sandbox.rijksapp.dev` |
| geheimen | AGE, productiesleutel | AGE, sandboxsleutel |
| resources | getuned per project | probe-profiel: 32Mi/10m, limiet 128Mi/200m |

Dat laatste is niet cosmetisch: productiewaarden passen niet op een kind-cluster. De tuner-historie gaat mee weg, anders zet die het bij de eerste sweep terug.

## Controleren of het gelukt is

```bash
cd /tmp/sandbox-projects
echo "sandboxed-local : $(grep -rl 'sandboxed-local' . | wc -l)"   # hoort gelijk te zijn aan het aantal bestanden
echo "odcn-production : $(grep -rl 'odcn-production' . | wc -l)"
echo "rijksapps.nl    : $(grep -rl 'rijksapps.nl' . | wc -l)"
```

**Een paar treffers op de oude waarden zijn normaal**, en het is de moeite waard te weten welke, want anders ga je zoeken naar een fout die er niet is. Gemeten op 7 augustus:

- **een deployment die `odcn-production` héét.** Dat is een naam, geen clusterveld, en die hoort te blijven staan (`amtbz-2m9`).
- **domeinhistorie.** Een project onthoudt zijn oude domeinen; die worden bewust niet herschreven (`ug-zxt`).
- **waarden in een helmfile-blok**, zoals een keycloak-host of een realmnaam. Het script herschrijft projectvelden, geen willekeurige helm-waarden (`mb-grist-helmfile`).

Wat er **niet** mag blijven staan is een `cluster:`-veld op `odcn-production` of een `repository`-url naar productie. Daar mag je wel op afgaan.

## Twee dingen die je uit elkaar moet houden

Bij het toetsen van een nieuwe versie zijn er twee vragen die op verschillende invoer horen:

1. **Haalt het bestand de schemapoort?** Dat toets je op de **originelen**, want conversie verandert juist de velden waar het om gaat. Dit heeft geen cluster nodig. De referentiemeting staat in `features/project-schema-versions.md`: op 6 augustus haalden 22 van de 47 de rauwe validatie niet, en 0 na migratie.
2. **Wordt het bestand verwerkt op het cluster?** Dat toets je op de **geconverteerde** bestanden.

Ze door elkaar halen levert een uitkomst op die niets zegt.

## Wie dit kan doen

Alleen iemand met de **productiesleutel** (`security/key.txt`) kan converteren, want de geheimen moeten eerst ontsleuteld worden voordat ze opnieuw versleuteld kunnen worden. Een agent of sessie zonder die sleutel kan dit niet, en moet de geconverteerde bestanden dus aangeleverd krijgen.
