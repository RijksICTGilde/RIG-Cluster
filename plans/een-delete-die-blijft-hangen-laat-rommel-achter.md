# Een delete die blijft hangen, en het forceren dat rommel achterlaat

Status: plan, 24 september 2026. Hoort bij issue #184, waarin de waarneming en de metingen staan.

Het verwijderen van een ArgoCD-Application kan eeuwig blijven hangen, en de noodgreep die wij daarvoor ingebouwd hebben maakt het erger in plaats van beter. Dit plan repareert allebei.

## Wat er misgaat

Een sync-operatie die op health wacht blokkeert de verwijdering. De Application `mpfm-w3h-pr-310` stond veertien dagen vast met:

```
operationState.phase:   Running
operationState.message: waiting for healthy state of apps/Deployment/pr-310-magazijna and 2 more
```

Zolang die operatie draait komt de finalizer niet toe aan zijn cascade. En de operatie eindigt nooit, want hij wacht op workloads die niet kunnen starten: hun image is onpullbaar omdat de PR gesloten is en de tag is opgeruimd. De parent `user-applications` hangt daar weer achter, en die beheert ongeveer 250 resources, dus dan landt er voor niemand meer iets.

Onze code wacht zestig seconden en haalt dan de **finalizer** weg:

```
delete_project_manager.py:528   wait_for_application_deletion(max_retries=20)   = 60s
delete_project_manager.py:552   kubectl.remove_argocd_application_finalizers(...)
```

Dat is het verkeerde veld. De handmatige reparatie die werkte haalde de **operatie** weg, waarna de cascade binnen seconden afliep. Wij halen juist het slot weg dat de cascade zou uitvoeren, terwijl de blokkade blijft staan. Gevolg: de Application verdwijnt, er is nooit een delete-verzoek voor de resources geweest, en die blijven staan.

Gemeten op 24 september in `rig-prd-mpfm-w3h`: tien verdwenen Applications lieten **350 resources** achter, waaronder 140 secrets, opgebouwd tussen 3 en 22 september. Geen enkele droeg een `deletionTimestamp`. Daarbovenop elf mappen in de deployments-repo waar geen Application meer naar wees, en tien databaserollen die niet meer bestonden maar waar de achtergebleven pods wel op bleven inloggen: honderden mislukte pogingen per minuut, waarin een echte authenticatiefout niet meer op te merken valt.

## Wat het NIET is

Nagespeeld op het sandboxcluster, op dezelfde build `v3.5.1-rig2`: twee Applications met eronder een Deployment die een niet-bestaande image trekt, dus gegarandeerd nooit healthy. Eén met `resources-finalizer.argocd.argoproj.io`, één met de `/background`-variant. Allebei verwijderd:

```
foreground   app + deployment weg in 5 seconden
background   app + deployment weg in 5 seconden
```

Cascade-verwijdering trekt zich dus niets aan van de gezondheid van de workload, en ArgoCD-issue #23537 raakt deze build niet. **Een wissel naar de background-finalizer lost dit niet op** en hoort niet in dit plan: het knelpunt is de lopende operatie, en die blokkeert beide varianten even hard. Dit staat hier omdat het de voor de hand liggende verkeerde afslag is.

## Wat er gebouwd wordt

### 1. De operatie beëindigen voordat de Application verwijderd wordt

In de verwijderroute van OPI: haal eerst de lopende operatie van de Application weg, dan pas de `delete`. Dan komt de finalizer nooit achter een health gate te staan.

De handmatige vorm die aantoonbaar werkte:

```
kubectl -n <ns> patch application <naam> --type=json -p '[{"op":"remove","path":"/operation"}]'
```

Dit is de kern van het plan. Het werkt ook als de oorzaak buiten ons ligt: een registry die plat gaat, een kapot image, een crashloop in de applicatie van een gebruiker.

### 2. De resources zelf verwijderen voordat er geforceerd wordt

Blijft het daarna alsnog hangen, dan is forceren pas verantwoord nadat de resources zelf verwijderd zijn. Anders laat elke forcering opnieuw rommel achter, wat vandaag gebeurt.

Welke resources dat zijn is eenduidig: ze dragen de annotatie `argocd.argoproj.io/tracking-id` met de naam van de Application erin.

De volgorde wordt dus: operatie beëindigen, verwijderen, wachten; lukt dat niet, resources zelf verwijderen, en pas daarna de finalizer.

### 3. Een veegactie voor wat er al ligt

Een script dat resources opspoort met een `tracking-id` naar een Application die niet bestaat. Meldt standaard alleen; verwijdert met een expliciete vlag.

Dit is op 24 september al in ruwe vorm gedraaid tegen `rig-prd-mpfm-w3h` en vond precies de tien gevallen die met de hand waren uitgezocht, geen enkele meer en geen enkele minder, terwijl de vijf legitieme omgevingen buiten schot bleven. Die opzet hoort in `operations-manager/python/scripts/` te landen, in de vorm die dit project al kent.

De git-kant hoort erbij: een pad in de deployments-repo waar geen enkele Application naar verwijst is dezelfde soort wees. Dezelfde toets, andere bron.

### 4. De syncOptions die niets doen weghalen

In `operations-manager/python/manifests/argocd-application.yaml.jinja` staan twee regels die ArgoCD stil negeert:

- `Timeout=300` bestaat niet als sync option. Het leest als een bovengrens op het wachten, en die is er dus niet.
- `Delete=true` bestaat niet op app-niveau; alleen `Delete=false` en `Delete=confirm`.

Weghalen. Geen functioneel effect, wel eerlijk. Een regel die suggereert dat er een timeout is, is erger dan geen regel.

### 5. `PruneLast=true` heroverwegen

Deze staat in dezelfde template en is wat de prune naar de laatste wave duwt, die op de health van alles ervoor wacht. Zonder deze optie loopt de prune gewoon in de wave-volgorde mee.

Dit is de enige open vraag in dit plan: waarvoor was `PruneLast` destijds nodig, en weegt die reden zwaarder dan wat hier zichtbaar werd? Zoek dat uit voordat je hem weghaalt. Is het antwoord niet te vinden, laat hem dan staan: de punten 1 en 2 lossen het geval ook op met `PruneLast` erin.

## Wat erbuiten valt

Detectie en alarmering. Dit was veertien dagen onzichtbaar, en een alarm op Applications met een `deletionTimestamp` ouder dan een kwartier zou dat afvangen, net als een alarm op `user-applications` met een langlopende operatie. Dat hoort bij het bredere gat rond servicemonitoring en is een eigen taak.

Het opruimen van de bestaande rommel in `rig-prd-mpfm-w3h` is op 24 september al met de hand gedaan: 350 resources uit het cluster en elf mappen uit de deployments-repo.

## Hoe je weet dat het werkt

Naspelen op de sandbox, en wel in de vorm die het probleem echt bevat, want dat is precies waar de eerste proef langs schoot:

1. Een Application met een workload die nooit healthy wordt, en een sync die daarop wacht.
2. Terwijl die operatie loopt: verwijderen.
3. Zonder de reparatie hangt hij. Met de reparatie is de Application binnen seconden weg, **en** zijn resources ook.
4. Daarna de veegactie draaien: die hoort `SCHOON` te melden. Meldt hij iets, dan heeft de cascade niet alles meegenomen.

Die laatste stap is de belangrijkste toets van het geheel, want hij meet wat er overblijft en niet wat er gebeurd lijkt te zijn.
