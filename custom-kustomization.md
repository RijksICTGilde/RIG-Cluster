# Working with Kustomize and Local Secrets

This guide explains how to work with kustomize and manage secrets for local development in the RIG-Cluster project.

## Using Kustomize

The Taskfile includes several tasks that support working with kustomization directories:

### Testing Kustomization Builds

```bash
# Test with the default directory
task test-kustomize-build

# Test with a custom directory
task test-kustomize-build -- KUSTOMIZATION_DIR=path/to/different/dir
```

### Applying Kustomization to Clusters

```bash
# Apply with the default directory
task apply-local-bootstrap

# Apply with a custom directory
task apply-local-bootstrap -- KUSTOMIZATION_DIR=path/to/different/dir
```

## Technical Details

All kustomize-related tasks point directly to the directory containing a standard `kustomization.yaml` file:

- These tasks use the proper kustomize approach of targeting a directory, not a file
- They use the `--load-restrictor LoadRestrictionsNone` flag to allow resources from outside the directory
- This is the standard way to use kustomize and works for all cases in this project

## Example Use Cases

1. **Different cluster types**:
   ```bash
   # Apply to the local kind cluster (default)
   task apply-local-bootstrap
   
   # Apply to a different cluster configuration
   task apply-local-bootstrap -- KUSTOMIZATION_DIR=cluster-specific-repo/clusters/odcn
   ```

2. **Testing before applying**:
   ```bash
   # Test the build output without applying
   task test-kustomize-build
   
   # Review the output in test-output.yaml
   # Then apply when ready
   task apply-local-bootstrap
   ```

## Managing Secrets for Local Development

For local development, secrets are handled in a simplified way without the need for SOPS encryption. The RIG-Cluster project directly uses the template secrets for local development.

### Direct Use of Secret Templates

The `cluster-specific-repo/clusters/local-kind-cluster/kustomization.yaml` file directly references the secret templates:

```yaml
resources:
  # Direct local path reference for local development
  - ../../../implementation/bootstrap/clusters/local

  # Local overrides in resources directory
  - resources/postgres-override.yaml
  - resources/vault-override.yaml
  
  # Local development secrets directly from templates
  - ../../../implementation/bootstrap/infrastructure/secrets/templates
```

This approach:
1. Uses the template secrets directly without encryption
2. Simplifies the development workflow
3. Requires no additional task for secret generation
4. Ensures consistency with the production templates

### Default Credentials

The template secrets use default credentials (usually "changeMe123!") for local development. If you need to change these credentials:

1. Edit the template files directly for your local environment
2. Be careful not to commit these changes to version control

### Integration with Setup Process

The `setup-local-development` task handles the whole setup process:

```bash
task setup-local-development
```

Since the kustomization directly references the template secrets, no additional secret generation step is needed.