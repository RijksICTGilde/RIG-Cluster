# Manifest Extension Pipeline

## What it is

A per-cluster configurable pipeline that mutates generated Kubernetes manifests before they are committed to git. Extensions are defined as YAML files and referenced in the cluster configuration. Each extension type implements specific mutation logic.

The first extension is `registry-rewrite`, which rewrites container image registry URLs and adds the corresponding `imagePullSecrets`. This solves the ODCN requirement where direct access to public registries (e.g. `ghcr.io`) is blocked and images must be pulled through a private mirror (`rcr.rijksapps.nl`).

## How it works

```
Project File (YAML)
       |
  Manifest Generation (Jinja2 templates)
       |
  Files on disk (deployment.yaml, service.yaml, etc.)
       |
  Extension Pipeline  <-- runs here
       |
  Kustomization / SOPS encryption / Git commit
```

After `create_application_manifests()` writes all component manifests to disk, the pipeline:

1. Loads extension definitions from YAML files based on cluster config
2. Reads each manifest file (skipping `.sops.yaml`, `.to-sops.yaml`, `kustomization.yaml`)
3. Parses the YAML, runs each extension's `process()` method
4. Writes the modified manifest back to disk

The pipeline runs before `collect_manifest_files()` and kustomization generation, so it does not affect file structure -- only content.

## Configuration

### Extension YAML definition

Extension definitions live in `operations-manager/python/extensions/`. Each file is self-describing:

```yaml
# extensions/odcn-registry-rewrite.yaml
type: registry-rewrite
config:
  mappings:
    - from: ghcr.io
      to: rcr.rijksapps.nl/ghcr-rig
      imagePullSecret: ghcr-rig-robot-pull-secret
    - from: docker.io
      to: rcr.rijksapps.nl/dockerhub-rig
      imagePullSecret: dockerhub-rig-robot-pull-secret
```

| Field | Description |
|-------|-------------|
| `type` | Which extension class to use (e.g. `registry-rewrite`) |
| `config` | Extension-specific configuration passed to the class |

### Cluster config reference

In `opi/core/cluster_config.py`, each cluster can reference extensions by filename (without `.yaml`):

```python
"odcn-production": {
    # ... existing config ...
    "extensions": ["odcn-registry-rewrite"],
}
```

Clusters without `extensions` (e.g. `local`, `sandboxed-local`) are unaffected.

## Registry Rewrite Extension

The `registry-rewrite` extension processes any manifest with a pod spec (Deployment, StatefulSet, DaemonSet, Job, CronJob):

1. Scans `containers[]` and `initContainers[]` for image URLs
2. Rewrites the registry prefix based on configured mappings
3. Adds the corresponding `imagePullSecret` to the pod spec (deduplicates with existing secrets)

### Example

Input manifest:
```yaml
spec:
  template:
    spec:
      containers:
        - name: app
          image: ghcr.io/minbzk/my-app:latest
```

Output (with ODCN extension):
```yaml
spec:
  template:
    spec:
      imagePullSecrets:
        - name: ghcr-rig-robot-pull-secret
      containers:
        - name: app
          image: rcr.rijksapps.nl/ghcr-rig/minbzk/my-app:latest
```

## Adding a new extension type

1. Create a class in `opi/extensions/` that extends `ManifestExtension`
2. Implement `process(manifest: dict) -> dict`
3. Register it in `EXTENSION_TYPES` in `opi/extensions/pipeline.py`
4. Create a YAML definition in `extensions/`
5. Reference it in the cluster config

## Key files

| File | Purpose |
|------|---------|
| `opi/extensions/base.py` | Abstract `ManifestExtension` base class |
| `opi/extensions/registry_rewrite.py` | Registry rewrite extension |
| `opi/extensions/pipeline.py` | Pipeline runner, YAML loading, extension registry |
| `extensions/odcn-registry-rewrite.yaml` | ODCN registry mapping config |
| `opi/core/cluster_config.py` | `get_extensions()` helper, per-cluster config |
| `opi/manager/project_manager.py` | Pipeline hook in `_process_deployment_manifests()` |
