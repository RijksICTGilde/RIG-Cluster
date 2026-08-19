# Een cluster toevoegen

Het stappenplan, gedestilleerd uit het daadwerkelijk toevoegen van fundament-poc in augustus 2026.

`docs/een-nieuw-cluster-installeren.md` is het uitzoekdocument dat hieraan voorafging: het beschrijft de drie lagen en de inventaris. Dit document is korter en zegt wat je doet, in welke volgorde, en waar het misgaat. `docs/fundament-cluster-checklist.md` is het voorbeeld van een ingevuld resultaat.

## 1. Wat je nodig hebt voordat je begint

- **Clustertoegang** met het recht om CRD's, namespaces en ClusterRoles aan te maken. Controleer met `kubectl auth can-i create customresourcedefinitions`. Kan dat niet, dan is dit een cluster van het ODCN-type en wijkt paragraaf 5 af.
- **Een naam.** Die bepaalt vijf bestandsnamen (de `CLUSTER_CONFIG`-sleutel, `.env-taskfile-<naam>`, en de mappen in de drie kustomize-bomen). Hernoemen is duur, dus kies hem bewust.
- **Een DNS-zone** waar je een wildcard in mag zetten. Zonder DNS werkt Let's Encrypt niet en is het portaal niet bereikbaar.
- **Een git-token** voor de repo's, met een eigen token per cluster. Raakt hij gelekt en wordt hij ingetrokken, dan gaat niet meteen elk ander cluster mee onderuit.

## 2. Meten voordat je invult

Neem geen waarden over van een ander cluster. Dit is wat je wilt weten, en hoe je het meet:

| vraag | commando |
|---|---|
| Draait er een policy-engine? | `kubectl get validatingwebhookconfigurations` |
| Wijst iets een UID toe (SCC, PSA)? | `kubectl get ns -o custom-columns='NS:.metadata.name,ENFORCE:.metadata.labels.pod-security\.kubernetes\.io/enforce'` |
| Welke StorageClasses zijn er, en kan er een snapshots? | `kubectl get sc,csidrivers,volumesnapshotclass` |
| Draait er een ingresscontroller? | `kubectl get ingressclass` |
| Draait VPA, en levert hij aanbevelingen? | `kubectl get vpa -A` |
| Zijn er quota of LimitRanges? | `kubectl get limitrange,resourcequota -A` |
| Is er egress naar internet? | de firewall- of NetworkPolicy-objecten van het platform |
| Hoe groot is de node? | `kubectl get nodes -o custom-columns='CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory'` |

## 3. De drie plaatsen die je invult

1. **`CLUSTER_CONFIG` in `opi/core/cluster_config.py`.** Een blok met de gemeten waarden. Alles wat clusterafhankelijk is hoort hier en nergens anders.
2. **`.env-taskfile-<cluster>`.** `CLUSTER_TYPE`, `RIG_NAMESPACE`, de twee mapnamen, `AGE_KEY_FILE` en `KUBE_CONTEXT`.
3. **De overlays**: `infrastructure/bootstrap/clusters/<cluster>/` plus een overlay per component, en de twee onder `bootstrap/rig-system/kustomize/`.

## 4. De reeks

```
task select-cluster        # kies het nieuwe cluster
task cluster:bootstrap     # sleutel, secrets, credentials, namespace, operator, ArgoCD
```

`cluster:bootstrap` doet alles wat moet bestaan voordat ArgoCD het over kan nemen: de AGE-sleutel, de infrastructuur- en bootstrap-secrets, de git-credentials (hij vraagt erom en slaat zichzelf over als ze al staan), de namespace, het `sops-age-key` secret, de ArgoCD-operator en de bootstrap zelf. Daarna komt alles uit git.

Meer is het niet. Alles daarboven, inclusief de operators, hoort in de app-of-apps van het cluster.

## 5. Waar het misgaat

Dit zijn geen theoretische risico's; ze zijn allemaal een keer voorgekomen.

**Elke PVC zonder expliciete `storageClassName` pakt de default.** Als die default kapot of afwezig is, blijft de claim eeuwig `Pending` zonder foutmelding. Dit beet drie keer op rij: bij redis en prometheus (die noemen geen klasse in hun base) en bij de PVC van de operations-manager. Zet hem overal expliciet.

**De AGE-sleutel valt niet terug.** Alle sleuteltaken lezen `AGE_KEY_FILE` uit het env-bestand en stoppen als hij ontbreekt. Dat is bewust: een terugval betekende vroeger versleutelen met de productiesleutel van een ander cluster, zonder waarschuwing.

**De ingress-base is de Kind-variant.** `bootstrap/rig-system/kustomize/ingress-nginx` haalt het Kind-manifest en patcht hostPorts. Op een cluster met een echte LoadBalancer moet je de cloud-variant hebben. Twee van de drie patches daar zijn overigens niet Kind-specifiek en horen wel mee.

**De CMP-plugin doet een namespace per target.** Hij injecteert de destination-namespace in elke kustomization die hij rendert, en faalt hard als die namespace niet bestaat of als `sops-age-key` daar niet staat. Componenten die in hun eigen namespace installeren, zoals de operators, moeten dus buiten de plugin om: die hebben geen versleutelde inhoud en kunnen op ArgoCD's eigen kustomize.

**ArgoCD is standaard namespace-scoped.** De argocd-operator geeft een instantie alleen cluster-brede rechten als `ARGOCD_CLUSTER_CONFIG_NAMESPACES` gezet is. Zonder dat kan ArgoCD geen namespaces, CRD's of ClusterRoles aanmaken en lopen de operator-Applications stuk op RBAC. `prepare-argocd-operator` zet dat nu, met `ARGOCD_CLUSTER_SCOPED` als knop en `true` als default.

**Prune op een CRD cascadeert.** Verdwijnt een CRD, dan nemen finalizers elke custom resource mee, inclusief de `Cluster`-objecten van de databases. Zet `allowEmpty: false` op elke Application en `prune: false` op de Applications die CRD's meebrengen.

**Een branch-override geldt alleen voor onze eigen code.** Leest een cluster tijdelijk van een branch, dan geldt dat voor RIG-Cluster. De datarepo's waar OPI zelf in schrijft (`argo-applications`, `rig-cluster-projects`, `zad-deployments`) blijven op main; daar bestaat die branch niet eens.

**`rig-system` zit hardgecodeerd in de Prometheus-scrapeconfig.** Vier plaatsen binnen een ConfigMap, en kustomize herschrijft geen strings in ConfigMap-data. Kies je een andere namespace, dan heb je een vervangende patch van ruim honderd regels nodig, zoals ODCN die heeft.

**Remote kustomize-URL's zijn onbeproefd via ArgoCD.** Geen enkele component die vandaag in een clusterlijst staat haalt zijn manifest van een URL, dus of de repo-server dat tijdens een render mag is niet vastgesteld. Meet het voordat je erop bouwt.

## 6. Verifiëren

Wat je wilt zien voordat je zegt dat het werkt:

- de ingresscontroller heeft een adres, en dat adres antwoordt van buiten;
- een PVC bindt en is beschrijfbaar door een non-root pod met dezelfde securityContext die `deployment.yaml.jinja` voor dit cluster rendert;
- de Applications lopen door hun waves heen zonder op een te blijven hangen;
- OPI start, en zijn ConfigMap bevat het juiste `CLUSTER_MANAGER`.

`task fundament:verify` is daar het voorbeeld van; het is het waard om zoiets per cluster te hebben in plaats van met de hand te kijken.
