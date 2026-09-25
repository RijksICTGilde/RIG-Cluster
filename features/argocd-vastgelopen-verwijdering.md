# Een Application die niet weg wil, en het forceren dat rommel achterlaat

Het verwijderen van een ArgoCD-Application kan blijven hangen achter een lopende
sync-operatie. OPI haalt die operatie nu eerst weg, en forceert pas nadat het de resources
zelf heeft verwijderd. Er is een veegactie voor wat er al ligt.

## Wat er misging

Een sync-operatie die op health wacht blokkeert de verwijdering zolang hij loopt. En hij
loopt eeuwig als de workloads waarop hij wacht niet gezond kunnen worden, bijvoorbeeld
omdat hun image onpullbaar is nadat de PR gesloten is. De Application `mpfm-w3h-pr-310`
stond zo veertien dagen vast:

```
operationState.phase:   Running
operationState.message: waiting for healthy state of apps/Deployment/pr-310-magazijna and 2 more
```

De verwijderroute wachtte zestig seconden en haalde dan de **finalizer** weg. Dat is het
verkeerde veld: de finalizer is juist wat de cascade uitvoert. De blokkade bleef staan, de
Application verdween, en er is nooit een delete-verzoek voor zijn resources geweest.

Gemeten op 24 september 2026 in `rig-prd-mpfm-w3h`: tien verdwenen Applications, **350
achtergebleven resources** waaronder 140 secrets, geen enkele met een `deletionTimestamp`.
Daarbovenop elf mappen in de deployments-repo waar geen Application meer naar wees.

Dit is geen kwestie van de finalizer-variant. Nagespeeld op de sandbox met build
`v3.5.1-rig2`: een Application met `resources-finalizer.argocd.argoproj.io` en een met de
`/background`-variant, allebei met een gegarandeerd ongezonde workload eronder, waren
allebei binnen vijf seconden weg met hun Deployment erbij. De cascade trekt zich niets aan
van de gezondheid van de workload. Het knelpunt is de lopende operatie, en die blokkeert
beide varianten even hard.

## Wat er nu gebeurt

De volgorde in de verwijderroute is: **operatie beëindigen, verwijderen, wachten**, en
lukt dat niet: **resources zelf verwijderen, en pas daarna de finalizer**.

1. `KubectlConnector.terminate_argocd_application_operation()` haalt de lopende operatie
   weg, met de patch die met de hand aantoonbaar werkte:

   ```
   kubectl -n <ns> patch application <naam> --type=json -p '[{"op":"remove","path":"/operation"}]'
   ```

   Dit gebeurt op elke plek waar OPI een Application laat verwijderen die een workload
   onder zich heeft: de wezenopruiming, de infrastructuur-Application, `delete_deployment`
   en `delete_deployment_from_yaml_change`. Het werkt ook als de oorzaak buiten ons ligt:
   een registry die plat gaat, een kapot image, een crashloop bij de gebruiker.

   Drie van die vier verwijderen via GitOps: het manifest gaat uit de
   argo-applications-repo en ArgoCD ruimt de Application op. De wezenopruiming heeft dat
   manifest per definitie niet meer en verwijdert de Application zelf, met
   `kubectl delete application -n <argo-namespace>`. De cascade komt daar van de
   `resources-finalizer.argocd.argoproj.io` die `manifests/argocd-application.yaml.jinja`
   meeschrijft.

   De projectapplicatie `{project}-project` krijgt die patch niet. Die verdwijnt ook niet
   via een eigen delete-aanroep, maar doordat `_delete_project_argocd_folder` de hele
   argo-map `{cluster}/{project}/` weghaalt, met het manifest van die applicatie erin.
   Nodig is het daar ook niet: de bron van die applicatie is de `_project`-map, en die
   draagt geen workload, alleen de serviceaccount van het project, pull-secrets en
   eventueel een quay-organisatie. Zonder workload is er niets om op health te wachten, dus
   de blokkade uit dit plan kan daar niet ontstaan.

