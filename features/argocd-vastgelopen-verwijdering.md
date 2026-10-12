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
lukt dat niet: **resources zelf verwijderen, en pas daarna de finalizer**. Kon de finalizer
niet weg omdat de veegactie niet alles meekreeg, dan wordt **de cascade afgewacht** die die
finalizer draagt.

1. `KubectlConnector.terminate_argocd_application_operation()` haalt de lopende operatie
   weg, met de patch die met de hand aantoonbaar werkte:

   ```
   kubectl -n <ns> patch application <naam> --type=json -p '[{"op":"remove","path":"/operation"}]'
   ```

   `.operation` wordt eerst gelezen, zodat de patch alleen loopt als er iets te verwijderen
   is. Mislukt het alsnog, dan wordt `.operation` teruggelezen om te zien of de operatie
   ondertussen is afgelopen.

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

Bij `partial` komt de reden als fout in `deletion_results["errors"]` terecht en meldt
`_force_delete_stuck_application()` `finalizer_removed: false` met `swept_partially: true`,
dus de aanroeper meldt `finalizer_removed: false`.

Blijft de finalizer zo staan, dan wacht `_force_delete_or_await_cascade()` op de cascade die
die finalizer draagt. Die is vrij omdat de operatie al beëindigd en alle leesbare resources
al weg zijn. Verdwijnt de Application in dat venster, dan loopt de rest van de
verwijderroute door, inclusief de manifestmap. De redenen van de partial sweep blijven
zichtbaar als geschiedenis in de operatie `argocd_app_cascade_wait`. Gaat de Application
niet weg, dan wordt de manifestmap gemarkeerd voor uitgestelde opruiming en meldt de delete
een fout.

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

Dezelfde vorm stond op de twee handgeschreven platform-Applications onder
`bootstrap/rig-system/kustomize/overlays/odcn-production/`: `SyncTimeout=60s`, om dezelfde
reden weg. ArgoCD v3.5.1 kent die naam niet als sync option; wat hij wel `SyncTimeout`
noemt is een interne wachttijd op een informer-cache in de API-server. Een bovengrens op
het wachten bestaat wel, maar als instelling op de ArgoCD-instantie:
`controller.sync.timeout.seconds: "300"` in de `extraConfig` van `argocd-deployment.yaml`,
op odcn-production gezet en op de twee andere overlays niet. Die grens breekt een
hangende sync niet af, want hij vraagt om beëindiging en de operatie blijft staan tot
`.operation` zelf weg is.

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

### Drie lagen, vanuit git

Met de drie GitOps-checkouts erbij (`--projects-repo`, `--argo-repo`,
`--deployments-repo`, of in de task `GITOPS=<map met de drie>`) leest hij van het
projectbestand naar beneden:

* **argo-repo**: een Application waarvan de render-root bij geen enkele deployment in het
  projectbestand hoort. ArgoCD houdt hem, en wat hij uitrolt, terecht in stand zolang hij in
  git staat. Alleen gemeld: weghalen is een commit in de argo-repo. De verwachte paden komen
  uit dezelfde functies in `opi/utils/naming.py` waarmee OPI ze schrijft, dus ook
  `_project` en `infrastructure` tellen mee. Het projectbestand wordt na
  `migrate_to_latest()` gelezen.
* **deployments-repo**: zoals hierboven, maar alleen onder de `<cluster>/`-map van het
  gekozen cluster. De Applications komen uit dat ene cluster, dus elke map van een ander
  cluster zou anders als wees lezen.
* **cluster**: zoals hierboven.

Gemeten op `odcn-production`, 9 oktober 2026: drie Applications in de argo-repo
(`dd-mco-pr-41`, `pm-5sj-pr139`, `toets-hn7-pr-32`), 42 verdwenen Applications met resources
in het cluster, 27 wezenmappen in de deployments-repo.

### In een Capsule-tenant

