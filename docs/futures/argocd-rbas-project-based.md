# ArgoCD RBAC Project Configuration

## Overview

ArgoCD supports Role-Based Access Control (RBAC) through two primary mechanisms:
- **Global RBAC**: Configured via `argocd-rbac-cm` ConfigMap for instance-wide policies
- **Project-Level RBAC**: Configured directly in AppProject specifications for project-scoped access

## Project-Level RBAC Configuration

### Basic AppProject with RBAC

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: my-project
  namespace: argocd
spec:
  description: "Example project with RBAC"
  
  # Define allowed destinations
  destinations:
  - namespace: my-app-namespace
    server: https://kubernetes.default.svc
  
  # Define allowed source repositories
  sourceRepos:
  - https://github.com/myorg/my-app-repo.git
  
  # Project-specific roles and policies
  roles:
  - name: developer
    description: "Developer access to project applications"
    policies:
    - p, proj:my-project:developer, applications, get, my-project/*, allow
    - p, proj:my-project:developer, applications, sync, my-project/*, allow
    - p, proj:my-project:developer, applications, create, my-project/*, allow
    - p, proj:my-project:developer, applications, update, my-project/*, allow
    groups:
    - my-oidc-developer-group
    
  - name: viewer
    description: "Read-only access to project applications"
    policies:
    - p, proj:my-project:viewer, applications, get, my-project/*, allow
    groups:
    - my-oidc-viewer-group
    jwtTokens:
    - iat: 1234567890  # Token metadata for lifecycle management
```

## Key Concepts

### Policy Format
```
p, proj:PROJECT_NAME:ROLE_NAME, RESOURCE, ACTION, OBJECT, EFFECT
```

- **PROJECT_NAME**: Name of the AppProject
- **ROLE_NAME**: Role defined in the project
- **RESOURCE**: ArgoCD resource type (applications, repositories, etc.)
- **ACTION**: Operation (get, create, update, delete, sync, etc.)
- **OBJECT**: Target object pattern (project/app-name or wildcards)
- **EFFECT**: allow or deny

### Available Resources
- `applications` - Application management
- `repositories` - Repository access
- `clusters` - Cluster management
- `logs` - Application logs
- `exec` - Pod execution access

## JWT Token Management

### Token Creation
```bash
# Create a token for a project role
argocd proj role create-token my-project developer -e 24h

# List project roles
argocd proj role list my-project

# Revoke tokens by updating the AppProject spec
```

### Token Security
- Actual JWT tokens are generated via CLI and stored securely
- AppProject spec only contains token metadata (`iat` timestamps)
- Tokens inherit all policies from their associated role

## Integration with SSO

Project roles integrate seamlessly with OIDC/SAML providers:
- Map SSO groups to project roles via the `groups` field
- Users automatically inherit project permissions based on group membership
- No need to update main ArgoCD deployment for project-specific access

## Benefits

- **Isolation**: Project-scoped access without affecting other projects
- **Independent Management**: Update project RBAC without touching main ArgoCD deployment
- **Dynamic Policies**: Role policy changes take immediate effect
- **GitOps Friendly**: Project RBAC can be managed through Git repositories

## Individual Access Limitations in AppProject
AppProject Structure: AppProject roles support a groups field that can include values from the email scope or groups scope, but the examples consistently show group-based assignments rather than individual user policies.

```yaml
policy.csv: |
  p, role:project-developer, applications, *, my-project/*, allow
  g, user@example.org, role:project-developer
```