2. Hangt het daarna nog, en staat `force` aan, dan verwijdert
   `_force_delete_stuck_application()` eerst de resources zelf. Pas daarna, en alleen als
   dat helemaal gelukt is, gaat de finalizer weg. Welke resources dat zijn leest
   `opi/utils/argocd_tracking.py`, en dat kijkt naar allebei de merktekens die ArgoCD kan
   zetten (zie hieronder).

Heeft die veegactie wel gedraaid maar niet alles meegenomen, dan is de status `partial` en
blijft de finalizer staan: hij is wat de cascade uitvoert, en dat is dan de enige weg
waarlangs de rest alsnog onder zijn eigen Application verdwijnt. Zwijgend forceren over
resources die blijven staan is precies wat de schade maakte. Drie dingen maken de veegactie
`partial`, en elk ervan betekent dat er resources van deze Application kunnen blijven staan:

* een delete die mislukte;
* een merkteken dat de veegactie niet kon plaatsen (zie de labelcap hieronder): dat kan van
  een buur zijn, maar net zo goed van deze Application zelf;
* een namespace die maar voor een deel van zijn resourcetypes antwoordde: wat wel
  geantwoord heeft wordt netjes verwijderd, en juist de types die zwegen houden hun
  resources. Zonder die derde uitkomst mee te geven is de melding `success`.

Bij `partial` komt de reden als fout in `deletion_results["errors"]` terecht en antwoordt
`_force_delete_stuck_application()` met `False`, dus de aanroeper meldt
`finalizer_removed: false` en wacht niet op een Application die er expres nog staat.

Kan de bestemmingsnamespace niet gelezen worden of lukt de inventaris van die namespace
helemaal niet, dan gaat de finalizer wel weg (een Application die blijft staan blokkeert de
parent voor iedereen, en er viel hier langs deze weg toch niets te verwijderen), maar ook
dat komt als fout binnen, met status `unknown_namespace` of `inventory_failed`. Een mislukte
inventaris leest anders als een lege namespace, en dan meldt de forcering `deleted: 0`
zonder fout terwijl de resources gewoon blijven staan.

### Twee merktekens, niet een

Welke resources bij een Application horen hangt af van de `resourceTrackingMethod` van
ArgoCD, en dit platform draait er twee:

| clustertype | ingesteld in | merkteken op de resource |
|---|---|---|
| `odcn-production` | `bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml` (`resourceTrackingMethod: annotation`) | annotatie `argocd.argoproj.io/tracking-id` |
| `local`, `sandboxed-local` | niets ingesteld, dus de standaard van ArgoCD | label `app.kubernetes.io/instance` |

Gemeten op het sandboxcluster op 24 september 2026: `argocd-cm` zegt
`application.resourceTrackingMethod: label`, en geen enkele resource onder een levende
Application droeg een tracking-id. Alleen op de annotatie selecteren vindt daar dus niets,
en een forcering die niets vindt verwijdert niets terwijl hij succes meldt: precies de
schade die dit moet voorkomen. Daarom tellen allebei de merktekens, met de annotatie
voorop. Geen van tweeën is hier dubbelzinnig: OPI's eigen manifesten schrijven ze geen van
beide, dus wat er staat komt van ArgoCD.

Het label heeft een maat die de annotatie niet heeft: Kubernetes laat 63 tekens in een
labelwaarde toe, en een Application-naam mag langer zijn. Het schema staat 30 tekens toe
voor de projectnaam en 63 voor de deploymentnaam (`project_v2.json`) en
`generate_argocd_application_name` kapt pas op 253, dus 94 tekens is haalbaar. Gemeten met
een server-dry-run op 25 september 2026: de API-server weigert een labelwaarde van 94
tekens ronduit. Zo'n Application krijgt op een labelcluster dus of geen enkele resource
(niets om te vergelijken), of resources met de naam afgekapt op 63 tekens erop. Wat ArgoCD
van die twee doet is **niet** gemeten: op het sandboxcluster is de langste
Application-naam vandaag 24 tekens, dus de situatie komt er niet voor, en hem daar maken
vraagt het cluster terwijl een andere PR erop test.

