# Keycloak YAML Templates

The operations manager uses declarative YAML templates to configure Keycloak realms. These templates define the desired state of a realm, and the system ensures that state is achieved idempotently.

## Overview

- **Location**: `operations-manager/python/opi/configs/keycloak/`
- **Format**: YAML with Jinja2 variable substitution
- **Processing**: Idempotent - safe to run multiple times
- **Templates Available**:
  - `sso-only.yaml` - SSO-only authentication (auto-redirect to identity provider)
  - `sso-support.yaml` - SSO + local account authentication (login form with SSO button)

## Idempotency

All operations are idempotent - running them multiple times produces the same result without errors:

| Operation | Behavior |
|-----------|----------|
| Create realm | Creates if not exists, uses existing if found |
| Create client | Creates if not exists, skips if found (409 handled) |
| Create identity provider | Creates if not exists, skips if found (409 handled) |
| Create realm role | Creates if not exists, skips if found (409 handled) |
| Create group | Creates if not exists, skips if found (409 handled) |
| Create user | Creates if not exists, returns existing if found |
| Assign roles to user | Keycloak API is naturally idempotent |
| Add user to group | Keycloak API is naturally idempotent |

## Template Sections

### Variables

Define template variables (can be overridden by context):

```yaml
variables:
  default_theme: "nl-design-system"
  enable_registration: false
```

### Realms

Create Keycloak realms:

```yaml
realms:
  - name: "{{ project_realm_name }}"
    displayName: "{{ project_display_name }}"
    enabled: true
    registrationAllowed: false
    loginWithEmailAllowed: false
    bruteForceProtected: true
    loginTheme: "nl-design-system"
```

### Platform Clients

Create federation clients in the platform realm (for IdP federation):

```yaml
platformClients:
  - as: platform_client  # Capture output for use in identityProviders
    realm: "{{ platform_realm_name }}"
    clientId: "{{ platform_client_id }}"
    name: "{{ project_name }} - Federation"
    enabled: true
    publicClient: false
    redirectUris:
      - "{{ keycloak_url }}/realms/{{ project_realm_name }}/broker/rig-platform-oidc/endpoint/*"
```

The `as` keyword captures the created client's credentials for use in subsequent sections:
- `{{ platform_client.client_id }}` - The client ID
- `{{ platform_client.client_secret }}` - The generated client secret

### Identity Providers

Configure OIDC identity providers:

```yaml
identityProviders:
  - alias: "rig-platform-oidc"
    displayName: "SSO Rijk"
    providerId: "oidc"
    enabled: true
    authenticateByDefault: true  # Auto-redirect (sso-only) or false (show login form)
    config:
      clientId: "{{ platform_client.client_id }}"
      clientSecret: "{{ platform_client.client_secret }}"
      discoveryUrl: "{{ keycloak_url }}/realms/{{ platform_realm_name }}/.well-known/openid-configuration"
      syncMode: "INHERIT"
    mappers:
      - name: "email-mapper"
        identityProviderMapper: "oidc-user-attribute-idp-mapper"
        config:
          syncMode: "INHERIT"
          claim: "email"
          user.attribute: "email"
```

### Authentication Flows

Configure custom authentication flows:

```yaml
authenticationFlows:
  - alias: "External IDP Redirector"
    description: "Auto-redirect to identity provider"
    providerId: "basic-flow"
    topLevel: true
    builtIn: false
    executions:
      - authenticator: "auth-cookie"
        requirement: "ALTERNATIVE"
        priority: 10
      - authenticator: "identity-provider-redirector"
        requirement: "ALTERNATIVE"
        priority: 20
        authenticatorConfig:
          alias: "platform-redirector"
          config:
            defaultProvider: "rig-platform-oidc"
    setAsBrowserFlow: true  # Set as realm's browser flow
```

### Clients

Create OAuth/OIDC clients:

```yaml
clients:
  - clientId: "{{ invite_client_id }}"
    name: "Operations Manager Invite Flow"
    enabled: true
    publicClient: true
    protocol: "openid-connect"
    redirectUris:
      - "{{ operations_manager_domain }}/invite/*"
    webOrigins:
      - "{{ operations_manager_domain }}"
    standardFlowEnabled: true
    implicitFlowEnabled: false
    directAccessGrantsEnabled: false
    serviceAccountsEnabled: false
    pkceCodeChallengeMethod: "S256"  # Enable PKCE
```

#### Client Options

| Property | Type | Description |
|----------|------|-------------|
| `clientId` | string | Client identifier |
| `name` | string | Display name |
| `enabled` | boolean | Whether client is enabled |
| `publicClient` | boolean | Public (no secret) or confidential client |
| `protocol` | string | Protocol (`openid-connect`) |
| `redirectUris` | list | Allowed redirect URIs (supports wildcards at end) |
| `webOrigins` | list | Allowed web origins for CORS |
| `postLogoutRedirectUris` | list | Allowed post-logout redirect URIs |
| `standardFlowEnabled` | boolean | Enable authorization code flow |
| `implicitFlowEnabled` | boolean | Enable implicit flow (not recommended) |
| `directAccessGrantsEnabled` | boolean | Enable resource owner password credentials |
| `serviceAccountsEnabled` | boolean | Enable service account (client credentials) |
| `pkceCodeChallengeMethod` | string | PKCE method (`S256` or `plain`) |
| `clientRoles` | list | Client roles to create |
| `restrictAccess` | object | Access restriction configuration |