Het tenant-account mag ongeveer 90 van de types die het cluster serveert niet lijsten
(Calico, Elastic, metal3, kyverno, ...). Elke namespace antwoordt dan voor een deel van zijn
types, en de weigering hierboven sloeg op de eerste namespace al toe. Met de
deployments-repo erbij beslist wat die repo RENDERT: zijn de kinds die erin staan apart nog
eens opgevraagd en hebben die allemaal geantwoord, dan telt de namespace als gemeten. Een
gerenderd type dat zwijgt is nog steeds een weigering.

`SCHOON` betekent daarmee "niets in de kinds die deze repo vandaag rendert", en dat is
smaller dan "niets". Twee kinds vallen erbuiten: een kind dat geen enkele deployment meer
rendert (juist daar staat de rest van een verdwenen deployment), en een kind waarvan de
`kind:`-regel een template is (dat wordt gemeld, niet meegeteld). Op een cluster waar het
account elk type mag lijsten, zoals de sandbox, is de volle inventaris meestal al volledig en
komt deze toets niet aan te pas. Meestal, want een deel-antwoord komt ook van een onbereikbare
aggregated API, en dan beslist hij daar net zo.

De kinds worden gelezen uit de regels `apiVersion:` en `kind:` vooraan een document, niet
geparsed: de helmfile-projecten bevatten chart-templates, die pas YAML zijn als ArgoCD ze
rendert. Een getemplatete `apiVersion` telt voor elke groep met dat kind; een getemplatete
`kind` wordt gemeld, niet geraden. Een met SOPS versleuteld kind is een Secret: OPI
versleutelt alleen wat het als `.to-sops.yaml` schrijft.

### Wat nooit een wees is

* Een resource met een `ownerReference`: die hoort bij zijn eigenaar en gaat met die mee.
  Operators kopiëren het label `app.kubernetes.io/instance` op wat ze maken. Gemeten op
  `odcn-production`: de pod en services van de levende CNPG-database `algor-odc-db` lazen
  als wezen van een Application `algor-odc-db` die niet bestaat, en `--delete` had ze
  meegenomen. Dit geldt ook voor de forcering in OPI, die dezelfde lezing gebruikt.
* Endpoints en EndpointSlices: ArgoCD sluit ze zelf uit (`resource.exclusions`), en
  Endpoints dragen geen `ownerReference` om ze aan te herkennen.
* Alles van een helmfile-project (een deployment met een `helmfile`-lijst, dezelfde toets
  als `ProjectManager._deployment_uses_helmfile`): niet in de argo-laag, niet in de
  deployments-repo, en hun namespaces niet in het cluster. Dat vraagt de projects-repo,
  dus gebruik `GITOPS`.
* Een map in de deployments-repo die de projects-repo nog vraagt, ook als er op dit moment
  geen Application naar wijst. Dat is precies de stand die een forcering achterlaat: de
  deployment staat nog in het projectbestand en ArgoCD maakt de Application opnieuw aan, dus
  de map is nodig. Zonder deze regel liet de argo-laag die Application terecht staan en
  haalde de git-laag zijn map weg, met exitcode 0.

```bash
task argocd-orphan-sweep                                  # het hele cluster
NAMESPACE=rig-prd-mpfm-w3h task argocd-orphan-sweep       # een namespace, veel sneller
REPO=/path/to/zad-deployments task argocd-orphan-sweep    # ook de git-kant
GITOPS=~/IdeaProjects/rig-cluster-test-git-repositories task argocd-orphan-sweep
                                                          # alle drie de lagen; nodig in een tenant
NAMESPACE=... DELETE=1 task argocd-orphan-sweep           # echt verwijderen
```

Cluster en ArgoCD-namespace komen uit `task select-cluster` (`CLUSTER_TYPE`,
`RIG_NAMESPACE`). Het script las ze eerst uit de `.env` van `operations-manager/python`, en
die wijst naar de sandbox (`rig-system`), ook met select-cluster op productie.