Een labelwaarde die precies op de 63 zit is daarmee dubbelzinnig, en in twee richtingen
tegelijk: hij is de hele naam van een Application, én de afgekapte vorm van elke langere
naam die ermee begint. Allebei die Applications kunnen op dat moment draaien, want op een
labelcluster dragen de resources van buur `<waarde>-x` precies `<waarde>`. Gelijkheid
beslist het dus niet; de enige naam die de waarde niet dubbelzinnig maakt is de waarde
zelf. Meer dan die dubbelzinnigheid zegt `may_be_cut_from` niet, want een predikaat dat
"hoort bij" antwoordt keert van betekenis om per aanroeper. Geen van tweeën beslist er
destructief op:

* De **forcering** verwijdert alleen wat haar Application ondubbelzinnig NOEMT. Daarvoor
  haalt ze de namen van alle Applications op (`list_argocd_applications`): begint een
  ANDERE naam met de waarde, dan is de resource net zo goed van een buur in dezelfde
  namespace, ook als de waarde gelijk is aan de naam die geforceerd wordt. 30 tekens
  project plus 63 deployment is wat het schema toestaat, en `projects/simple-example.yaml`
  zet vier deployments in een namespace. Op die lezing verwijderen is de secrets en PVC's
  van een draaiende buur meenemen, en een PVC komt niet terug. Lukt het lezen van de namen
  niet, dan blijft elke waarde op de 63 staan: zonder die namen valt er niets te plaatsen.
* De **veegactie** houdt zo'n resource buiten de wezenlijst, want `--delete` gaat daarop
  af en de andere lezing is een draaiende deployment. Hem daarmee ook buiten het rapport
  houden mag niet: dan is een echte wees `SCHOON` zolang er een langere zuster leeft. Is
  de waarde zélf de naam van een levende Application, dan valt er niets te melden: de
  resource is dan van die Application of van de langere zuster, en geen van beide is een
  wees.

Allebei melden ze daarom wat ze niet konden plaatsen, met naam en toenaam. Welke van de
twee lezingen klopt is aan de resource zelf te zien, en dat is mensenwerk.

Een waarde korter of langer dan 63 tekens is nooit afgekapt en doet niet mee. Een naam uit
de **annotatie** ook niet: die kent de grens niet. Op `odcn-production`, het enige
clustertype dat op de annotatie merkt en ook het clustertype waar dit plan over gaat, is
de vergelijking dus gewoon exact.

Dat een merkteken van ArgoCD komt gaat voor de veegactie niet zonder meer op, want het
label is niet van ArgoCD alleen: Helm zet `app.kubernetes.io/instance` ook, en bedoelt er
de release mee. Op de sandbox dragen zes resources in `ingress-nginx` het label
`instance: ingress-nginx` terwijl er geen Application met die naam bestaat. Daarom kijkt de
veegactie alleen in namespaces die OPI zelf heeft aangemaakt: die dragen
`created-by: operations-manager` (`manifests/namespace.yaml.jinja`). Dat is een
toelatingslijst, geen lijst met namen om over te slaan, en ook een met de hand opgegeven
`--namespace` moet erop staan.

### syncOptions die niets deden

Uit `manifests/argocd-application.yaml.jinja` zijn twee regels weg die ArgoCD stil
negeerde: `Timeout=300` bestaat niet als sync option (en las als een bovengrens op het
wachten die er dus niet was), en `Delete` kent op app-niveau alleen `false` en `confirm`.

