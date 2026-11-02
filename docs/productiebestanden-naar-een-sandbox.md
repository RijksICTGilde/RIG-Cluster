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

Gebruik de taak, ook hiervoor. Sinds 7 augustus kent hij `SANDBOX_PUBLIC_KEY`:

```bash
SANDBOX_PUBLIC_KEY=age1... task sandbox:import-project -- $(cat /tmp/proj.txt)
```

Dat converteert **en** pusht. Wil je alleen converteren, dan is dit het commando dat de taak eronder draait:

```bash
cd operations-manager/python
uv run python -m scripts.migrate_project_to_sandbox \
  --source-dir $SRC \
  --output-dir /tmp/sandbox-projects \
  --sandbox-public-key age1... \
  --probe-image ghcr.io/minbzk/base-images/e2e-allservices:latest \
  --probe-port 8080 \
  $(cat /tmp/proj.txt)
```

Verwachte uitkomst: `Done: 47/47 projects migrated`.

**Twee valkuilen die je een run kosten, allebei in de praktijk gebeurd.**

*`--probe-image` moet je een expliciete waarde geven.* De vlag heeft een optionele waarde (`nargs="?"`), dus `--probe-image` gevolgd door de projectnamen slikt de eerste naam op als waarde. Het gevolg is stil en ernstig: elk component krijgt dan `image: <die-projectnaam>`, en dat project verdwijnt uit de uitvoer. Je merkt het alleen als je de images natelt.

*Laat de vlag niet weg.* Zonder `--probe-image` blijven de **productie-images én de productie-resources** staan (961Mi, 512Mi en zo). Het script past het probe-resourceprofiel alleen toe als er een probe-image is. Dat past niet op een kind-cluster.

*De namen moeten als losse argumenten binnenkomen.* Een variabele die je met `$(...)` vult en daarna aanhaalt, komt als **één** argument aan. Gebruik `$(cat ...)` zoals hierboven.

## Wat de conversie doet

| | van | naar |
|---|---|---|
| cluster | `odcn-production` | `sandboxed-local` |
| domeinen | `rijksapps.nl` | `sandbox.rijksapp.dev` |
| repositories | productie-Forgejo | `forgejo.sandbox.rijksapp.dev` |
| beheerder | de echte gebruikers | `admin@sandbox.rijksapp.dev` |
| projectsleutel | de sleutel uit productie | een nieuw gegenereerd AGE-paar |
| geheimen | AGE, productiesleutel | placeholder of opnieuw gegenereerd, op de nieuwe projectsleutel |
| resources | getuned per project | probe-profiel: 32Mi/10m, limiet 128Mi/200m |

Dat laatste is niet cosmetisch: productiewaarden passen niet op een kind-cluster. De tuner-historie gaat mee weg, anders zet die het bij de eerste sweep terug.

## Het project krijgt een eigen sleutel

Gemigreerde projecten krijgen nu hun eigen projectsleutel in plaats van de sleutel uit productie. Eerder hergebruikten we de publieke sleutel: dat betekende dat waarden die later in productie met die sleutel werden versleuteld ook op de sandbox leesbaar werden. Nu maakt de conversie een nieuw AGE-paar: publieke helft in `config.age-public-key`, private helft versleuteld voor het sandboxcluster in `config.age-private-key`.

Alles wat versleuteld meegaat, gaat dus op die nieuwe sleutel. Wat er van de inhoud overblijft:

| veld | wat ermee gebeurt |
|---|---|
| `config.api-key` | nieuw gegenereerd |
| `config.keycloak` | weggehaald, OPI maakt het per cluster opnieuw |
| `repositories[].password` | vervangen door de credentials van de sandbox-Forgejo |
| `user-env-vars` (component en deployment-component) | sleutels blijven, elke waarde wordt `SANDBOX-PLACEHOLDER` |
| `aliases` | inhoud blijft, alleen de ontvanger verandert (een alias verwijst naar een platformvariabele en is geen geheim) |
| bijlagen (`attachments`, `data[].content`) | inhoud wordt een placeholder; `id` en `filename` blijven staan |
| `deployments[].sleep.wake-token` | opnieuw gemunt |
| `deployments[].configuration` | weggehaald |
| elk ander versleuteld veld | placeholder, met een regel in het log welk pad |

Die laatste regel is het vangnet voor velden die de conversie niet bij naam kent, zoals het wachtwoord van een private registry in de config van `image-registries` of de credentials op een `remote-sources`-regel. Die worden dus **niet** leesbaar meegenomen: wil je in de sandbox echt bij zo'n registry kunnen, dan vul je die waarde daar zelf in.

Twee meldingen om op te letten in de uitvoer:

- `N overig(e) geheim(en) vervangen door een placeholder: [...]` is normaal en noemt de paden.
- `<pad> gaat met geen van beide projectsleutels open` betekent een waarde op een andere sleutel dan de projectsleutel. Dat hoort er niet meer te staan; controleer dat veld voordat je het bestand gebruikt.

Komt er een nieuw versleuteld veld in het projectschema, dan faalt `tests/test_migrate_project_sandbox_keys.py` zolang `AGE_FIELD_HANDLING` in het conversiescript niet zegt wat er met dat veld moet gebeuren.

## Controleren of het gelukt is

Tel dit na; alleen "het script zei Done" is niet genoeg gebleken.

```bash
cd /tmp/sandbox-projects
echo "bestanden        : $(ls *.yaml | wc -l)"
echo "sandboxed-local  : $(grep -rl 'sandboxed-local' . | wc -l)"    # == aantal bestanden
echo "met resources    : $(grep -rl 'resources:' . | wc -l)"
echo "met probe-profiel: $(grep -rl '32Mi' . | wc -l)"               # == met resources
grep -rhE "^ *image:" . | sed 's/^ *image: *//' | sort | uniq -c | sort -rn | head -3
```

De laatste regel is de belangrijkste: vrijwel alle images horen de probe-image te zijn. Zie je daar een projectnaam staan, dan is de `--probe-image`-valkuil hierboven toegeslagen.

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
