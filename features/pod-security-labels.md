# Pod Security-labels op projectnamespaces

Elke namespace die OPI aanmaakt of verwerkt krijgt de Pod Security Admission-labels van
het cluster. Op de sandbox wordt `baseline` afgedwongen en waarschuwt `restricted`. Dat is
een tweede verdedigingslaag naast de hardening die in de manifestsjablonen zelf zit (BIO2
8.27, CIS Kubernetes 5.2).

## Wat het is

De pods die OPI zelf rendert staan onder `baseline` op elk cluster dat geen CA van de
node mount: de sjablonen zetten een securityContext zonder privileges en zonder
hostnetwerk. De uitzondering is `ca_certificate.enabled`, dat per component een
`hostPath`-volume rendert (`deployment.yaml.jinja`), en daarom staat `local` op
waarschuwen in plaats van afdwingen (zie de tabel hieronder).

Maar een project mag in zijn deploymentmap ook eigen manifesten, een helm-chart of een
helmfile meegeven, en de AppProject laat elke namespaced kind toe
(`namespaceResourceWhitelist: '*'`). Voor die pods was er geen tweede controle.

Met de labels op de namespace weigert de API-server zelf een pod die `baseline`
overtreedt, ongeacht wie het manifest schreef:

| label | effect |
|---|---|
| `pod-security.kubernetes.io/enforce` | de API-server WEIGERT een pod die het niveau overtreedt |
| `pod-security.kubernetes.io/warn` | de aanvrager krijgt een waarschuwing, de pod start wel |
| `pod-security.kubernetes.io/audit` | de overtreding komt in de auditlog van de API-server |

`baseline` weigert onder meer privileged containers, `hostPath`-volumes, `hostNetwork`,
`hostPID` en `hostIPC`. `restricted` eist daarnaast `runAsNonRoot`, een seccompprofiel en
het laten vallen van alle capabilities. Dat laatste staat bewust nog op waarschuwen: pas
als de lijst met waarschuwingen leeg is, kan `restricted` afgedwongen worden.

## Hoe je het gebruikt

Er is niets in te stellen per project. De waarden komen uit de clusterconfiguratie:

```python
# opi/core/cluster_config.py, bij sandboxed-local
"pod_security": {"enforce": "baseline", "warn": "restricted", "audit": "restricted"},
```

Een cluster zonder de sleutel `pod_security` krijgt geen enkel pod-security-label. Dat is
de uit-stand, en het is wat `odcn-production` nu doet (zie hieronder).

| cluster | enforce | warn en audit | waarom |
|---|---|---|---|
| `sandboxed-local` | `baseline` | `restricted` | geen hostpaden in wat OPI hier rendert |
| `local` | `privileged` (dwingt niets af) | `baseline` | `ca_certificate.enabled` mount de CA van de node met een `hostPath`, en dat verbiedt `baseline`. Afdwingen zou hier elke componentpod weigeren. Pas als die mount geen hostPath meer is, kan `local` naar `baseline` |
| `odcn-production` | geen labels | geen labels | twee open vragen, zie onder |

Een niveau dat alleen waarschuwt is nog steeds informatie: `local` geeft bij elke apply de
lijst met `baseline`-overtredingen, en die lijst is precies het werk dat af moet voordat
afdwingen daar kan.

De labels worden op twee momenten gezet, zodat ook namespaces van voor deze wijziging ze
krijgen:

1. **bij aanmaak**, via `manifests/namespace.yaml.jinja`;
2. **bij elke verwerking**, in dezelfde `kubectl label`-call die
   `argocd.argoproj.io/managed-by` zet (`apply_labels_to_resource` op de
   KubectlConnector). De lijst staat op één plek, `namespace_labels()` in
   `opi/core/cluster_config.py`, en wordt gebruikt door zowel de ProjectManager als de
   git-monitor.

Dit geldt ook voor de infrastructuurnamespace van een project (de namespace met het
eigen CNPG-cluster): die loopt door dezelfde twee functies.

De startupcontrole leest alle namespacelabels in één `kubectl get namespaces`-call en
slaat de labelcall over zolang alle labels al goed staan, dus een cluster met correcte
labels kost nog steeds geen enkele extra kubectl-aanroep.

## Wat een project hiervan merkt

Een pod die `baseline` overtreedt start niet meer en de deployment blijft in ArgoCD op
`Progressing` staan met een duidelijke weigering van de API-server:

```
pods "x" is forbidden: violates PodSecurity "baseline:latest": privileged
(container "app" must not set securityContext.privileged=true)
```

Dat raakt alleen eigen manifesten die zoiets doen. Voor wat OPI rendert verandert niets op
een cluster dat `baseline` afdwingt, want daar mount het geen hostpaden; op `local` is dat
wel zo, en daarom dwingt dat cluster niets af.

