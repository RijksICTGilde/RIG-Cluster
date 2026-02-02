# Config Hash Pod Restart

## Overview

By default, Kubernetes does not restart pods when ConfigMaps or Secrets they reference change. Pods only restart when the Deployment/StatefulSet/DaemonSet specification itself changes. This feature automatically injects a hash annotation into workload pod templates, causing pods to restart when configuration changes.

## The Problem

When a Secret or ConfigMap is updated:

1. Kubernetes updates the resource in the cluster
2. Pods using that resource via `envFrom` or volume mounts **do not restart**
3. The pod continues running with stale configuration
4. Manual intervention is required to restart pods

This is especially problematic for:
- Credential rotation (pods keep using old credentials)
- Configuration updates (pods ignore new settings)
- SOPS-encrypted secrets (decrypted values change but pods don't notice)

## The Solution

The ArgoCD CMP (Config Management Plugin) automatically:

1. Calculates a SHA256 hash of all Secrets and ConfigMaps in the rendered manifests
2. Injects this hash as an annotation on all Deployment, StatefulSet, and DaemonSet pod templates
3. When any Secret or ConfigMap changes, the hash changes
4. Kubernetes sees the pod template changed and performs a rolling restart

### Implementation

Located in `bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml`:

```bash
# Calculate hash of all Secrets and ConfigMaps
CONFIG_HASH=$(yq eval-all 'select(.kind == "Secret" or .kind == "ConfigMap")' "$TEMP_OUTPUT" | sha256sum | cut -d' ' -f1)
CONFIG_HASH="${CONFIG_HASH:0:16}"

# Inject annotation into workload pod templates
export CONFIG_HASH
yq eval-all '
  with(select(.kind == "Deployment" or .kind == "StatefulSet" or .kind == "DaemonSet");
    .spec.template.metadata.annotations["checksum/config"] = env(CONFIG_HASH)
  )
' "$TEMP_OUTPUT"
```

### Example Result

A Deployment rendered by ArgoCD will have the annotation automatically added:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
spec:
  template:
    metadata:
      annotations:
        checksum/config: "a1b2c3d4e5f67890"  # Automatically injected
    spec:
      containers:
      - name: app
        image: my-app:latest
```

## Scope

- **Per ArgoCD Application**: Each Application's hash is calculated independently from its own Secrets and ConfigMaps only
- **Isolated**: Changes in Application A do not affect Application B
- **Automatic**: No manual configuration required
- **Works for all manifests**: Applies to any Deployment/StatefulSet/DaemonSet rendered through ArgoCD, including:
  - Our own deployment templates
  - Helmfile deployments (e.g., docs)
  - Third-party Helm charts
  - Kustomize overlays

This is a key advantage over manual solutions - we don't need to modify third-party charts or helmfiles to get automatic restarts on config changes.

## Migration from Timestamp-Based Approach

Previously, the operations manager's deployment template (`manifests/deployment.yaml.jinja`) included a timestamp annotation to force pod restarts:

```yaml
annotations:
  # Timestamp to force pod restart when secrets are regenerated
  opi.rig.nl/generated-at: "{{ generated_at }}"
```

This approach had drawbacks:
- Pods restarted on every deployment, even when nothing changed
- Only worked for our own templates, not third-party charts
- Created unnecessary churn and downtime

The timestamp annotation has been removed. The CMP-based config hash now handles restarts more intelligently - pods only restart when actual configuration content changes.

## Current Limitations

### All-or-Nothing Hash

Currently, the hash includes ALL Secrets and ConfigMaps in the Application's rendered output. This means:

- If a Deployment only uses `secret-a`, but `secret-b` also exists in the Application, changing `secret-b` will still restart the Deployment
- This is a conservative approach that ensures no configuration change is missed

### Ideal Future State

A more precise implementation would:

1. Parse each Deployment to identify which Secrets and ConfigMaps it actually references:
   - `envFrom` with `secretRef` or `configMapRef`
   - `env` with `valueFrom.secretKeyRef` or `valueFrom.configMapKeyRef`
   - `volumes` with `secret` or `configMap` types
2. Calculate a per-Deployment hash based only on referenced resources
3. Inject a unique hash per Deployment

This would prevent unnecessary restarts when unrelated configuration changes.

### Why We Don't Do Per-Deployment Hashes Yet

Tracing resource references is complex:

1. **Volume mounts**: Need to parse `spec.volumes[].secret.secretName` and `spec.volumes[].configMap.name`
2. **Environment variables**: Need to parse `spec.containers[].env[].valueFrom.secretKeyRef` and similar
3. **envFrom**: Need to parse `spec.containers[].envFrom[].secretRef` and `spec.containers[].envFrom[].configMapRef`
4. **Init containers**: Same parsing for `spec.initContainers[]`
5. **Projected volumes**: Complex nested structure with multiple sources
6. **CSI volumes**: May reference secrets indirectly

The current approach trades precision for simplicity and reliability.

## Alternatives Considered

### Reloader (stakater/Reloader)

A Kubernetes controller that watches for ConfigMap/Secret changes and triggers rolling updates.

**Pros:**
- Per-resource tracking via annotations
- No modification of rendered manifests

**Cons:**
- Additional component to deploy and maintain
- Requires explicit annotations on Deployments
- Another moving part that can fail

### Helm sha256sum

Helm charts can include checksums in annotations:

```yaml
annotations:
  checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}
```

**Pros:**
- Per-resource precision
- Native Helm feature

**Cons:**
- Only works for Helm charts you control
- Doesn't work for kustomize or third-party charts

## Verification

To verify the feature is working:

1. Check that Deployments have the annotation:
   ```bash
   kubectl get deployment <name> -o jsonpath='{.spec.template.metadata.annotations.checksum/config}'
   ```

2. Update a Secret and verify the annotation changes after ArgoCD sync:
   ```bash
   # Before sync
   kubectl get deployment <name> -o jsonpath='{.spec.template.metadata.annotations.checksum/config}'

   # Update secret, sync ArgoCD app

   # After sync - should be different
   kubectl get deployment <name> -o jsonpath='{.spec.template.metadata.annotations.checksum/config}'
   ```

3. Verify pods were restarted:
   ```bash
   kubectl get pods -l app=<name> -o jsonpath='{.items[*].metadata.creationTimestamp}'
   ```