Hij is traag: per namespace een `kubectl get` over elk type dat het cluster kent, zo'n tien
seconden, een minuut of acht voor heel `odcn-production`. Voor een eenmalige opruiming
volstaat dat; voor doorlopende detectie, zie hieronder.

Zonder `DELETE=1` verandert hij niets. Met `DELETE=1` verwijdert hij de resources, en haalt
hij de wezenmappen uit de checkout; committen en pushen blijft handwerk. `DELETE=1` samen
met `REPO` weigert (exitcode 2): mappen beoordelen zonder de projects-repo erbij gaat alleen
op de Applications die het cluster nu heeft. Met `GITOPS` mag het wel, zolang ook de
clusternaam er is: bij een leeg `CLUSTER_MANAGER` berekent hij de verwachte mappen niet, en
dan weigert hij om dezelfde reden.

Exitcodes: `0` niets gevonden (hij meldt dan `SCHOON`), `1` er staat iets, wezen of
resources die hij niet kon plaatsen, `2` geweigerd, waaronder elke mislukte lezing
hierboven. Dat maakt hem bruikbaar als laatste stap van een verwijdertoets: hij meet wat
er OVER is, niet wat er gebeurd lijkt te zijn.

## Bestanden

| Bestand | Wat het doet |
|---|---|
| `opi/utils/argocd_tracking.py` | Welke Application een resource bezit, via allebei de merktekens; `may_be_cut_from` voor de labelwaarde die twee namen kan zijn |
| `opi/connectors/kubectl.py` | `terminate_argocd_application_operation`, `get_argocd_application_destination_namespace`, `list_namespaced_resource_types`, `list_namespaced_kinds`, `list_tracked_resources`, `delete_tracked_resources`, `list_argocd_applications` |
| `opi/manager/delete_project_manager.py` | `_terminate_application_operation`, `_force_delete_stuck_application`, `_force_delete_or_await_cascade`, en de vier verwijderplekken |
| `manifests/argocd-application.yaml.jinja` | De syncOptions van een gegenereerde Application |
| `scripts/argocd_orphan_sweep.py` | De veegactie |
| `tests/test_argocd_stuck_delete.py` | De volgorde in de verwijderroute, en de merktekens zelf |
| `tests/argocd_sync_options.py` | De syncOptions die ArgoCD leest, gedeeld door beide grendels |
| `tests/test_argocd_generated_application_syncoptions.py` | De syncOptions van de gegenereerde Application |
| `tests/test_argocd_handwritten_application_syncoptions.py` | De syncOptions van de handgeschreven platform-Applications |
| `tests/test_argocd_orphan_sweep.py` | De veegactie |

## Wat hier niet in zit

Detectie en alarmering. Dit was veertien dagen onzichtbaar, en een alarm op Applications
met een `deletionTimestamp` ouder dan een kwartier zou dat afvangen, net als een alarm op
`user-applications` met een langlopende operatie. Dat hoort bij het bredere gat rond
servicemonitoring en is een eigen taak.

Doorlopende detectie van wezen. ArgoCD houdt zelf een watch-cache bij van alles in de
namespaces die hij beheert en kan melden wat daar staat zonder dat een Application het
bezit: `spec.orphanedResources: {warn: true}` in het AppProject. Dat kost geen extra
API-verkeer, waar deze veegactie per run duizenden lijstvragen stelt. Voorstel, nog niet
gebouwd.

De volledigheidstoets van de forcering soepeler maken. Die gebruikt dezelfde toets als de
veegactie, maar zonder de deployments-repo erbij, dus in een tenant eindigt elke forcering
als `partial`. Het wachten op de cascade hierboven lost op wat dat kostte (de manifestmap,
issue #205) zonder de guard aan te raken. Hem baseren op de gerenderde kinds, zoals de
veegactie met `--deployments-repo` doet, is pas aan de orde als blijkt dat dat niet
volstaat.
