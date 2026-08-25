# ArgoCD: van één Application naar app-of-apps, zonder iets kwijt te raken

Runbook voor het omzetten van een draaiend cluster van de platte opzet (één Application die de hele clusterlijst rendert) naar de app-of-apps-opzet (één Application per component, met sync-waves). Geschreven na de bouw van die opzet op fundament-poc, met productie als doel.

Dit is geen theorie: elke stap heeft een controle en een stopconditie. Werk hem van boven naar beneden af en sla niets over.

## 1. Wat er verandert

Productie draait nu op drie Applications, waarvan er één alles doet:

| Application | pad |
|---|---|
| `production-infrastructure` | `infrastructure/bootstrap/clusters/odcn` |
| `ron-infrastructure` | `infrastructure/bootstrap/infrastructure/mail/controller/overlays/odcn` |
| `user-applications` | de datarepo `argo-applications`, map `odcn-production` |

Die eerste rendert acht componenten in één keer: secrets/config, postgresql/database, keycloak/controller, minio/controller, minio/config, prometheus/controller, redis/controller en external-dns/controller.

Na de migratie is elk van die acht een eigen Application met een sync-wave, zoals op fundament-poc.

## 2. Waarom dit gevaarlijk is

Twee mechanismen bepalen of er iets kwijtraakt. Begrijp ze voordat je begint.

**Resource tracking is enkelvoudig.** ArgoCD schrijft in het label `app.kubernetes.io/instance` welke Application een resource bezit. Eén resource, één eigenaar. Zodra een nieuwe Application dezelfde resource toepast, springt dat label over: dat is de adoptie, en die gaat vanzelf. ArgoCD weigert niet en maakt niets opnieuw aan. Op fundament-poc is geverifieerd dat `application.resourceTrackingMethod` op `label` staat.

**De finalizer bepaalt of verwijderen doorwerkt.** Draagt een Application de finalizer `resources-finalizer.argocd.argoproj.io`, dan sleept het verwijderen ervan álle beheerde resources mee. Zonder die finalizer laat verwijderen de resources staan. Hier zit het echte risico, niet in de adoptie.

## 3. Eerst verifiëren, niet aannemen

Deze twee zijn bij het schrijven van dit document **niet** gecontroleerd op productie, omdat het cluster op dat moment onbereikbaar was. Doe ze als eerste.

```
# 1. Welke tracking-methode gebruikt productie? Verwacht 'label' of leeg (leeg = default label).
kubectl --context odcn-rig-production -n rig-prd-operations \
  get cm argocd-cm -o jsonpath='{.data.application\.resourceTrackingMethod}'

# 2. Draagt de oude Application de cascade-finalizer? Verwacht: geen.
kubectl --context odcn-rig-production -n rig-prd-operations \
  get application production-infrastructure -o jsonpath='{.metadata.finalizers}'
```

In git staat op geen van de drie productie-Applications een finalizer, maar het live object kan afwijken: ArgoCD zet hem er zelf op bij een verwijdering via de UI. Wijkt punt 1 af van `label`, stop dan en herzie dit runbook, want dan klopt de adoptie-aanname niet.

## 4. De asymmetrie met fundament-poc

Kopieer de opzet van fundament-poc niet één op één. Daar staan vijftien Applications, waaronder wave -1 met cert-manager, CloudNativePG, ingress-nginx en de opslagprovisioner.

Op ODCN kan dat niet. Daar praat ArgoCD via de Capsule-proxy en is hij tenant-scoped: hij mag geen CRD's, geen namespaces en geen cluster-brede RBAC aanmaken. De operators worden daar buiten GitOps geïnstalleerd en dat blijft zo. Zie `plans/operators-via-argo-in-plaats-van-taken.md`.

Productie migreert dus alleen de acht bestaande componenten. `forgejo` en `operations-manager` staan op fundament-poc wel in de lijst en op productie niet; dat zijn aparte besluiten, niet iets voor deze migratie.

## 5. Het runbook

### Stap 0: backup en uitgangspunt vastleggen

```
kubectl --context odcn-rig-production -n rig-prd-operations get application -o yaml > /tmp/apps-voor.yaml
kubectl --context odcn-rig-production -n rig-prd-operations get pvc,cluster.postgresql.cnpg.io -o wide
```

Zorg dat er een verse database-backup is. Verifieer dat die backup ook teruggezet kán worden, niet alleen dat hij bestaat.

**Stop als** er geen geldige backup is.

### Stap 1: zet de oude Application stil

