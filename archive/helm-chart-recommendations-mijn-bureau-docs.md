# Helm Chart Improvement Recommendations: MijnBureau Docs

This document provides recommendations for the MijnBureau Docs Helm chart maintainers to reduce configuration complexity for deployers.

**Context**: These recommendations are based on real-world deployment experience using the `docs` chart from `github.com/MinBZK/mijn-bureau-infra` (helmfile/apps/docs/charts/docs).

---

## What CAN Be Simplified Today

### Using `commonAnnotations`

The chart supports `commonAnnotations` which are merged into ALL resource annotations (including ingresses). However, there's a **limitation**: the TLS section conditional only checks the specific ingress's `annotations` field, not the merged annotations.

**What works**: Adding labels/annotations to all resources
```yaml
commonAnnotations:
  company.com/team: "platform"
```

**What doesn't work**: Using `commonAnnotations` for cert-manager to enable TLS
```yaml
# This adds the annotation to ingresses BUT won't enable TLS section
commonAnnotations:
  cert-manager.io/cluster-issuer: "letsencrypt-prod"
```

---

## Issue 1: Repetitive Ingress Configuration

### Problem

The chart has **5 separate ingresses** that each require identical configuration:
- `ingress` (main)
- `ingressAdmin`
- `ingressCollaborationWS`
- `ingressCollaborationApi`
- `ingressMedia`

Each ingress requires the deployer to specify:
- `enabled: true`
- `hostname`
- `ingressClassName`
- `tls: true`
- `annotations` (cert-manager annotation MUST be here, not in commonAnnotations)

**Current configuration burden** (example):
```yaml
ingress:
  enabled: true
  hostname: "docs.example.com"
  ingressClassName: "nginx"
  tls: true
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"  # MUST be here

ingressAdmin:
  enabled: true
  hostname: "docs.example.com"     # Repeated
  ingressClassName: "nginx"        # Repeated
  tls: true                        # Repeated
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"  # MUST be here too

ingressCollaborationWS:
  enabled: true
  hostname: "docs.example.com"     # Repeated
  ingressClassName: "nginx"        # Repeated
  tls: true                        # Repeated
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"  # MUST be here too

# ... same for ingressCollaborationApi and ingressMedia
```

### Why commonAnnotations Doesn't Help for TLS

Looking at the ingress templates (e.g., `ingress.yaml:54`):
```yaml
{{- if or (and .Values.ingress.tls
           (or (include "common.ingress.certManagerRequest"
                ( dict "annotations" .Values.ingress.annotations ))  # <-- Only checks ingress.annotations!
               .Values.ingress.selfSigned))
          .Values.ingress.extraTls }}
```

The `certManagerRequest` check only looks at `.Values.ingress.annotations`, not the merged annotations. So `commonAnnotations` is ignored for TLS enablement.

### Quick Fix (for chart maintainers)

Change the TLS conditional to check the merged annotations:
```yaml
# Before (ingress.yaml:54):
{{- $mergedAnnotations := include "common.tplvalues.merge" (dict "values" (list .Values.ingress.annotations .Values.commonAnnotations) "context" .) | fromYaml }}
{{- if or (and .Values.ingress.tls (or (include "common.ingress.certManagerRequest" ( dict "annotations" $mergedAnnotations )) .Values.ingress.selfSigned)) .Values.ingress.extraTls }}
```

This would allow deployers to use `commonAnnotations` for cert-manager across all ingresses.

### Recommendation

Add a **global ingress configuration** that all ingresses inherit from:

```yaml
# Proposed values.yaml structure
global:
  ingress:
    hostname: ""                    # Required: e.g., "docs.example.com"
    ingressClassName: ""            # Required: e.g., "nginx"
    tls: true                       # Default to secure
    annotations: {}                 # Shared annotations (e.g., cert-manager)

# Individual ingresses only need enable flag and path overrides
ingress:
  enabled: true                     # Inherits hostname, className, tls, annotations from global

ingressAdmin:
  enabled: true                     # Inherits from global
  # Override only if different from global:
  # hostname: "admin.example.com"
```

**Template change** (example for ingress.yaml):
```yaml
{{- $hostname := .Values.ingress.hostname | default .Values.global.ingress.hostname }}
{{- $ingressClassName := .Values.ingress.ingressClassName | default .Values.global.ingress.ingressClassName }}
{{- $tls := .Values.ingress.tls | default .Values.global.ingress.tls }}
{{- $annotations := merge .Values.ingress.annotations .Values.global.ingress.annotations }}
```

**Benefit**: Deployers configure ingress settings once instead of 5 times.

---

## Issue 2: PodDisruptionBudget Matches Job Pods

### Problem

The backend PDB selector matches pods with label `app.kubernetes.io/component: backend`.

In `backend-job.yaml` (line 32), Job pods use the same label:
```yaml
labels:
  app.kubernetes.io/component: backend   # Same as Deployment pods
```