`PruneLast=true` blijft staan. Die kwam mee met de eerste commit van de repo en nergens in
de historie of documentatie staat waarvoor hij nodig was. Zonder die reden is weghalen
gokken, en de twee stappen hierboven lossen het geval ook op met `PruneLast` erin.

## Nagemeten op de sandbox

Nagespeeld op 24 september 2026 op het sandboxcluster, met een Application waarvan de
Deployment een niet-bestaande image trekt (`ErrImagePull`, dus nooit healthy) en een
ConfigMap in sync-wave 5. Die latere wave is wat de sync op health laat wachten: zonder
hem meldt ArgoCD gewoon `Succeeded` terwijl de workload nog `Progressing` is. OPI maakt die
vorm zelf ook, want zijn secrets staan op wave -1 en een `postgresql-cluster` op wave 1.

```
22:09:43  op=Running  msg=waiting for healthy state of apps/Deployment/proef-web
22:09:45  kubectl delete application            deletionTimestamp gezet
22:12:18  na 2m33s: app er nog, finalizer er nog, deployment en configmap onaangeraakt
22:12:50  kubectl patch --type=json remove /operation
22:12:53  app EN resources weg, 3 seconden later
```

En de oude noodgreep, op dezelfde opstelling: `delete`, dan de finalizer weg terwijl de
operatie loopt. De Application was meteen weg, en de Deployment bleef staan **zonder
`deletionTimestamp`**, precies zoals de 350 resources in `rig-prd-mpfm-w3h`. De veegactie
vond hem daarna, verwijderde hem met `--delete`, en meldde `SCHOON`.

## De veegactie

`scripts/argocd_orphan_sweep.py` spoort op wat er al ligt. Twee bronnen, één toets:
bestaat de Application waar dit ding bij hoort nog?

* **cluster**: elke resource die ArgoCD als de zijne merkte, via welk van de twee
  merktekens hierboven het cluster ook gebruikt. De naam erin is zijn Application.
* **git**: elke `<cluster>/<project>/<leaf>`-map in een checkout van de deployments-repo.
  Dat is de vorm die `opi/utils/naming.py` voor deze repo oplevert, en elk zo'n
  pad hoort het doel te zijn van de `spec.source.path` van een Application.

Een `spec.source.path` wordt genormaliseerd voordat hij met een map vergeleken wordt: OPI
schrijft hem kaal, maar de Applications op het cluster dragen hem als
`./sandboxed-local/<project>/<deployment>` (gemeten op de sandbox), en die twee vormen
letterlijk vergelijken zou elk levend pad een wees noemen. Een pad dat ONDER een map ligt
beschermt hem ook: de diepte drie is de vorm van deze repo vandaag, en een repository-entry
met een niet-lege `path` legt de boom een niveau dieper. Zonder die regel was daar elke
levende projectmap een wees, en `--delete` had ze allemaal meegenomen.

Of een Application bestaat wordt via de Kubernetes-API gelezen, nooit via die van ArgoCD:
die antwoordt onder druk een dubbelzinnige `permission denied` voor Applications die wel
bestaan, en een levende Application als afwezig lezen zou de veegactie een draaiende
deployment laten verwijderen.

Om dezelfde reden weigert hij op een mislukte lezing die anders als leeg zou doorgaan. Een
mislukte `kubectl` mag nooit als meting doorgaan, want dan komt de uitkomst er precies zo
uit als bij een schoon cluster: `SCHOON` op stdout en exitcode 0, zonder dat er iets
bekeken is:

* geen enkele Application terug: dat leest hetzelfde als een mislukte query, en in beide
  lezingen wordt elke resource een wees;
