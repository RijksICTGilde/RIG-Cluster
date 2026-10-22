# ZAD praat met ArgoCD over TLS

OPI roept de ArgoCD-API aan voor elke sync, statusvraag en refresh. Die verbinding liep
als plain HTTP over het clusternetwerk, dus het wachtwoord en het sessietoken gingen
onversleuteld over de lijn. BIO2 8.24 (cryptografie) en 8.20 (netwerkbeveiliging) vragen
versleuteling op transport, ook binnen het cluster.

De server serveert nu TLS, OPI toetst het certificaat tegen een CA-bundel die per cluster
vastligt, en de serverpoort staat alleen nog open voor wie hem nodig heeft.

## Waar het certificaat vandaan komt

| Cluster | Bron van het servercertificaat | CA-bundel voor OPI |
|---|---|---|
| odcn-production (OpenShift) | de service-CA, via het secret `argocd-server-tls` | ConfigMap `argocd-ca-bundle` met `service.beta.openshift.io/inject-cabundle: "true"`, sleutel `service-ca.crt` |
| odcn-staging (OpenShift) | de service-CA, via het secret `argocd-server-tls` | ConfigMap `argocd-ca-bundle` met `service.beta.openshift.io/inject-cabundle: "true"`, sleutel `service-ca.crt` |
| sandboxed-local (Kind) | `task sandbox:generate-argocd-tls`, eigen CA | ConfigMap `argocd-ca-bundle`, sleutel `ca.crt` |

Op OpenShift annoteert de argocd-operator de service `argocd-server` zelf met
`service.beta.openshift.io/serving-cert-secret-name` zodra de route op `reencrypt` staat.
Via de CR kan dat niet anders: `spec.server.service` kent alleen `type`. De service-CA
vult dan `argocd-server-tls`, en argocd-server leest zijn certificaat uit dat secret.

Kind heeft geen service-CA, en cert-manager staat er als lege CRD zonder controller. Het
wildcardcertificaat van de sandbox dekt alleen `*.sandbox.rijksapp.dev` en niet de
servicenaam waarop OPI de server aanspreekt, dus de sandbox tekent zelf. Die taak draait
mee in `task sandbox:setup` en is idempotent: de CA blijft staan, het servercertificaat
wordt opnieuw uitgegeven.

## Instellingen van OPI

| Variabele | Betekenis |
|---|---|
| `ARGOCD_USE_TLS` | `https` in plaats van `http` |
| `ARGOCD_VERIFY_SSL` | het certificaat van de server wordt gecontroleerd |
| `ARGOCD_CA_BUNDLE` | pad van de gemounte CA-bundel |
| `ARGOCD_PORT` | 443 in plaats van 80 |
| `ARGOCD_HOST` | de naam waarop OPI de server aanspreekt, en die in het certificaat moet staan |

De hostnaam wordt gecontroleerd, dus `ARGOCD_HOST` moet een SAN van het servercertificaat
zijn. De service-CA zet alleen `argocd-server.<namespace>.svc` en `...svc.cluster.local`,
en laat zich niet uitbreiden, dus op de ODCN-clusters staat de volledige servicenaam in
de configmap: `argocd-server.rig-prd-operations.svc` op productie en
`argocd-server.rig-staging-operations.svc` op staging. Het zelfgetekende certificaat van
de sandbox zet ook de korte naam in de SAN-lijst, dus daar blijft `argocd-server` staan.
Loop de SAN-lijst van `argocd-server-tls` op odcn na voordat het onderhoudsvenster
opengaat (op staging met `-n rig-staging-operations`):

```bash
kubectl -n rig-prd-operations get secret argocd-server-tls \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -text | grep -A1 'Subject Alternative Name'
```

Staan TLS en controle aan zonder bundel, dan weigert `ArgoConnector` met een
`ArgoTLSConfigurationError` in plaats van door te gaan. De CA achter de server zit niet in
de systeemtrust van de container, dus zonder bundel faalt elke aanroep toch, en een
duidelijke fout bij de constructie is beter dan een certificaatfout zestien aanroepen
verderop. Staat er wel een pad maar is het bestand er niet of bevat het geen certificaat,
dan weigert dezelfde fout bij de eerste aanroep, met het pad erin.

De bundel wordt gemount op `/etc/ssl/argocd` (zie
`bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml`). De
bestandsnaam verschilt per cluster, dus `ARGOCD_CA_BUNDLE` staat in de overlay.

Die mount staat op `optional: true`. Zonder dat blijft de pod op een cluster zonder
ConfigMap `argocd-ca-bundle` hangen in `ContainerCreating`, en dan staat er nergens een
melding die naar de oorzaak wijst. Met de optionele mount start de pod en weigert OPI de
verbinding met een fout die het pad noemt. Terugvallen op plain HTTP doet hij niet.

## Wie de serverpoort mag bereiken

`allow-argo` liet poort 8080 open voor elke pod in het cluster, op odcn-production en op
odcn-staging. Nu alleen voor de OPI-pod (`app: operations-manager`), de router
(`network.openshift.io/policy-group: ingress`) en de ArgoCD-componenten onderling. De
metrics-poort 8083 staat alleen open voor Prometheus.

NetworkPolicy kijkt naar de CONTAINERpoort, niet naar de servicepoort: de service van de
operator wijst zowel 80 als 443 naar `targetPort: 8080`, dus OPI op `ARGOCD_PORT=443`
komt binnen op 8080. 8443 stond in het beleid als reserve, maar daar luistert niets.

Deze regel werkt pas als de tijdelijke allow-all over de operations-namespace weg is; dat
volgt het netwerkplan.

## Uitrollen

De CR, de route en het netwerkbeleid zitten in de bootstrap-overlay
(`task bootstrap-argo-system`); de configmap van OPI gaat mee met de OPI-uitrol. Server
en OPI-configmap horen in hetzelfde onderhoudsvenster: een OPI met de oude instellingen
praat plain HTTP tegen een TLS-poort en krijgt zijn login niet rond. De automatische
overstap van `http` naar `https` na een mislukte login is weg, want het protocol is een
instelling en geen gok.

Twee taken, in deze volgorde, per cluster. Eerst de server, dan OPI. `select-cluster` zet
de clustervariabelen (`SECRETS_DIR` op staging, de AGE-sleutel) die beide taken nodig
hebben:

```bash
task select-cluster      # 3) odcn-staging, daarna opnieuw voor 1) odcn-production
task bootstrap-argo-system
task deploy-operations-manager
```

Rol staging eerst uit, en daarna productie. Tussen de twee taken praat OPI plain HTTP
tegen een poort die al TLS serveert, dus elke sync en statusvraag faalt zolang dat venster
open staat. Dat de server er is, zie je aan het secret dat de service-CA vult:

```bash
kubectl -n rig-staging-operations get secret argocd-server-tls
```

Faalt daarna elke ArgoCD-aanroep met een `ArgoTLSConfigurationError`, dan is de bundel leeg
of ontbreekt hij. De mount is optioneel, dus de pod start wel:

```bash
kubectl -n rig-staging-operations get configmap argocd-ca-bundle -o jsonpath='{.data.service-ca\.crt}' | head -1
```
