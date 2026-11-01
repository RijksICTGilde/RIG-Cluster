# Restrictive Network Policies

**Status**: Default-deny per projectnamespace uitgerold (RC-343); de fasen 2 tot 5 hieronder zijn nog ontwerp
**Priority**: In Progress
**Created**: 2026-02-17
**Testing Added**: 2026-10-17
**Default-deny**: 2026-10-18

## Problem Statement

The current network policies are too permissive. The `allow-all-network-policy.yaml.jinja` template opens all ingress and egress for every pod in a namespace (`podSelector: {}` with empty ingress/egress rules). The `network-policy.yaml.jinja` template restricts by port but places no restriction on the source of traffic.

This is problematic because multiple deployments can share the same namespace. Under the current policies, any deployment in the namespace can freely communicate with any other deployment in that namespace, and there is no isolation between unrelated workloads.

## Current State

### `allow-all-network-policy.yaml.jinja`
- Applies to all pods in the namespace (`podSelector: {}`)
- Allows all ingress from any source
- Allows all egress to any destination
- Effectively disables network policy enforcement for the namespace

### `network-policy.yaml.jinja`
- Optionally scoped to pods via `pod_selector`
- Restricts ingress to specific ports
- Does **not** restrict the source of ingress traffic (no `from` clause)
- Does **not** define egress rules

### Current Pod Labels (from `deployment.yaml.jinja`)

```yaml
labels:
  app: "{{ name }}"              # deployment-specific label
  project: "{{ project.name }}"  # project-level label
  component: application
  network-policy: allow-db-access
```

These labels are already in place and can be used for network policy selectors.

## Goal

Implement label-based network policies that restrict traffic so that only related objects within a deployment can communicate with each other, while still allowing necessary access to shared RIG infrastructure services.

---

## Design

### Label-Based Isolation

Each deployment already labels its pods with `app: <deployment-name>`. Network policies use these labels to scope traffic:

- **Ingress**: Only allow traffic from pods with the same `app` label (same deployment), plus the ingress controller
- **Egress**: Only allow traffic to pods with the same `app` label, plus required infrastructure

### Required Infrastructure Access

| Service | Namespace | Ports | Direction | Purpose |
|---------|-----------|-------|-----------|---------|
| Ingress controller | `ingress-nginx` | 80, 443 | Ingress | External traffic routing |
| PostgreSQL (CNPG) | project namespace or `rig-system` | 5432 | Egress | Database access |
| Keycloak | `rig-system` or infrastructure ns | 8080, 8443 | Egress | SSO/authentication |
| MinIO | infrastructure namespace | 9000 | Egress | Object storage |
| CoreDNS | `kube-system` | 53 (TCP+UDP) | Egress | DNS resolution |
| Prometheus | infrastructure namespace | 9090 | Ingress | Metrics scraping |
| External HTTPS | any | 443 | Egress | Outbound API calls |

---

## Implementation

### Phase 1: Default-Deny Base Policy

**File**: `manifests/default-deny-network-policy.yaml.jinja` (new)

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: {{ namespace }}
  labels:
    project: "{{ project_name }}"
    managed-by: rig
spec:
  podSelector: {}
  policyTypes:
    - Ingress
    - Egress
