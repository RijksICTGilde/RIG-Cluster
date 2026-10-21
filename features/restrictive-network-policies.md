# Restrictive Network Policies

**Status**: Testing Infrastructure Ready (Phase 1 Complete)
**Priority**: In Progress
**Created**: 2026-02-17
**Testing Added**: 2026-10-17

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
`TEMPLATE_SOURCES` elke netpol-template. Van de templates rendert de matrix de
tenant-baseline en `network-policy.yaml.jinja`: die laatste levert de acme-http-policy
(`podSelector: {}` op de hele tenant-namespace, poorten 80 en 8089 voor de
HTTP-01-solver), en omdat alle drie de productiedomeinen `issuer: letsencrypt` dragen
krijgt elk gepubliceerd deployment hem. `service-network-policy.yaml.jinja` valt erbuiten:
die hangt aan de config van cross-domain-access, send-email of vlam. Twee tests in
`test_policy_sources.py` grendelen beide lijsten, dus een nieuw bestand of een nieuwe
template geeft rood tot je hem erin zet.

De odcn-staging-overlays staan wel in de registry, met `None` als namespace: ze draaien op
dezelfde cluster in de Capsule-tenant rig-staging, selecteren alleen pods in de
rig-staging-namespaces, en de matrix toetst odcn-production. Een nieuwe staging-policy moet
er dus ook in, maar verandert de matrix niet.

Eén grens: een endpoint in het cluster heeft geen IP, dus een `ipBlock` dat niet alles
dekt (een smal blok, of `0.0.0.0/0` met `except`) is niet tegen een pod-doel te leggen.
Dan zegt de evaluator wat niet te beoordelen is. Zo'n regel bewijs je met `task
netpol:meet`, waar de pods een adres hebben.

Drie dingen die belangrijk zijn:
- `emergency-restore-allow-all` maakt rig-prd-operations open. MinIO 9001 staat daarom
  open voor tenants, terwijl `allow-minio-access` dat bewust beperkt. Beide staan in de
  matrix, zodat wijzigingen zichtbaar zijn.
- De internetregel van de tenant-baseline (`0.0.0.0/0` en `::/0`) opent aan de
  egress-kant ook intra-cluster verkeer op 80 en 443. Dat de matrix daar geen open regel
  voor heeft, komt van de ingress-kant: de acme-http-policy selecteert elke pod in de
  tenant-namespace en laat alleen 80 en 8089 toe, dus ook een helm-pod zonder
  deployment-label is op 443 niet te bereiken.
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

Een derde constructie staat wel in de policies maar hangt aan geen enkele matrixregel
meer: de `0.0.0.0/0` zonder `except` van de internetregel tegen een pod-IP. Sinds
`tenant-app -> tenant-helm-pod:443` op de ingress-kant dicht gaat, bepaalt de CNI die
uitkomst niet. Geeft acme-http ooit een `podSelector` mee, dan komt die regel terug als
CNI-afhankelijk.

Komt uit zo'n meting een verschil tussen de twee CNIs, zet het hier neer met de regel en
de datum erbij, zodat Calico alleen nodig blijft voor wat aantoonbaar afwijkt.

## Related

- [`features/cross-domain-access.md`](cross-domain-access.md) - The cross-domain-access service writes an **additive** per-component NetworkPolicy next to this baseline, so a project can explicitly open named cross-project peers on named ports without touching the baseline itself.
- `features/shared-resource-warning-acme-network-policy.md` - Related issue with shared network policies across deployments
- `manifests/network-policy.yaml.jinja` - Current network policy template
- `manifests/allow-all-network-policy.yaml.jinja` - Current allow-all template
- `manifests/deployment.yaml.jinja` - Deployment template (source of pod labels)