### Client Scopes

Create custom client scopes:

```yaml
clientScopes:
  - name: "custom_attributes_passthrough"
    description: "Custom attributes passthrough"
    realmType: "PROJECT"  # or "PLATFORM"
    protocol: "openid-connect"
    attributes:
      include.in.token.scope: "true"
      display.on.consent.screen: "false"
    protocolMappers:
      - name: "Organization Name"
        protocol: "openid-connect"
        protocolMapper: "oidc-usermodel-attribute-mapper"
        config:
          user.attribute: "organization.name"
          claim.name: "organization.name"
          jsonType.label: "String"
          id.token.claim: "true"
          access.token.claim: "true"
    defaultScope: true  # Add as realm-level default
```

### Realm Roles

Create realm-level roles:

```yaml
realmRoles:
  - name: "developer"
    description: "Developer role with access to development resources"
  - name: "admin"
    description: "Administrator role with full access"
```

### Groups

Create groups:

```yaml
groups:
  - name: "developers"
    path: "/developers"
  - name: "admins"
    path: "/admins"
```

### Users

Create users with optional role and group assignments:

```yaml
users:
  - username: "test-user"
    email: "test@example.com"
    firstName: "Test"
    lastName: "User"
    enabled: true
    credentials:
      - type: "password"
        value: "{{ test_user_password }}"
    realmRoles:
      - "developer"
    groups:
      - "developers"
    removeDefaultRoles: false  # Set true to remove default role assignments first
```

#### User Options

| Property | Type | Description |
|----------|------|-------------|
| `username` | string | Username (required) |
| `email` | string | Email address |
| `firstName` | string | First name |
| `lastName` | string | Last name |
| `enabled` | boolean | Whether user is enabled (default: true) |
| `credentials` | list | List of credentials (password) |
| `realmRoles` | list | Realm roles to assign |
| `groups` | list | Groups to add user to |
| `removeDefaultRoles` | boolean | Remove default roles before assigning (default: false) |

## Variable Substitution

Variables use Jinja2-style syntax: `{{ variable_name }}`

### Context Variables

These variables are automatically provided:

| Variable | Description |
|----------|-------------|
| `project_name` | Project name |
| `cluster` | Cluster name |
| `keycloak_url` | Keycloak base URL |
| `platform_realm_name` | Platform realm name (e.g., `rig-platform`) |
| `project_realm_name` | Generated project realm name |
| `project_display_name` | Display name for the realm |
| `operations_manager_domain` | Operations manager domain (hostname) |
| `invite_client_id` | Client ID for invite flow |
| `platform_client_id` | Platform client ID for federation |

### Captured Outputs

Use `as: name` to capture operation outputs:

```yaml
platformClients:
  - as: platform_client  # Capture this
    clientId: "my-client"
    ...

identityProviders:
  - config:
      clientId: "{{ platform_client.client_id }}"      # Use captured value
      clientSecret: "{{ platform_client.client_secret }}"
```

### Nested Access

Access nested values with dot notation:

```yaml
config:
  clientId: "{{ platform_client.client_id }}"
  url: "{{ keycloak_url }}/realms/{{ realm_name }}"
```

## ForEach Loops

Create multiple resources from a list:

```yaml
users:
  forEach: "{{ users_to_create }}"
  as: user
  user:
    username: "{{ user.username }}"
    email: "{{ user.email }}"
    realmRoles: "{{ user.roles }}"
```

## Processing Order

Sections are processed in dependency order:

1. `realms` - Create realm first
2. `platformClients` - Create federation clients (captures outputs)
3. `identityProviders` - Uses platform client credentials
4. `authenticationFlows` - Configure authentication
5. `clientScopes` - Create custom scopes
6. `clients` - Create application clients
7. `realmRoles` - Create roles
8. `groups` - Create groups
9. `users` - Create users (may reference roles/groups)

## When Templates Are Applied

Templates are executed:

1. **Initial realm creation** - Full `execute_config()` runs all sections
2. **Project refresh** - Selective ensures:
   - `ensure_authentication_flows()` - Updates flows if needed
   - `ensure_clients()` - Creates missing clients
   - IdP/platform client URL updates (for http/https fixes)

## Creating Custom Templates

1. Copy an existing template as a starting point
2. Modify sections as needed
3. Reference the template in your project YAML:

```yaml
services:
  - keycloak:
      config:
        template: "my-custom-template"
        variables:
          custom_var: "value"
```

## Troubleshooting

### Template Not Found

```
FileNotFoundError: Keycloak template 'xyz' not found
```

Verify the template file exists at `configs/keycloak/{template}.yaml`

### Variable Not Found

```
KeyError: Variable path not found: 'my_variable'
```

Ensure the variable is either:
- Defined in the `variables` section of the template
- Provided in the context (project-level config)
- Captured with `as:` from a previous operation

### Client Already Exists

This is normal - the handler logs this as info and continues:
```
Client 'my-client' already exists in realm 'my-realm'
```