```

This blocks all traffic by default. Per-deployment policies then layer on top to allow only what is needed.

### Phase 2: Per-Deployment Restrictive Policy

**File**: `manifests/deployment-network-policy.yaml.jinja` (new)

Replace `allow-all-network-policy.yaml.jinja` and `network-policy.yaml.jinja` with a single comprehensive template:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ name }}-network-policy
  namespace: {{ namespace }}
  labels:
    app: "{{ name }}"
    project: "{{ project_name }}"
    managed-by: rig
spec:
  podSelector:
    matchLabels:
      app: "{{ name }}"
  policyTypes:
    - Ingress
    - Egress

  ingress:
    # 1. Allow traffic from pods in the same deployment
    - from:
        - podSelector:
            matchLabels:
              app: "{{ name }}"

    # 2. Allow traffic from the ingress controller
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: ingress-nginx
      ports:
        - protocol: TCP
          port: {{ application_port | default(8080) }}

    # 3. Allow Prometheus metrics scraping
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ infrastructure_namespace | default('rig-system') }}
      ports:
        - protocol: TCP
          port: {{ metrics_port | default(9090) }}

  egress:
    # 1. Allow traffic to pods in the same deployment
    - to:
        - podSelector:
            matchLabels:
              app: "{{ name }}"

    # 2. DNS resolution (CoreDNS in kube-system)
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53

    # 3. PostgreSQL database access
{% if uses_database %}
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ database_namespace }}
      ports:
        - protocol: TCP
          port: 5432
{% endif %}

    # 4. Keycloak SSO access
{% if uses_keycloak %}
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ keycloak_namespace | default('rig-system') }}
      ports:
        - protocol: TCP
          port: 8080
        - protocol: TCP
          port: 8443
{% endif %}

    # 5. MinIO object storage access
{% if uses_minio %}
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ minio_namespace }}
      ports:
        - protocol: TCP
          port: 9000
{% endif %}

    # 6. External HTTPS (outbound API calls)
{% if allow_external_https | default(true) %}
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8
              - 172.16.0.0/12
              - 192.168.0.0/16
      ports:
        - protocol: TCP
          port: 443
{% endif %}
```

### Phase 3: Template Variable Population

**File**: `opi/manager/manifest_manager.py` (modify)

The manifest manager needs to populate the new template variables by inspecting the project's service configuration:

```python
def get_network_policy_context(
    self, project_data: dict, deployment: dict, component: dict
) -> dict:
    """Derive network policy template variables from project configuration."""
    services = component.get("uses-services", [])
    service_names = []
    for s in services:
        if isinstance(s, str):
            service_names.append(s)
        elif isinstance(s, dict):
            service_names.extend(s.keys())

    deployment_services = component.get("services", {})

    return {
        "uses_database": (
            "postgresql" in service_names
            or "persistent-storage" in service_names
            or "database" in deployment_services
        ),
        "database_namespace": self._get_database_namespace(project_data, deployment),
        "uses_keycloak": "keycloak" in service_names,
        "keycloak_namespace": self._get_keycloak_namespace(project_data),
        "uses_minio": (
            "minio" in service_names
            or "object-storage" in service_names
        ),
        "minio_namespace": self._get_minio_namespace(project_data),
        "allow_external_https": component.get("allow-external-https", True),
        "application_port": component.get("ports", {}).get("inbound", [8080])[0]
            if isinstance(component.get("ports", {}).get("inbound"), list)
            else 8080,
        "metrics_port": component.get("metrics-port", 9090),
    }
```

### Phase 4: Manifest Generation Integration

**File**: `opi/manager/project_manager.py` (modify)

In the deployment processing logic, replace calls to generate `allow-all-network-policy.yaml.jinja` and `network-policy.yaml.jinja` with the new templates:

```python
# Replace:
# manifests.append(render("allow-all-network-policy.yaml.jinja", ...))
# manifests.append(render("network-policy.yaml.jinja", ...))

# With:
manifests.append(render("default-deny-network-policy.yaml.jinja", {
    "namespace": namespace,
    "project_name": project_name,
}))

np_context = manifest_manager.get_network_policy_context(project_data, deployment, component)
manifests.append(render("deployment-network-policy.yaml.jinja", {
    "name": deployment_name,
    "namespace": namespace,
    "project_name": project_name,
    **np_context,
}))
```

### Phase 5: Legacy Mode Opt-In

**File**: Project YAML configuration

For projects that genuinely need open policies during migration:

```yaml
deployments:
  - name: staging
    network-policy-mode: permissive   # "restrictive" (default) | "permissive"
```

When `network-policy-mode: permissive`, generate the old `allow-all-network-policy.yaml.jinja` instead of the restrictive policies.

---

## Testing Strategy

### Pre-Deployment Validation

Before enabling restrictive policies on a deployment:

