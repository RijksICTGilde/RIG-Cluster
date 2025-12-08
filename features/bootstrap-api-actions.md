# Bootstrap API Actions

Bootstrap API Actions allow you to execute HTTP API calls automatically when a deployment is initialized. This is useful for setup tasks like creating initial data, configuring external systems, or triggering one-time initialization workflows.

## Overview

Bootstrap actions are executed once during deployment creation, after the ArgoCD application is deployed but before the project is fully registered. They support:

- Multiple authentication methods (Keycloak OAuth2, Basic Auth, Bearer tokens)
- Variable substitution from deployment context
- Response capture and chaining between actions
- Idempotent design with configurable expected status codes

## Configuration

Bootstrap actions can be configured using **templates** (recommended) or **inline** definitions.

### Using Templates (Recommended)

Templates are reusable bootstrap configurations stored in `operations-manager/python/opi/configs/bootstrap/`.

**In your project file:**

```yaml
deployments:
  - name: deployment-1
    cluster: odcn-production
    namespace: my-project
    repository: main-repo
    components:
      - reference: component-1
        image: my-app:latest

    # Reference bootstrap template
    bootstrap:
      template: "algoritmeregister-init"
      variables:
        org_code: "RIG"
        org_name: "Rijks ICT Gilde"
        bootstrap_username: "admin_user"
        bootstrap_password: "demo123"  # Can be AGE encrypted
```

### Inline Definition

For simple or one-off bootstrap actions:

```yaml
deployments:
  - name: deployment-1
    # ... other deployment config ...

    # Inline bootstrap definition
    bootstrap:
      - name: "my-bootstrap-action"
        base_url: "https://{{ PUBLIC_HOST }}"

        authentication:
          type: "keycloak-password"
          token_url: "{{ OIDC_URL }}/realms/{{ OIDC_REALM }}/protocol/openid-connect/token"
          client_id: "authentication-client"
          username: "admin"
          password: "secret123"

        actions:
          - name: "initialize-app"
            method: "POST"
            path: "/api/setup"
            body: |
              {
                "project": "{{ PROJECT_NAME }}"
              }
            expected_status: [200, 201]
```

## Available Context Variables

The following variables are automatically available for substitution:

- `PROJECT_NAME` - Name of the project
- `DEPLOYMENT_NAME` - Name of the deployment
- `CLUSTER` - Cluster name (e.g., "odcn-production")
- `PUBLIC_HOST` - Public URL for the deployment
- `INGRESS_POSTFIX` - Cluster ingress postfix
- `OIDC_URL` - Keycloak URL (if Keycloak service is used)
- `OIDC_REALM` - Keycloak realm name (if Keycloak service is used)

## Authentication Types

### Keycloak OAuth2 (Password Flow)

For public clients with username/password:

```yaml
authentication:
  type: "keycloak-password"
  token_url: "{{ OIDC_URL}}/realms/{{ OIDC_REALM }}/protocol/openid-connect/token"
  client_id: "authentication-client"
  username: "admin_user"
  password: "secret"  # Plain or AGE encrypted
```

### Keycloak OAuth2 (Client Credentials)

For confidential clients:

```yaml
authentication:
  type: "keycloak-client"
  token_url: "{{ OIDC_URL }}/realms/{{ OIDC_REALM }}/protocol/openid-connect/token"
  client_id: "service-client"
  client_secret: "secret"  # Plain or AGE encrypted
```

### Basic Authentication

```yaml
authentication:
  type: "basic"
  username: "admin"
  password: "secret"
```

### Bearer Token

```yaml
authentication:
  type: "bearer"
  token: "my-static-token"
```

### No Authentication

```yaml
authentication:
  type: "none"
```

## Response Capture and Chaining

Capture API responses and use them in subsequent actions:

```yaml
actions:
  - name: "create-user"
    as: "user_response"  # Capture response
    method: "POST"
    path: "/api/users"
    body: |
      {
        "email": "admin@example.com"
      }
    expected_status: [200, 201]

  - name: "assign-role"
    method: "PUT"
    path: "/api/users/{{ user_response.id }}/role"  # Use captured response
    body: |
      {
        "role": "admin"
      }
    expected_status: [200]
```

## Example: Algoritmeregister Bootstrap

The Algoritmeregister application requires creating an organization and making it visible. Here's how to configure it:

**Template file:** `configs/bootstrap/algoritmeregister-init.yaml`

```yaml
variables:
  org_code: "RIG"
  org_name: "Rijks ICT Gilde"
  org_type: "adviescollege"
  org_flow: "ictu_last"

bootstrap:
  - name: "algoritmeregister-organization-setup"
    base_url: "https://{{ PUBLIC_HOST }}"

    authentication:
      type: "keycloak-password"
      token_url: "{{ OIDC_URL }}/realms/{{ OIDC_REALM }}/protocol/openid-connect/token"
      client_id: "authentication-client"
      username: "{{ bootstrap_username }}"
      password: "{{ bootstrap_password }}"

    actions:
      - name: "create-organization"
        method: "POST"
        path: "/aanleverapi/organisation"
        headers:
          Content-Type: "application/json"
        body: |
          {
            "name": "{{ org_name }}",
            "code": "{{ org_code }}",
            "org_id": "{{ org_code }}",
            "type": "{{ org_type }}",
            "flow": "{{ org_flow }}"
          }
        expected_status: [200, 201, 409]  # 409 = already exists (idempotent)

      - name: "enable-organization-visibility"
        method: "PUT"
        path: "/organisation/{{ org_code }}/show_page/true"
        expected_status: [200, 204]
```

**Usage in project file:**

```yaml
deployments:
  - name: deployment-1
    # ... other config ...

    bootstrap:
      template: "algoritmeregister-init"
      variables:
        bootstrap_username: "ictu_user1"
        bootstrap_password: "demo123"
```

## Idempotency

Bootstrap actions support idempotent design through `expected_status` codes. For example, accepting HTTP 409 (Conflict) as a success status allows the action to succeed even if the resource already exists:

```yaml
actions:
  - name: "create-resource"
    method: "POST"
    path: "/api/resource"
    expected_status: [200, 201, 409]  # Accept both creation and already-exists
```

## Error Handling

By default, bootstrap action failures do not block deployment. Set `fail_on_error: true` to make an action failure stop the bootstrap process:

```yaml
actions:
  - name: "critical-setup"
    method: "POST"
    path: "/api/setup"
    fail_on_error: true  # Stop on failure
    expected_status: [200, 201]
```

## Best Practices

1. **Use templates** for reusable bootstrap configurations
2. **Encrypt sensitive data** using AGE encryption
3. **Design for idempotency** by accepting relevant HTTP status codes
4. **Chain actions** using response capture when data from one API call is needed in another
5. **Keep actions simple** - bootstrap is for initialization only, not complex workflows
6. **Test locally** before applying to production environments

## Troubleshooting

Bootstrap actions are logged during deployment. Check the Operations Manager logs for details:

```bash
kubectl logs -n rig-prd-operations deployment/operations-manager -f
```

Look for log entries containing "bootstrap" to see execution details and any errors.
