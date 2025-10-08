# Using Taskfile for RIG-Cluster

This project uses [Task](https://taskfile.dev/) as a task runner to simplify common operations. Task is a simple, cross-platform task runner and build tool that works as an alternative to more complex tools like Make.

## Prerequisites

1. Install Task:
   ```bash
   # MacOS
   brew install go-task

   # Using npm
   npm install -g @go-task/cli

   # Using asdf
   asdf plugin add task
   asdf install task latest
   asdf global task latest
   ```

2. Install required dependencies:
   ```bash
   # MacOS
   brew install kind kubectl kustomize sops pwgen age

   # Debian/Ubuntu
   sudo apt-get install kind kubectl kustomize sops pwgen age
   ```

3. Ensure all required tools are installed:
   ```bash
   task requirements-check
   ```

## Common Tasks

List all available tasks:
```bash
task
# or
task --list-all
```

### Setting Up a Local Environment

1. Create a local Kind cluster:
   ```bash
   task create-k8s-cluster
   ```

2. Set up local storage classes:
   ```bash
   task setup-local-storage
   ```

3. Bootstrap the local cluster with minimal configuration (namespace + ArgoCD):
   ```bash
   task bootstrap-minimal
   ```
   This creates the rig-system namespace and deploys ArgoCD, which will then deploy the rest of the infrastructure components.

4. All-in-one command to create a cluster and deploy components (alternative approach):
   ```bash
   task run-local
   ```

### Managing Secrets

1. Generate SOPS-encrypted secrets for a cluster:
   ```bash
   # For local cluster
   task generate-sops-secrets -- local

   # For a specific ODCN cluster
   task generate-sops-secrets -- odcn-1
   ```

   This will generate human-readable passwords using `pwgen` (14 characters in length) for all service accounts.
   The passwords are saved in a `.credentials-[cluster-name].txt` file in the encrypted secrets directory.

   > **Note:** Before using this task, you may want to configure SOPS by creating a `.sops.yaml` file in your project 
   > root with your encryption settings (AGE, PGP, etc.). See the [SOPS documentation](https://github.com/mozilla/sops) 
   > for details.

2. Apply generated secrets to the cluster:
   ```bash
   task apply-secrets -- local
   ```

   This task will automatically decrypt the SOPS-encrypted secrets and apply them to the cluster.

### Bootstrapping New Clusters

1. Generate a new age key for SOPS encryption:
   ```bash
   # Generate in current directory
   task generate-age-key
   
   # Generate in a specific directory
   task generate-age-key -- OUTPUT_DIR=/path/to/dir
   ```

2. Bootstrap a new cluster repository:
   ```bash
   # Create a new cluster repo with default name
   task bootstrap-new-cluster
   
   # Create a repo for a specific cluster
   task bootstrap-new-cluster -- my-production-cluster
   
   # Create without generating an age key
   task bootstrap-new-cluster -- GENERATE_AGE_KEY=false my-production-cluster
   
   # Specify output directory
   task bootstrap-new-cluster -- OUTPUT_DIR=/path/to/repos my-production-cluster
   ```
   
   This task:
   - Creates a new Git repository from the example template
   - Generates an age key for SOPS encryption
   - Creates encrypted secrets with human-readable passwords
   - Initializes the Git repository with an initial commit
   - Saves credentials separately for secure storage

### Local Development with ArgoCD

This project now includes a complete local development environment that uses ArgoCD with direct filesystem access, without requiring a Git repository or HTTP server.

#### Quick Setup (All-in-One)

For a complete setup in one command:
```bash
# Set up everything with a single command
task setup-local-development
```

This will:
1. Create a Kind cluster
2. Set up local storage classes
3. Apply the bootstrap configuration using local files
4. Mount your local repository directory to ArgoCD pods
5. Configure ArgoCD to read from the local filesystem

#### Individual Tasks

If you prefer to set things up step by step:

1. Create a local Kind cluster and set up storage:
   ```bash
   task create-k8s-cluster
   task setup-local-storage
   ```

2. Choose one of the following approaches:

   **GitOps Approach** (recommended):
   ```bash
   # Apply minimal bootstrap (ArgoCD)
   task bootstrap-minimal SOURCE_TYPE=local-filesystem
   
   # Mount your local repository to ArgoCD
   task mount-repo-to-argocd
   ```
   This creates the rig-system namespace and deploys ArgoCD configured to use local filesystem paths.
   
   **Direct Approach** (bypassing GitOps):
   ```bash
   # Apply full infrastructure directly
   task apply-full-infrastructure
   ```
   This applies all infrastructure components directly, bypassing ArgoCD.
   
3. For the GitOps approach, you can mount a specific repository directory:
   ```bash
   task mount-repo-to-argocd -- PROJECT_DIR=/path/to/your/repo
   ```
   This allows ArgoCD to directly access your local files.

#### How It Works

The local development setup works by:

1. Using direct filesystem references in the ArgoCD configuration instead of Git URLs
2. Mounting your local directory into the ArgoCD pods
3. Configuring ArgoCD to use local filesystem paths for repositories
4. Creating an ArgoCD Application that points to your local files

This approach offers several advantages:
- No need for a Git repository or HTTP server
- Changes to local files are immediately detected
- True GitOps workflow with ArgoCD, even in local development
- No file copying or synchronization needed

### Testing and Monitoring

1. Test kustomize build without applying:
   ```bash
   task test-kustomize-build
   ```

2. Check the health of deployed components:
   ```bash
   task check-health
   ```

### Cleanup

Uninstall the local Kind cluster:
```bash
task uninstall
```

## Extending Taskfile

To add new tasks, edit the `Taskfile.yaml` file. Tasks can be organized into namespaces or kept at the root level.

Example of adding a new task:
```yaml
tasks:
  new-task:
    desc: "Description of the new task"
    cmds:
      - echo "Running the new task"
      - your-command --with-args
```

## Environment Variables

The Taskfile supports `.env` files for environment variables. Create a `.env` file in the project root to set custom variables:

```
KIND_CLUSTER_NAME=custom-cluster-name
KIND_VERSION=1.28.0
CLUSTER_TYPE=odcn
```

## Advanced Usage

For more advanced usage of Task, refer to the [official documentation](https://taskfile.dev/).