```bash
# 1. Apply the policy in "audit" mode by deploying alongside the existing allow-all
# The restrictive policy won't block anything while allow-all exists
kubectl apply -f generated-deployment-network-policy.yaml

# 2. Check that the policy was accepted
kubectl get networkpolicy -n <namespace>

# 3. Remove the allow-all policy
kubectl delete networkpolicy allow-all -n <namespace>

# 4. Verify application still works
curl -k https://<deployment-url>/
# Check logs for connection errors
kubectl logs -n <namespace> -l app=<deployment-name> --tail=50
```

### Automated Verification Script

**File**: `scripts/verify-network-policies.sh` (new)

```bash
#!/bin/bash
# Verify network policy doesn't break a deployment
NAMESPACE=$1
DEPLOYMENT=$2

echo "=== Checking DNS resolution ==="
kubectl exec -n $NAMESPACE deploy/$DEPLOYMENT -- nslookup kubernetes.default.svc.cluster.local

echo "=== Checking database connectivity ==="
kubectl exec -n $NAMESPACE deploy/$DEPLOYMENT -- nc -zv rig-db-rw.rig-system.svc.cluster.local 5432

echo "=== Checking external HTTPS ==="
kubectl exec -n $NAMESPACE deploy/$DEPLOYMENT -- nc -zv google.com 443

echo "=== Checking Keycloak ==="
kubectl exec -n $NAMESPACE deploy/$DEPLOYMENT -- nc -zv keycloak.rig-system.svc.cluster.local 8080

echo "=== Checking intra-deployment communication ==="
# List all pods with the same app label
kubectl get pods -n $NAMESPACE -l app=$DEPLOYMENT -o wide
```

### Test Matrix

| Test Case | Expected Result |
|-----------|----------------|
| Pod A in deployment X -> Pod B in deployment X | Allowed (same `app` label) |
| Pod in deployment X -> Pod in deployment Y (same namespace) | **Blocked** |
| External HTTP -> deployment via ingress | Allowed |
| Deployment -> PostgreSQL on port 5432 | Allowed (if `uses_database`) |
| Deployment -> PostgreSQL on port 5432 | **Blocked** (if not `uses_database`) |
| Deployment -> Keycloak on port 8080 | Allowed (if `uses_keycloak`) |
| Deployment -> external HTTPS (port 443) | Allowed |
| Deployment -> external HTTP (port 80) | **Blocked** |
| Deployment -> arbitrary internal service | **Blocked** |
| Prometheus -> deployment metrics port | Allowed |

---

## Migration Plan

### Rollout Order

1. **New deployments first**: All newly created deployments get restrictive policies by default
2. **Non-production existing**: Enable on staging/development deployments
3. **Production with testing**: Enable per-project after running the verification script
4. **Deprecate allow-all**: Remove `allow-all-network-policy.yaml.jinja` once all projects are migrated

### Rollback

If a deployment breaks after enabling restrictive policies:

```bash
# Quick fix: apply allow-all policy to the namespace
kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: emergency-allow-all
  namespace: <namespace>
spec:
  podSelector: {}
  ingress:
    - {}
  egress:
    - {}
  policyTypes:
    - Ingress
    - Egress
EOF
```

Then set `network-policy-mode: permissive` in the project YAML and redeploy to persist the change.

---

## Configuration (config.py)

```python
# Network policy defaults
NETWORK_POLICY_MODE: str = "restrictive"   # "restrictive" | "permissive"
NETWORK_POLICY_ALLOW_EXTERNAL_HTTPS: bool = True
```

---

## Files Summary

### New Files

| File | Purpose |
|------|---------|
| `manifests/default-deny-network-policy.yaml.jinja` | Default-deny base policy per namespace |
| `manifests/deployment-network-policy.yaml.jinja` | Per-deployment restrictive policy with conditional infrastructure access |
| `scripts/verify-network-policies.sh` | Verification script for testing policies |

### Modified Files

