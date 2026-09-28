# Welke grendel de ServiceAccount liet lopen

Meting van 25 september 2026 op het gedeelde sandboxcluster (ArgoCD `v3.5.1-rig1`,
namespace `rig-system`, umbrella `user-applications`). De opdracht was eerst te meten WELKE van
de drie grendels het liet lopen, want een reparatie op de waves is iets anders dan een
reparatie op de wacht in OPI.

## Uitslag: grendel 1, de sync-waves

De projectapplicatie staat op wave 0 en de deployment-applicaties op 1. Die ordening is
leeg, om twee redenen die los van elkaar al genoeg zijn.

### Een Application is `Healthy` een seconde nadat hij bestaat

Een proefapplicatie in `rig-system`, zonder `syncPolicy.automated`, dus hij synchroniseert
nooit iets:

```
creationTimestamp:  2026-09-25T09:32:16Z
health:             Healthy   (lastTransitionTime 2026-09-25T09:32:17Z)
sync:               Unknown
operationState:     afwezig
resources:          0
conditions:         ComparisonError - "Failed to load target state: failed to generate
                    manifest for source 1 of 1: rpc error: ..."
```

Eén seconde. Met nul resources, zonder ooit gesynchroniseerd te hebben, en terwijl ArgoCD
zijn bron niet eens kan renderen. De wave-0-grendel van de umbrella gaat dus open voordat
de `*-project`-applicatie zijn ServiceAccount heeft uitgerold, en wave 1 mag meteen.

### En daarna valt er alsnog niets te wachten

De resources van de `*-project`-applicatie krijgen van ArgoCD geen health. Bij het gemeten
project is dat er één, de ServiceAccount:

```
$ kubectl -n rig-system get app pgsch-2iw-project -o json
resources: [('ServiceAccount', 'pgsch-2iw-sa', None)]
health:    Healthy
```

Health `None` draagt niet bij aan de health van de applicatie. Er is dus op geen enkel
moment een `Progressing` waarop de wave kan wachten: niet voor de sync, niet tijdens, niet
erna.

## Grendel 2 en 3

- **De expliciete wacht in OPI** (voor deze PR in `process_project_from_git`) stond
  structureel te laat: hij liep nadat de deployment-applicaties bestonden, en elke
  gegenereerde applicatie draagt `syncPolicy.automated`
  (`manifests/argocd-application.yaml.jinja`), dus ArgoCD synchroniseert de
  deployment-applicatie zelfstandig zodra zijn CR er staat. Daarbij gaf hij
  `refreshed_after` niet mee, dus bij een herhaalrun kon een `Synced` van VOOR onze commit
  hem al tevredenstellen.
- **Het zelfherstel** werkt, maar duurt langer dan de 300s die `process_project_from_git`
  per deployment-applicatie op de sync wacht, en dan is een aanmaak die uiteindelijk
  convergeert voor de gebruiker toch mislukt.

## Waarom de reparatie niet in de waves zit

De voor de hand liggende reparatie is een eigen health-check voor
`argoproj.io/Application` in `extraConfig`, zodat een kind pas gezond is als het echt
gesynchroniseerd heeft. Die is gemeten en afgewezen: wave 0 is clusterbreed.

```
$ kubectl -n rig-system get applications -o json   # wave 0, 25 september 2026
regis-0vf-project    sync=Unknown   health=Healthy   operationState=NONE   resources=0
```

`regis-0vf-project` heeft nooit gesynchroniseerd en komt daar ook niet meer uit. Met een
strengere health-check staat die applicatie voor altijd op `Progressing`, en omdat wave 0
van de umbrella alle projecten van het cluster bevat, houdt hij daarmee wave 1 van ELK
project tegen. Eén dood project zou dan alle uitrol op het cluster stilzetten.

De ordening hoort dus per project, en niet aan een gedeelde grendel.

## Waar de ordening wel thuishoort

OPI ordent deze klasse afhankelijkheid al ergens anders, voor de
infrastructuurapplicatie, die net zo goed op wave 0 staat en er net zo goed moet staan
voordat de pods komen (`project_manager.py`, STEP 6 tot 10): aanmaken, wachten tot ArgoCD
de applicatie heeft aangemaakt, hem ZELF verversen, en dan wachten op een sync die
aantoonbaar NA die verversing komt (`refreshed_after`). Pas daarna gaat de run verder.

Het projectniveau volgt die vorm nu ook. Dat vraagt dat de deployment-applicaties niet meer
in dezelfde commit staan als de projectapplicatie, want anders bestaan ze al voordat er iets
te wachten valt.

## De gedragskeuze die daarbij hoort

De oude wacht in `process_project_from_git` ving `(TimeoutError, RuntimeError)` af met een
`logger.warning` en liep degraded door. De nieuwe grendel in `create_argocd_resources` laat
ze door, en de `except Exception` van `process_project` maakt daar `return False` van. Een
project waarvan het projectniveau niet kan synchroniseren rolt daarmee helemaal niets meer
uit, waar het eerst een waarschuwing in het log was.

Dat is met opzet. Mist de ServiceAccount, dan haalt de Deployment die hem noemt zijn eigen
sync-timeout van 300s toch niet, en dan is degraded doorlopen een mislukking die zich als
succes voordoet: precies het beeld waarmee deze melding binnenkwam. De schade blijft bij dit ene
project, terwijl de hierboven afgewezen health-override hetzelfde clusterbreed deed. Het
onderscheid is de blast radius, niet de strengheid.

Een project als `regis-0vf-project`, dat nooit meer synchroniseert, valt hier dus onder: dat
komt met deze wijziging niet meer door een `process_project` heen. Dat is de bedoelde
uitkomst, want zo'n project rolde ook voorheen niets werkends uit, alleen zonder het te
melden.