Dit vóór alles. Zolang `production-infrastructure` automatisch synct met `prune: true`, kan hij tijdens de overgang resources opruimen die net van eigenaar gewisseld zijn.

Zet in git `syncPolicy.automated` uit en `prune: false`, en laat ArgoCD dat toepassen.

**Controle:** `kubectl get application production-infrastructure -o jsonpath='{.spec.syncPolicy}'` toont geen `automated` meer.

**Stop als** de Application nog steeds automatisch synct.

### Stap 2: haal de finalizer eraf als hij er is

Alleen nodig als stap 3.2 hem aantrof.

```
kubectl --context odcn-rig-production -n rig-prd-operations \
  patch application production-infrastructure --type=json \
  -p='[{"op":"remove","path":"/metadata/finalizers"}]'
```

**Controle:** het veld `metadata.finalizers` is weg.

**Stop als** de finalizer er nog op staat. Dit is de stap die bepaalt of stap 6 veilig is.

### Stap 3: rol de nieuwe Applications uit

Zet de acht Applications in `infrastructure/bootstrap/clusters/odcn/applications/` met hun waves, en verwijder de platte lijst uit de kustomization. Elke Application krijgt eerst `prune: false`: adopteren mag, opruimen nog niet.

Waves, op basis van wat waarvan afhangt:

| wave | componenten |
|---|---|
| 0 | secrets/config |
| 1 | postgresql/database, redis/controller, minio/controller |
| 2 | keycloak/controller, prometheus/controller, minio/config |
| 3 | external-dns/controller |

**Controle:** alle acht bestaan en het instance-label is overgesprongen.

```
kubectl --context odcn-rig-production -n rig-prd-operations \
  get deploy,sts,svc,secret -o custom-columns='K:.kind,N:.metadata.name,APP:.metadata.labels.app\.kubernetes\.io/instance'
```

Er mag geen enkele resource meer op `production-infrastructure` staan.

**Stop als** een resource `Missing` meldt of als het label niet is overgesprongen.

### Stap 4: vergelijk met de uitgangssituatie

Verwacht diff-ruis: de nieuwe overlays renderen niet byte-identiek aan de oude. Beoordeel elke diff en teken af dat hij bedoeld is.

Let specifiek op de dingen die je niet terugkrijgt:

- PVC's van postgresql, minio en prometheus
- de CNPG `Cluster` en de `Database`-objecten
- de MinIO-buckets en hun inhoud
- de secrets in `secrets/config`

**Stop als** één van deze in de diff staat als verwijdering.

### Stap 5: zet prune weer aan

Pas als stap 3 en 4 schoon zijn. Per Application `prune: true` en `automated` terug.

**Controle:** alle acht `Synced` en `Healthy`, en niets verdwenen. Draai de controle van stap 0 opnieuw en vergelijk.

### Stap 6: verwijder de oude Application

Nu pas, en alleen als stap 2 is afgetekend.

```
kubectl --context odcn-rig-production -n rig-prd-operations \
  delete application production-infrastructure --cascade=orphan
```

`--cascade=orphan` is de tweede grendel naast de ontbrekende finalizer. Gebruik beide, niet één.

**Controle:** de acht Applications staan er nog, alle workloads draaien, geen enkele pod is herstart.

## 6. Wat je hierna wilt controleren

De health-check voor `Application` moet aan staan, anders doen de waves niets. Zonder die check kent de root zijn kinderen geen health toe en schuift elke wave onmiddellijk door. Zie `bootstrap/rig-system/kustomize/overlays/fundament-poc/argocd-deployment.yaml` voor de check, en let op: de application-controller moet herstart worden voordat een wijziging in `extraConfig` effect heeft.

Waves gelden bovendien alleen tijdens de sync van de root. Elk kind heeft daarna zijn eigen `syncPolicy.automated` en synct zelfstandig verder. De waves regelen de volgorde bij het aanmaken, niet daarna.

## 7. Terugrollen

Tot en met stap 5 is terugrollen eenvoudig: zet de platte kustomization terug, laat `production-infrastructure` de resources weer adopteren, en verwijder de acht nieuwe Applications met `--cascade=orphan`.

Na stap 6 is de oude Application weg. Terugrollen betekent dan hem opnieuw aanmaken en laten adopteren. Dat werkt, maar je bent de zekerheid kwijt dat hij precies hetzelfde beheerde. Doe stap 6 daarom pas als je een dag met de nieuwe opzet gedraaid hebt.