| File | Change |
|------|--------|
| `opi/manager/manifest_manager.py` | Add `get_network_policy_context()` to derive service dependencies |
| `opi/manager/project_manager.py` | Replace allow-all generation with restrictive policy generation |
| `opi/core/config.py` | Add `NETWORK_POLICY_MODE`, `NETWORK_POLICY_ALLOW_EXTERNAL_HTTPS` |

### Deprecated Files

| File | Replacement |
|------|-------------|
| `manifests/allow-all-network-policy.yaml.jinja` | `manifests/default-deny-network-policy.yaml.jinja` + `manifests/deployment-network-policy.yaml.jinja` |
| `manifests/network-policy.yaml.jinja` | Merged into `manifests/deployment-network-policy.yaml.jinja` |

---

## Dependencies

- Deployment labels must be consistent across all manifest templates (already in place: `app`, `project`, `component`)
- Infrastructure namespace labels (`kubernetes.io/metadata.name`) must exist (Kubernetes 1.21+ sets this automatically)
- The operations manager needs to know which infrastructure services a deployment depends on (derived from `uses-services` in project YAML)

## Verification

1. **Default-deny works**: Deploy without per-deployment policy, verify all traffic is blocked
2. **Intra-deployment allowed**: Two pods with same `app` label can communicate
3. **Cross-deployment blocked**: Two pods with different `app` labels in same namespace cannot communicate
4. **Database access conditional**: Deployment with `uses_database=true` connects; without it, connection refused
5. **DNS works**: All pods can resolve DNS (port 53 to kube-system)
6. **External HTTPS works**: Pods can reach external APIs on port 443
7. **Ingress works**: External traffic reaches pods via NGINX ingress controller
8. **Legacy mode**: `network-policy-mode: permissive` still generates the old allow-all policy

## De toets: de flowmatrix

Elke stap hierboven zet verbindingen dicht, en tot nu toe bleek pas bij het toepassen
welke. Dat heeft op 2026-06-10 de operations-namespace onderbroken. De toets staat in
`operations-manager/python/tests/netpol/`: een flowmatrix met een evaluator en een
meting op een echt cluster.

**De unit test** evalueert een YAML-lijst verbindingen tegen de policies uit git:

```bash
cd operations-manager/python
uv run pytest tests/netpol -q --noconftest
```

De matrix beschrijft de huidige toestand. Een PR die een policy aanraakt zet hier de
regels om die hij bedoelt te veranderen. Namespace-labels mag je weglaten; de evaluator
vult `kubernetes.io/metadata.name` zelf aan.

**Wat de matrix dekt.** `PLATFORM_POLICY_SOURCES` in `tests/netpol/policies.py` noemt elk
policy-bestand in de repo met de namespace waarin productie het krijgt, en
`TEMPLATE_SOURCES` elke netpol-template. De matrix rendert alle drie de templates: de
tenant-baseline, `network-policy.yaml.jinja` (de acme-http-policy, sinds RC-336 alleen de
HTTP-01-solver op 8089 en alleen vanaf de router, en omdat alle drie de productiedomeinen
`issuer: letsencrypt` dragen krijgt elk gepubliceerd deployment hem) en
`service-network-policy.yaml.jinja`, met de config van cross-domain-access, send-email en
vlam uit `service_policies()`. Twee tests in `test_policy_sources.py` grendelen beide
lijsten, dus een nieuw bestand of een nieuwe template geeft rood tot je hem erin zet.

De odcn-staging-overlays staan wel in de registry, met `None` als namespace: ze draaien op
dezelfde cluster in de Capsule-tenant rig-staging, selecteren alleen pods in de
rig-staging-namespaces, en de matrix toetst odcn-production. Een nieuwe staging-policy moet
er dus ook in, maar verandert de matrix niet.