This causes Kubernetes to report:
```
CalculateExpectedPodCountFailed: Failed to calculate the number of expected pods:
jobs.batch does not implement the scale subresource
```

The error is harmless but noisy (1653 events in our deployment).

### Recommendation

Use **distinct labels for Job pods** to exclude them from PDB selection:

**In `backend-job.yaml`**, change pod template labels (line 32):
```yaml
# Before:
labels:
  app.kubernetes.io/component: backend

# After:
labels:
  app.kubernetes.io/component: backend-job
```

Or add an additional label to distinguish:
```yaml
labels:
  app.kubernetes.io/component: backend
  app.kubernetes.io/part-of: job    # Distinguishing label
```

Then update `backend-pdb.yaml` selector to exclude jobs:
```yaml
selector:
  matchLabels:
    app.kubernetes.io/component: backend
  matchExpressions:
    - key: app.kubernetes.io/part-of
      operator: NotIn
      values: ["job"]
```

**Benefit**: Eliminates noisy warning events in cluster logs.

---

## Issue 3: No Sensible Defaults for Required Django Settings

### Problem

The chart requires several Django settings that have no defaults but are essential:
- `DJANGO_CONFIGURATION` - Required but defaults to empty
- `DJANGO_SECRET_KEY` - Required but no default/generation
- `DJANGO_ALLOWED_HOSTS` - Required but defaults to empty
- `DJANGO_CSRF_TRUSTED_ORIGINS` - Required for TLS deployments

Deployers must discover these through trial and error when pods crash.

### Recommendation

1. **Provide sensible defaults** where possible:
```yaml
backend:
  envVars:
    DJANGO_CONFIGURATION: "Production"  # Default to production config
    DJANGO_ALLOWED_HOSTS: '{{ .Values.global.ingress.hostname | default .Values.ingress.hostname }}'
```

2. **Auto-generate secrets** if not provided:
```yaml
{{- $secretKey := .Values.backend.envVars.DJANGO_SECRET_KEY | default (randAlphaNum 50) }}
- name: DJANGO_SECRET_KEY
  value: {{ $secretKey | quote }}
```

3. **Derive values from ingress config**:
```yaml
{{- $hostname := .Values.global.ingress.hostname | default .Values.ingress.hostname }}
{{- $protocol := ternary "https" "http" (.Values.global.ingress.tls | default .Values.ingress.tls) }}
- name: DJANGO_CSRF_TRUSTED_ORIGINS
  value: "{{ $protocol }}://{{ $hostname }}"
- name: DJANGO_ALLOWED_HOSTS
  value: "{{ $hostname }}"
```

**Benefit**: Chart works out-of-the-box with minimal configuration.

---

## Issue 4: TLS Disabled by Default

### Problem

All ingresses have `tls: false` by default. In 2024+, TLS should be the default.

### Recommendation

Change default to `tls: true` for all ingresses:

```yaml
ingress:
  tls: true   # Changed from false
```

**Benefit**: Secure by default; deployers opt-out of TLS rather than opt-in.

---

## Issue 5: Collaboration URLs Not Derived from Ingress

### Problem

Several environment variables require manual URL construction:
- `COLLABORATION_API_URL`
- `COLLABORATION_WS_URL`
- `MEDIA_BASE_URL`
- `LOGIN_REDIRECT_URL`

These should be derivable from the ingress hostname and TLS settings.

### Recommendation

Add helper templates to derive these automatically:

```yaml
{{- define "docs.collaboration.apiUrl" -}}
{{- $hostname := .Values.global.ingress.hostname | default .Values.ingress.hostname -}}
{{- $protocol := ternary "https" "http" (.Values.global.ingress.tls | default .Values.ingress.tls) -}}
{{- printf "%s://%s/collaboration/api/" $protocol $hostname -}}
{{- end -}}

{{- define "docs.collaboration.wsUrl" -}}
{{- $hostname := .Values.global.ingress.hostname | default .Values.ingress.hostname -}}
{{- $protocol := ternary "wss" "ws" (.Values.global.ingress.tls | default .Values.ingress.tls) -}}
{{- printf "%s://%s/collaboration/ws/" $protocol $hostname -}}
{{- end -}}
```

Then in the deployment:
```yaml
- name: COLLABORATION_API_URL
  value: {{ include "docs.collaboration.apiUrl" . }}
- name: COLLABORATION_WS_URL
  value: {{ include "docs.collaboration.wsUrl" . }}
```

**Benefit**: Reduces configuration from ~15 URL-related env vars to just the hostname.

---

## Issue 6: Media Ingress Points to Non-Existent Service (BUG)

### Problem

The `ingressMedia` template (`ingress-media.yaml:32`) hardcodes a service name that doesn't exist:

```yaml
backend: {{- include "common.ingress.backend" (dict "serviceName" (printf "%s-nginx" (include "common.names.fullname" .)) "servicePort" "http" "context" $) | nindent 14 }}
```