* geen lijst van resourcetypes terug: dan inventariseert elke namespace als leeg;
* een namespace die niet te inventariseren was: die is niet leeg, hij heeft niet geantwoord;
* een namespace die maar voor een deel van zijn resourcetypes antwoordde: `kubectl get`
  eindigt niet-nul zodra EEN type faalt en drukt de rest gewoon af, en precies in het stuk
  dat zweeg staat de wees. De typelijst wordt een keer ontdekt en aan elke namespace
  meegegeven, en die ontdekking kijkt naar de verbs van het CLUSTER, niet naar wat de
  serviceaccount in die namespace mag opvragen, dus de weigering hierboven vangt dit niet;
* geen lijst van namespaces terug: dan lijkt er geen enkele door OPI gemaakt, dus zonder
  `--namespace` valt er niets te vegen en met `--namespace` valt de opgegeven namespace
  buiten de toelatingslijst.

`list_tracked_resources` en `list_namespaced_resource_types` dragen dat verschil zelf, met
`None` voor een mislukte lezing naast een lege lijst voor "niets gevonden".
`list_tracked_resources` geeft er een derde antwoord bij: naast de gevonden resources zegt
hij of de inventaris VOLLEDIG is (`tuple[list[TrackedResource], bool]`). De veegactie
weigert op een halve inventaris net zo hard als op een mislukte; de forcering verwijdert
het deel dat wel geantwoord heeft, maar meldt `partial` met een fout erbij.
`get_namespace_label_map` gooit in plaats daarvan een `KubectlExecutionError`, die de
veegactie vangt en als weigering meldt.

Daarnaast weigert hij op een cluster dat niet te bereiken is. Dat is geen lezing die als
leeg doorgaat maar een `KubectlConnectionError`, die alle vier de lezingen kunnen gooien.

```bash
task argocd-orphan-sweep                                  # het hele cluster
NAMESPACE=rig-prd-mpfm-w3h task argocd-orphan-sweep       # een namespace, veel sneller
REPO=/path/to/zad-deployments task argocd-orphan-sweep    # ook de git-kant
NAMESPACE=... DELETE=1 task argocd-orphan-sweep           # echt verwijderen
```

Zonder `DELETE=1` verandert hij niets. Met `DELETE=1` verwijdert hij de resources, en haalt
hij de wezenmappen uit de checkout; committen en pushen blijft handwerk.

Exitcodes: `0` niets gevonden (hij meldt dan `SCHOON`), `1` er staat iets, wezen of
resources die hij niet kon plaatsen, `2` geweigerd, waaronder elke mislukte lezing
hierboven. Dat maakt hem bruikbaar als laatste stap van een verwijdertoets: hij meet wat
er OVER is, niet wat er gebeurd lijkt te zijn.

## Bestanden

| Bestand | Wat het doet |
|---|---|
| `opi/utils/argocd_tracking.py` | Welke Application een resource bezit, via allebei de merktekens; `may_be_cut_from` voor de labelwaarde die twee namen kan zijn |
| `opi/connectors/kubectl.py` | `terminate_argocd_application_operation`, `get_argocd_application_destination_namespace`, `list_namespaced_resource_types`, `list_tracked_resources`, `delete_tracked_resources`, `list_argocd_applications` |
| `opi/manager/delete_project_manager.py` | `_terminate_application_operation`, `_force_delete_stuck_application`, en de vier verwijderplekken |
| `manifests/argocd-application.yaml.jinja` | De syncOptions van een gegenereerde Application |
| `scripts/argocd_orphan_sweep.py` | De veegactie |
| `tests/test_argocd_stuck_delete.py` | De volgorde in de verwijderroute, en de merktekens zelf |
| `tests/test_argocd_application_syncoptions.py` | De syncOptions die ArgoCD kent |
| `tests/test_argocd_orphan_sweep.py` | De veegactie |

## Wat hier niet in zit

Detectie en alarmering. Dit was veertien dagen onzichtbaar, en een alarm op Applications
met een `deletionTimestamp` ouder dan een kwartier zou dat afvangen, net als een alarm op
`user-applications` met een langlopende operatie. Dat hoort bij het bredere gat rond
servicemonitoring en is een eigen taak.