Eén grens: een endpoint in het cluster heeft geen IP, dus een `ipBlock` dat niet alles
dekt (een smal blok, of `0.0.0.0/0` met `except`) is niet tegen een pod-doel te leggen.
Dan zegt de evaluator wat niet te beoordelen is. Zo'n regel bewijs je met `task
netpol:meet`, waar de pods een adres hebben.

Drie dingen die belangrijk zijn:
- `emergency-restore-allow-all` maakt rig-prd-operations in beide richtingen open, dus
  de ingress van een platformdienst is daar geen grens. De egress van de tenant is dat
  wel: sinds RC-336 is MinIO 9001 dicht omdat de ops-egress MinIO alleen op 9000 noemt.
- De internetregel van de tenant-baseline (`0.0.0.0/0` en `::/0`) opent ook intra-cluster
  verkeer op 80 en 443 zolang een cluster geen `cluster_cidrs` heeft. Vul die sleutel bij
  een nieuw cluster voor BEIDE adresfamilies: een `except` op alleen IPv4 laat `::/0`
  catch-all, en een dual-stack pod blijft dan bereikbaar via zijn IPv6-adres.
- Een test muteert de internet-regel en controleert dat rood wordt. Zonder die test zegt
  groen niets.

**De meting** is dezelfde matrix op een echt cluster:

```bash
task netpol:meet              # standaard: kindnet
task netpol:meet CNI=calico   # voor de regels hieronder
```

De task maakt een dual-stack wegwerpcluster, rolt de policies uit, zet per endpoint een
pod neer en probeert per regel een TCP-verbinding. Draai dit bij elke policy-wijziging,
voordat je een PR opent. Wijkt een regel af, dan klopt de evaluator, de matrix of de CNI
niet.

**Waarom kindnet standaard is.** Kindnet handhaaft NetworkPolicies; dat is twee keer
gemeten op de sandbox (2026-07-28, zie
`features/bio-network-access-no-vpn-compliance.md`, en RC-144 op 2026-08-20, zie
`features/cross-domain-access.md`). Er is tot nu toe geen enkele constructie uit deze
matrix waarvan is aangetoond dat kindnet hem mist. Calico is daarom niet de standaard,
maar de tweede meting voor de regels waar het antwoord van de CNI afhangt en nog niet
gemeten is:

- `tenant-app -> internet-v6:443` en `:25` hangen aan `::/0`, en een CNI hoeft de twee
  adresfamilies niet gelijk te behandelen.
- De poorten in de policies staan zonder protocol, wat volgens de API TCP betekent. Een
  CNI die dat anders invult, laat dat op elke regel zien.
- `tenant-app -> tenant-helm-pod:443` legt de internetregel tegen een pod-IP. Die regel
  stond tot RC-336 in de matrix, maar nu odcn een `cluster_cidrs` heeft, draagt de regel
  een `except` en is hij niet meer te berekenen. Hij staat als berekening in
  `TestDeExceptSluitHetClusterUit`. Meten kan alleen op een cluster met de pod-CIDR van
  odcn, en het wegwerpcluster heeft die van Kind; gemeten is hij dus niet.

Komt uit zo'n meting een verschil tussen de twee CNIs, zet het hier neer met de regel en
de datum erbij, zodat Calico alleen nodig blijft voor wat aantoonbaar afwijkt.

## De ops-egress per service, en wat je na de release waarneemt

Sinds RC-336 noemt de tenant-baseline de gedeelde services van de operations-namespace
stuk voor stuk, met hun pod-labels en hun poort, in plaats van de hele namespace open te
zetten. De lijst staat in `OPS_EGRESS` in `opi/core/cluster_config.py` en is per cluster
aan te passen via de sleutel `ops_egress`. Dezelfde PR zet de pod- en service-CIDR van
het cluster in de `except` van de internetregel, uit de sleutel `cluster_cidrs`. Die
sleutel is op `local` en `sandboxed-local` gevuld met de Kind-defaults, en op
`odcn-production` en `odcn-staging` (dezelfde cluster) met de OpenShift-CIDRs, op
2026-10-21 read-only afgeleid uit de pod- en service-IP's in rig-prd-operations,
rig-prd-regel-k4c en rig-staging-operations omdat `network.config/cluster` niet leesbaar
is. Noemt een cluster de sleutel niet, dan waarschuwt `get_cluster_cidrs()` bij elke
generatie en blijft de internetregel zo ruim als hij was.

Die `except` sluit ook de ingress-controller uit, en die is wel een bestemming: een app
bereikt Keycloak op zijn publieke hostnaam, en die naam wijst naar de router. De baseline
heeft daarom een eigen egressregel naar de namespace van de ingress-controller op 80 en
443, met dezelfde selector als de ingress-regel. Zonder die regel gaat het hele OIDC-pad
dicht zodra een cluster `cluster_cidrs` noemt: de dienst keycloak, de auth-wall sidecar en
de db-console-pod lopen er alle drie over.

### Wat RC-336 openliet, en RC-343 sloot

De ACME-policy selecteerde tot RC-336 elke pod in de namespace (`podSelector: {}`,
ingress op 80 en 8089). Daarmee beperkte hij ook de pods zonder `deployment:`-label, en
dat label dragen helm- en helmfile-workloads niet. Nu de policy alleen de solver
selecteert, viel zo'n pod terug op default-allow. De default-deny per projectnamespace
hieronder sluit dat gat; de matrix houdt het zichtbaar met de regels van
`vreemde-helm-pod`.

### Wat waar te meten is

De `except` van de internetregel en de smalle ACME-policy zijn op verschillende plekken
te bewijzen, en niet elk stuk kan op de sandbox:

- **De gegenereerde policy** (de vorm die OPI wegschrijft) toetst
  `tests/test_acme_network_policy_emit.py` op de helper, en op de sandbox de inhoud van
  het bestand in `zad-deployments`. Daarvoor is geen publiek bereikbaar cluster nodig:
  het is een generatiestap. Wel een deployment met een base domain dat een
  `issuer: letsencrypt` draagt, want anders komt `effective_issuer()` op None uit en
  schrijft OPI helemaal geen ACME-policy. Op `sandboxed-local` is dat niet de
  standaardzone `sandbox.rijksapp.dev` (die heeft geen issuer), maar
  `robbertuittenbroek.nl` of een eigen domein van het project.
- **Of de challenge slaagt** is op de sandbox niet te meten: Let's Encrypt moet de
  solverpod van buiten bereiken, en de sandbox heeft geen publieke DNS of inkomend
  verkeer. Dat stuk blijft aan productie.
- **De `except` tegen een echt pod-adres** vraagt een CNI die hem handhaaft, dus
  `task netpol:meet CNI=calico`. De unit test rekent hem uit, zie
  `TestDeExceptSluitHetClusterUit`.

Op productie gemeten (RC-336, 2026-10-21, door de reviewfase in `rig-prd-regel-k4c`):
de router-selector die de baseline gebruikt (namespace `openshift-ingress` plus
`ingresscontroller.operator.openshift.io/deployment-ingresscontroller=rig`) laat het
webverkeer naar elke tenant-app vandaag al toe, en elk podlabel uit `OPS_EGRESS` vindt op
productie precies een pod. De egressregels landen dus op de pods die ze bedoelen.

### Uitrolvolgorde

De baseline wordt per deployment opnieuw gegenereerd bij de eerstvolgende verwerking van
het projectbestand. Er is dus geen moment waarop de vloot omslaat: de wijziging
verspreidt zich deployment voor deployment, en een project dat niet verwerkt wordt houdt
zijn oude policy. Wie het wil versnellen verwerkt de projecten, bijvoorbeeld via de
API; wie het wil terugdraaien zet de regel in de template terug en verwerkt opnieuw.

Drie dingen zijn na de release het waarnemen waard:

- **De readiness van tenant-pods die de gedeelde database gebruiken.** Een pod die niet
  meer bij `rig-db-rw` komt faalt op zijn readiness-probe of blijft hangen op connect.
  De regel selecteert op `cnpg.io/cluster: rig-db`; een app die via een pooler praat
  draagt een ander label (issue #116) en heeft een eigen regel nodig.
- **Het inloggen van apps met de dienst keycloak.** Dat pad loopt via de router, en de
  egressregel daarvoor staat naast de `except` van de internetregel. Mist die regel, dan
  haalt de app zijn discovery-URL niet op en komt een gebruiker niet voorbij het inloggen.
- **De foutmeldingen van projecten met sleep-mode.** De waker roept de API van OPI aan
  op poort 8000 (`ops_api_url`). Komt die niet door, dan wordt een slapende app niet
  meer wakker, en dat is aan de waker-logs te zien voordat een gebruiker het meldt.

## De operations-namespace: per component, en de allow-all krimpt

`rig-prd-operations` liep tot 10 juni 2026 onder een allow-all over elke pod, en dat
maskeerde elke restrictieve policy in die namespace. Het beleid staat nu per workload in
de overlay van zijn component:

| workload | policy |
|---|---|
| OPI | `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/network-policy.yaml` |
| ArgoCD | `bootstrap/rig-system/kustomize/overlays/odcn-production/network-policies/argocd-network-policy.yaml` |
| cert-manager-solver | `bootstrap/rig-system/kustomize/overlays/odcn-production/network-policies/acme-solver-network-policy.yaml` |
| rig-db | `infrastructure/bootstrap/infrastructure/postgresql/database/overlays/odcn/network-policies/` |
| Redis, MinIO | de gelijknamige overlays onder `infrastructure/bootstrap/infrastructure/` |
| Keycloak | `infrastructure/bootstrap/infrastructure/keycloak/controller/overlays/odcn/network-policies/` |
| Prometheus, kube-state-metrics | `infrastructure/bootstrap/infrastructure/prometheus/controller/overlays/odcn/network-policies/` |
| external-dns | `infrastructure/bootstrap/infrastructure/external-dns/controller/overlays/odcn/network-policies/` |

`emergency-restore-allow-all.yaml` selecteert niet langer elke pod, maar alleen de pods die
nog niet zijn overgezet: een `matchExpressions`-regel met `NotIn` per labelsleutel. `NotIn`
matcht ook een pod zonder dat label, dus een onbekende workload valt nog onder de allow-all.

**De uitsluitlijst is de volgorde.** Elke stap is een eigen PR van EEN regel in die lijst,
uitgerold via ArgoCD, met daarna een dag waarnemen op de bestaande signalen:

| stap | workload | waar je op let |
|---|---|---|
| a | kube-state-metrics, external-dns | ArgoCD-health van production-infrastructure, en of de DNS-records van nieuwe Ingresses nog verschijnen |
| b | Prometheus (gedaan, RC-409) | de scrape-doelen op `/targets`, en de resource- en facturatiepagina's van OPI |
| c | MinIO, Redis | de readiness van beide pods, en de OPI-logs op S3- en sessionfouten |
| d | Keycloak | een echte login op keycloak.rijksapp.nl, en de OPI-logs op realmbeheer |
| e | ArgoCD | de sync- en health-status van elke Application |
| f | OPI | de readiness van de pod, de portal zelf, en het waker-endpoint van sleep-mode |
| g | rig-db | de readiness van het CNPG-cluster en de logs van OPI en Keycloak |

Na (g) selecteert de allow-all niets bekends meer en mag het bestand weg, in een eigen PR,
na een week zonder afwijking. Terugdraaien is de regel weer uit de uitsluitlijst halen.

Twee dingen die bij deze volgorde horen:
- De operations-namespace draagt opzettelijk niet het label `created-by: operations-manager`
  (issue #83 wordt daarom afgewezen): dat label is de selector waarmee policies
  tenant-namespaces herkennen, dus met dat label zou de namespace zichzelf als tenant
  toelaten. Een policy die deze namespace als bron nodig heeft, selecteert op
  `kubernetes.io/metadata.name`.
- De ingress van OPI is versmald tot de router, Prometheus en het waker-endpoint voor
  tenants. Dat is stap 3 van
  `features/futures/client-ip-vertrouwen-achter-de-router.md` (issue #195).

## De default-deny per projectnamespace (RC-343)

Elke projectnamespace krijgt sinds RC-343 drie namespace-brede NetworkPolicies, naast de
tenant-baseline die per deployment blijft bestaan. Geen pod valt nog terug op Kubernetes'
default-allow (BIO2 8.22, defense in depth).

| Policy | Template | Wat hij doet |
|---|---|---|
| `default-deny-network-policy` | `manifests/default-deny-network-policy.yaml.jinja` | `podSelector: {}`, beide richtingen, geen regels: alles dicht tenzij een andere policy het opent |
| `unlabeled-baseline-network-policy` | `manifests/tenant-baseline-network-policy.yaml.jinja` (render met `unlabeled_selector`) | Dezelfde rechten als een deployment, voor pods ZONDER `deployment:`-label |
| `dedicated-database-network-policy` | `manifests/dedicated-database-network-policy.yaml.jinja` | Een toegewijd PostgreSQL-cluster in deze namespace: 5432 vanuit de eigen namespace, plus de CNPG-operator |

### Wat dit voor een helm-chart verandert

Een pod zonder `deployment:`-label houdt DNS, de gedeelde services uit `ops_egress`,
internet op 80 en 443, en ingress vanaf de router. Hij verliest:

- **ingress vanuit andere tenant-namespaces**: een chart die vanuit een ander project
  wordt aangeroepen heeft een regel nodig via cross-domain-access;
- **egress naar de pods van een deployment in dezelfde namespace**, en andersom. Praat
  je chart met een OPI-deployment naast zich, dan is dat een regel die er niet is. De
  uitzondering is het toegewijde databasecluster: dat bereiken beide kanten op 5432;
- **egress naar alles buiten DNS, de gedeelde services en 80/443**, dus bijvoorbeeld een
  eigen poort op een externe host.

Pods onderling zonder dat label mogen elkaar wel bereiken, dus een chart met meerdere
workloads blijft intern werken.

Zet een chart zelf een `deployment:`-label met een waarde die geen deployment van het
project is, dan selecteert geen van beide baselines die pod: de per-deployment baseline
kijkt naar de echte naam, de baseline zonder label naar het ONTBREKEN van het label. Zo'n
pod valt alleen onder de default-deny en staat dus volledig dicht. Laat het label weg, of
geef hem de naam van de deployment waar hij bij hoort. De matrix houdt dat zichtbaar met
de regels van `tenant-vreemd-label-pod`.

### De restore-afweging

Restore- en kloonpods dragen geen `deployment:`-label en vallen dus onder de baseline
zonder label. Ze houden daarmee 80 en 443 naar buiten, zodat een restore naar een externe
bestemming mogelijk blijft. Dat is een bewuste keuze en geen omissie: de technische review
noemt zo'n restore verdedigbaar mits die open egress een expliciete keuze blijft. Het
beperken ervan hoort bij een egress-allowlist per project (#85), niet hier.

### Open punt: admission webhooks en operators

Twee typen charts werken niet onder deze policies: een admission webhook, omdat de
kube-apiserver de webhookpod aanroept en geen ingress-regel de control plane toelaat, en
een operator, omdat `kubernetes.default.svc` in de service-CIDR valt die de internetregel
juist uitsluit. Hier is daar bewust niets voor gebouwd: een oplossing kiest tussen een
egressregel naar de apiserver en een ingressregel voor de control plane, en dat hoort bij
de egress-allowlist per project (#85). De infrastructuurnamespace kent dezelfde grenzen al.

## Related

- [`features/cross-domain-access.md`](cross-domain-access.md) - The cross-domain-access service writes an **additive** per-component NetworkPolicy next to this baseline, so a project can explicitly open named cross-project peers on named ports without touching the baseline itself.
- `features/shared-resource-warning-acme-network-policy.md` - Related issue with shared network policies across deployments
- `manifests/default-deny-network-policy.yaml.jinja` - Namespace-wide default-deny base policy
- `manifests/tenant-baseline-network-policy.yaml.jinja` - Per-deployment and unlabeled-pod baseline policy
- `manifests/dedicated-database-network-policy.yaml.jinja` - Policy for dedicated database clusters
- `manifests/network-policy.yaml.jinja` - Per-component service-specific policy
- `manifests/deployment.yaml.jinja` - Deployment template (source of pod labels)