This generates a backend service name like `mijn-bureau-docs-nginx`, but **no nginx service exists in the chart**. The available services are:
- `<fullname>-backend`
- `<fullname>-frontend`
- `<fullname>-y-provider`

### Impact

- **On nginx ingress controllers** (e.g., Kind): The ingress is created but returns 503 when accessed - fails silently
- **On OpenShift**: The ingress-to-route controller refuses to create a route for an invalid backend - the route is simply missing
- **User experience**: Image uploads may succeed (via `/api/`), but viewing images at `/media/` fails with 404 or "Application not found"

### Evidence

```bash
$ kubectl describe ingress mijn-bureau-docs-media -n <namespace>
...
Rules:
  Host              Path  Backends
  ----              ----  --------
  docs.example.com
                    /media/(.*)   mijn-bureau-docs-nginx:http (<error: services "mijn-bureau-docs-nginx" not found>)
```

### Root Cause Analysis

Looking at commented annotations in `values.yaml` (lines 2435-2438):
```yaml
##   nginx.ingress.kubernetes.io/auth-url: https://docs.127.0.0.1.nip.io/api/v1.0/documents/media-auth/
##   nginx.ingress.kubernetes.io/upstream-vhost: dev-backend-minio.impress.svc.cluster.local:9000
```

The **intended design** appears to be using nginx ingress controller features to authenticate via the backend and proxy to MinIO. However, the template references a Kubernetes Service (`%s-nginx`) that was never created - this is an incomplete implementation or copy/paste error.

### Fix Required

Change line 32 in `templates/ingress-media.yaml`:

```yaml
# Before (broken):
backend: {{- include "common.ingress.backend" (dict "serviceName" (printf "%s-nginx" (include "common.names.fullname" .)) "servicePort" "http" "context" $) | nindent 14 }}

# After (fixed):
backend: {{- include "common.ingress.backend" (dict "serviceName" (printf "%s-backend" (include "common.names.fullname" .)) "servicePort" "http" "context" $) | nindent 14 }}
```

### Temporary Workaround (OpenShift)

When using ArgoCD and unable to modify the helm chart, create a manual Route:

```yaml
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: mijn-bureau-docs-media-fix
  namespace: <namespace>
  annotations:
    # Prevent ArgoCD from pruning this resource
    argocd.argoproj.io/sync-options: Prune=false
spec:
  host: docs.example.com
  path: /media
  to:
    kind: Service
    name: mijn-bureau-docs-backend
    weight: 100
  port:
    targetPort: http
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
  wildcardPolicy: None
```

**Note**: This workaround requires the route to be created outside of ArgoCD management or added to the ArgoCD Application with `Prune=false`.

---

## Issue 7: External Services Configuration Not Streamlined

### Problem

Configuring external services (PostgreSQL, Redis, S3/MinIO) requires setting many individual environment variables with no structure.

### Recommendation

Add structured external service configuration:

```yaml
externalServices:
  postgresql:
    enabled: true
    host: "postgres.example.com"
    port: 5432
    database: "docs"
    username: "docs_user"
    # password via existingSecret or direct value
    existingSecret: "postgres-credentials"
    secretKey: "password"

  redis:
    enabled: true
    host: "redis.example.com"
    port: 6379
    # password via existingSecret
    existingSecret: "redis-credentials"

  s3:
    enabled: true
    endpoint: "https://s3.example.com"
    bucket: "docs-media"
    region: "us-east-1"
    existingSecret: "s3-credentials"
```

Then in templates, generate the appropriate env vars:
```yaml
{{- if .Values.externalServices.postgresql.enabled }}
- name: DB_HOST
  value: {{ .Values.externalServices.postgresql.host | quote }}
- name: DB_PORT
  value: {{ .Values.externalServices.postgresql.port | quote }}
# ... etc
{{- end }}
```

**Benefit**: Structured configuration is self-documenting and easier to validate.

---

## Summary: Ideal Minimal Configuration

With these improvements, a production deployment could be configured with:

```yaml
global:
  ingress:
    hostname: "docs.example.com"
    ingressClassName: "nginx"
    annotations:
      cert-manager.io/cluster-issuer: "letsencrypt-prod"

externalServices:
  postgresql:
    host: "postgres.cluster.local"
    database: "docs"
    existingSecret: "docs-postgres"
  redis:
    host: "redis.cluster.local"
    existingSecret: "docs-redis"
  s3:
    endpoint: "https://minio.cluster.local:9000"
    bucket: "docs"
    existingSecret: "docs-s3"

backend:
  envVars:
    OIDC_RP_CLIENT_ID: "docs"
    # OIDC_RP_CLIENT_SECRET via existingSecret
```

**Current configuration required**: ~150 lines
**Proposed configuration required**: ~25 lines

---

## References

- Chart source: https://github.com/MinBZK/mijn-bureau-infra/tree/main/helmfile/apps/docs/charts/docs
- Bitnami common library patterns: https://github.com/bitnami/charts/tree/main/bitnami/common