## Wat `restricted` nog tegenhoudt

Gemeten op de sandbox (RC-316, commit `4d82d40a7`). De API-server geeft de lijst zelf,
read-only, met een server-dry-run die het enforce-niveau ophoogt:

```bash
kubectl label ns <namespace> pod-security.kubernetes.io/enforce=restricted \
  --overwrite --dry-run=server
```

Let op de semantiek: bij een labelwijziging toetst PSA bestaande pods alleen tegen het
nieuwe **enforce**-niveau. Een dry-run die alleen `warn` zet geeft dus geen lijst, ook
niet als er pods zijn die `restricted` overtreden.

De uitkomst op de sandbox:

| namespace | pods | `restricted` haalbaar |
|---|---|---|
| `rig-algor-odc-infrastructure` (eigen CNPG-cluster) | `algor-odc-db-1` | ja, geen enkele overtreding |
| `rig-backup-destination` | de backupdienst | ja |
| de projectnamespaces van de e2e-projecten | geen pods | niets te meten |
| `rig-ron` (platformnamespace, buiten scope) | `probeuid` | nee: `allowPrivilegeEscalation != false`, `unrestricted capabilities`, `runAsNonRoot != true`, `seccompProfile` |

De enige overtreding die de meting op dit cluster vond, zit in een met de hand
aangemaakte debugpod (`kubectl run probeuid`), niet in iets dat ZAD genereert. De pods
die ZAD en de operators zelf neerzetten halen `restricted` al.

Dat is nog geen vrijbrief om `restricted` af te dwingen: op de sandbox liep op het moment
van meten geen enkele projectcomponent, dus de pods uit `deployment.yaml.jinja` en uit een
helm- of helmfile-deployment staan nog niet in deze tabel. Die lijst hoort er eerst bij,
uit een cluster waar wel componenten draaien.

### De weigering en de waarschuwing in het echt

Beide helften gemeten in een testnamespace met dezelfde drie labels:

```
$ kubectl apply -f privileged-pod.yaml
Error from server (Forbidden): pods "psa-probe" is forbidden:
violates PodSecurity "baseline:latest": privileged
(container "probe" must not set securityContext.privileged=true)

$ kubectl apply -f hostpath-pod.yaml
Error from server (Forbidden): pods "psa-hostpath" is forbidden:
violates PodSecurity "baseline:latest": host namespaces (hostNetwork=true),
hostPath volumes (volume "host")

$ kubectl apply -f plain-pod.yaml
Warning: would violate PodSecurity "restricted:latest":
allowPrivilegeEscalation != false, unrestricted capabilities,
runAsNonRoot != true, seccompProfile
pod/psa-plain created
```

De derde is het punt van `warn`: de pod start wel, en de overtreding is zichtbaar.

### Bestaande namespaces bij een herstart

Bij het opstarten van OPI op de sandbox kregen alle zeven bestaande projectnamespaces de
labels in dezelfde `kubectl label`-call als `managed-by`:

```
Read 19 namespaces in one call for the startup check
Ensured ArgoCD managed-by and Pod Security labels on namespace: rig-alls5-1tv
...
```

De platformnamespaces (`rig-system`, `rig-ron`, `rig-backup-destination`) bleven
ongelabeld, zoals bedoeld.

## Voor ODCN: twee dingen om eerst na te gaan

Het aanzetten op `odcn-production` is daarna één regel in `cluster_config.py` (de sleutel
`pod_security`). Een beheerder moet het eerst read-only nagaan, want beide kunnen de
verwerking van elk nieuw project laten falen:

1. **Laat Capsule het label toe op een tenant-namespace?** Capsule kan
   namespacelabels beperken (`allowedLabels` op de tenant). Weigert de webhook het
   label, dan faalt de `kubectl label`-call en daarmee de hele verwerking van het
   project. Te zien in de Tenant-resource van een rig-prd-tenant en in de
   CapsuleConfiguration.
2. **Laat de PSA-label-syncer van OpenShift een gezet label staan?** OpenShift
   synchroniseert pod-security-labels op namespaces op basis van de SCC's die de
   serviceaccounts mogen gebruiken. Zet de syncer onze waarde terug, dan flapt het label
   heen en weer. De uitweg is de namespace-annotatie
   `security.openshift.io/scc.podSecurityLabelSync: "false"`, maar of die hier mag staan
   is een platformbeslissing, niet iets wat OPI zelf moet zetten.

## Afhankelijkheden

- Kubernetes 1.25 of nieuwer voor Pod Security Admission (ingebouwd, geen extra
  controller).
- Niets voor OpenShift: daar dwingen de SCC's al af, en deze labels zijn er de tweede
  laag naast, niet de vervanger.

## Buiten scope

- `restricted` afdwingen.
- De platformnamespaces (`rig-system`, `rig-prd-operations`).
