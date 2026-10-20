# Helmfile Single Application Deployment

This document describes the setup for deploying a single application (docs) from the mijn-bureau-infra helmfile repository, rather than deploying the entire suite.

## Problem Statement

The mijn-bureau-infra repository contains a comprehensive helmfile that deploys multiple applications (docs, keycloak, element, nextcloud, openproject, etc.). Deploying the entire suite requires significant cluster resources and is not practical when only one application is needed.

**Challenges:**
- Full helmfile deployment requires substantial CPU/memory resources
- ArgoCD CMP timeout issues when rendering large helmfiles
- Unnecessary applications consuming cluster resources
- Complex debugging when multiple apps are deployed together

## Solution: Selective Helmfile Deployment

We created a custom helmfile entry point that only deploys the docs application while leveraging the existing chart infrastructure from mijn-bureau-infra.

### Architecture Overview

```
rig-cluster-projects-github/
└── projects/
    └── mb-docs-helmfile.yaml          # Project definition with custom helmfile

rig-cluster-application-github/
└── odcn-production/
    └── mb-docs-helmfile/
        └── production/
            ├── helmfile.yaml.gotmpl   # Custom entry point (selective)
            ├── values.sops.yaml       # Encrypted values
            ├── kustomization.yaml     # For Issuer, NetworkPolicy, secrets
            ├── decrypt-sops.yaml      # KSOPS generator for secrets
            └── helmfile/              # Cloned from mijn-bureau-infra
```

### Key Configuration Choices

#### 1. Custom Helmfile Entry Point

Instead of using the root helmfile from mijn-bureau-infra, we define a custom `helmfile.yaml.gotmpl` in the project file that only includes the docs application:

```yaml
# In mb-docs-helmfile.yaml -> helmfile[].files
helmfile.yaml.gotmpl: |
  bases:
    - "helmfile/bases/environment.yaml.gotmpl"

  ---

  bases:
    - "helmfile/bases/default.yaml.gotmpl"

  helmfiles:
    - path: "helmfile/apps/docs/helmfile-child.yaml.gotmpl"
      values:
        - "helmfile/environments/default/*.yaml*"
        - {{ toYaml .Values | nindent 8 }}
```

This approach:
- Uses the existing bases for environment configuration
- Only includes `helmfile/apps/docs/helmfile-child.yaml.gotmpl`
- Passes custom values from our project configuration

#### 2. Disabling Other Applications

In the `helm-values` block, all other applications are explicitly disabled:

```yaml
helm-values:
  application:
    grist:
      enabled: false
    ollama:
      enabled: false
    keycloak:
      enabled: false
    element:
      enabled: false
    collabora:
      enabled: false
    nextcloud:
      enabled: false
    openproject:
      enabled: false
    livekit:
      enabled: false
    meet:
      enabled: false
    conversations:
      enabled: false
    docs:
      enabled: true      # Only docs is enabled
    bureaublad:
      enabled: false
    drive:
      enabled: false
    clamav:
      enabled: false
```

#### 3. Autoscaling Configuration

For test/development deployments, HPA can be disabled to minimize resource usage:

```yaml
helm-values:
  autoscaling:
    horizontal:
      docs:
        backend:
          enabled: false
        celery:
          enabled: false
        frontend:
          enabled: false
```

#### 4. OpenShift Compatibility

Security context adaptations for OpenShift:

```yaml
helm-values:
  global:
    compatibility:
      openshift:
        adaptSecurityContext: force
      omitEmptySeLinuxOptions: true
  security:
    default:
      containerSecurityContext:
        enabled: true
        seLinuxOptions:
        allowPrivilegeEscalation: false
        capabilities:
          drop:
          - ALL
        privileged: false
        readOnlyRootFilesystem: true
        runAsNonRoot: true
        runAsUser:
        runAsGroup:
```

### CMP Plugin Configuration

The ArgoCD CMP plugin was updated to support running **both** kustomize and helmfile in the same deployment:

1. **Kustomize** processes:
   - `kustomization.yaml` - main kustomize file
   - `decrypt-sops.yaml` - KSOPS generator for secrets
   - `issuer-letsencrypt-*.yaml` - Let's Encrypt Issuer
   - `*-network-policy.yaml` - Network policies
   - `*-secret.sops.yaml` - Encrypted service secrets

2. **Helmfile** processes:
   - `helmfile.yaml.gotmpl` - Custom helmfile entry point
   - `values.sops.yaml` - Encrypted helm values

The CMP script detects both and runs them sequentially, combining the output.

### Services Integration

The docs application uses RIG platform services:

| Service | Configuration |
|---------|--------------|
| **Database** | `namespace-postgresql-database` - Dedicated PostgreSQL cluster in infrastructure namespace |
| **Redis** | Shared Redis in `rig-prd-operations` |
| **MinIO** | Shared MinIO in `rig-prd-operations` with dedicated bucket |
| **Keycloak** | Dedicated realm with SSO support template |

Service credentials are:
1. Generated by Operations Manager
2. Stored in `*-secret.sops.yaml` files (encrypted)
3. Injected into `values.sops.yaml` for helmfile
4. Applied to cluster via KSOPS generator

### External-DNS Target

The ingresses of a helmfile deployment come from the chart, not from OPI's
`manifests/ingress.yaml.jinja`, so the `external-dns.alpha.kubernetes.io/target`
annotation that OPI puts on its own ingresses does not reach them. Without it,
external-dns writes a CNAME to the cluster's OCP-router hostname, and that crosses a
zone boundary: Google Public DNS answers such a name with SERVFAIL (EDE 12).

OPI therefore writes the target into the generated helm values, at the hook the
mijn-bureau charts read for their ingress annotations:

```yaml
cluster:
    ingress:
        annotations:
            external-dns.alpha.kubernetes.io/target: router.rijksapp.nl
```

The value comes from `get_external_dns_target_for_hostname` for the deployment's own
hostname (`subdomain` plus `base-domain` of the `publish-on-web` service). Details:

- A hostname in the cluster's own postfix zone gets no annotation; it gets its DNS from
  the OpenShift router and needs no explicit target.
- The values are merged as a base, so annotations already in the project's `helm-values`
  block (for example `cert-manager.io/issuer` or the HAProxy ip_whitelist) stay, and a
  target set there explicitly wins.
- One shared annotation map covers every ingress of the chart. That is enough because a
  deployment publishes within one zone: `docs.rijksapp.nl` and `static-docs.rijksapp.nl`
  share `router.rijksapp.nl`.

Migrating an existing record to the new target is a separate, manual step, described in
`../docs/dns-router-zone-migration.md`.

### Environment Variables for CMP

OPI writes the environment-specific configuration for helmfile as `cmp-env.sops.yaml`, a
map of `KEY: value` encrypted with SOPS on the project key:

```yaml
ENVIRONMENT: production
MIJNBUREAU_MASTER_PASSWORD: <encrypted on the project key>
```

The plugin fetches the project key from the `sops-age-key` secret in the target namespace,
decrypts the file, turns the map into `KEY=value` lines and parses those line by line
instead of sourcing them: shell syntax in a value stays a literal value. Blank lines and
`#` comments are skipped; anything else stops the render.

A deployment that has not been reprocessed since RC-333 may still carry a plain-text
`.cmp-env`; the plugin keeps reading that as a fallback, and OPI removes it as soon as it
rewrites the deployment. A new or changed `.cmp-env` is refused by the git connector, so
no plain-text env-var reaches `zad-deployments` again.

- A key is a shell variable name (`^[A-Za-z_][A-Za-z0-9_]*$`), the same form the schema
  demands of component `env-vars`.
- A value is one line. An AGE-encrypted value may span lines in the project file; OPI
  decrypts it before encrypting the map with SOPS, and the decrypted value must be
  single-line too.
- Variables the plugin reads itself are reserved and refused: anything starting with
  `ARGOCD_`, `SOPS_`, `KUSTOMIZE_`, `HELM_`, `HELMFILE_`, `KUBE`, `BASH`, `LD_` or `GIT_`,
  plus `PATH`, `HOME`, `IFS`, `ENV`, `SHELL`, `PS4` and `PROMPT_COMMAND`. The list lives
  in `CMP_ENV_RESERVED_PREFIXES`/`CMP_ENV_RESERVED_NAMES` (`opi/generation/manifests.py`)
  and in the plugin script; `tests/test_cmp_env_contract.py` pins the two together.
- `ENVIRONMENT` is the one variable a deployment may set that the plugin itself uses: it
  becomes `helmfile -e <value>`, as a single argument. A value with spaces is one
  environment name, never an extra helmfile flag.

OPI applies the same rules when it writes the file, so a bad pair fails at save time with
the key named, not at render time. Without any env-vars no file is written, and an
existing `cmp-env.sops.yaml` is removed, so a deleted variable also leaves the render.
The keys are written in a fixed order, so an unchanged map keeps its existing ciphertext
(see `sops-skip-unchanged-reencryption.md`) and does not churn `zad-deployments`.

### Deployment Flow

1. **Operations Manager** processes `mb-docs-helmfile.yaml`:
   - Clones mijn-bureau-infra repository
   - Creates custom `helmfile.yaml.gotmpl` from project definition
   - Generates `values.sops.yaml` with service credentials and the external-dns target
   - Creates service secret manifests
   - Creates kustomization.yaml with resources
   - Encrypts all sensitive files with SOPS
   - Pushes to application repository

2. **ArgoCD** syncs the application:
   - CMP plugin detects kustomization.yaml and helmfile
   - Decrypts SOPS files using namespace age key
   - Runs `kustomize build` for Issuer, NetworkPolicy, secrets
   - Runs `helmfile template` for application manifests
   - Applies combined output to cluster

### Resource Optimization

With this setup, the docs application deployment uses:
- 3 pods: backend, celery-worker, frontend (with HPA disabled: 1 each)
- 1 nginx pod for routing
- 1 y-provider pod for collaboration

Compared to full mijn-bureau deployment which would include 10+ additional applications.

### Troubleshooting

**Common Issues:**

1. **Credentials mismatch after sync failure**
   - ArgoCD sync failures can leave old credentials in pods
   - Solution: Trigger Operations Manager refresh to update service credentials, then force ArgoCD sync

2. **HPA scaling up unnecessarily**
   - Default HPA config may scale to maxReplicas
   - Solution: Disable HPA in helm-values for test deployments

3. **ACME certificate challenges failing**
   - Network policy must allow port 8089 for ACME solver
   - Solution: Ensure network policy includes both port 80 and 8089

### Files Reference

| File | Purpose |
|------|---------|
| `mb-docs-helmfile.yaml` | Project definition with custom helmfile and helm-values |
| `helmfile.yaml.gotmpl` | Custom entry point selecting only docs app |
| `values.sops.yaml` | Encrypted values including service credentials |
| `kustomization.yaml` | Kustomize config for additional resources |
| `decrypt-sops.yaml` | KSOPS generator referencing secret files |
| `cmp-env.sops.yaml` | Environment variables for helmfile, encrypted on the project key |
| `.helmfile-entry` | Points CMP to helmfile location (if in subfolder) |

### Related Documentation

- [Keycloak YAML Configuration](../docs/keycloak-yaml-configuration.md)
- [Namespace PostgreSQL Database](namespace-postgresql-database.md)
- [Client Access Restriction](client-access-restriction.md)
