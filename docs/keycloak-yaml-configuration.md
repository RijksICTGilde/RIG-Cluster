# Keycloak YAML Configuration

**Status:** Implemented ✅

This document describes the YAML-based Keycloak configuration system used in the Operations Manager.

## Overview

The Operations Manager provides a declarative YAML format for configuring Keycloak realms, clients, users, groups, and roles. This system combines:

1. **Custom orchestration logic** - Variables, loops, output chaining, and control flow
2. **Keycloak API conventions** - Standard Keycloak field names and structures

This hybrid approach allows you to:
- Define infrastructure as code
- Avoid repetitive configuration
- Maintain consistency across environments
- Use familiar Keycloak terminology

## Architecture

### YAML Handler (`opi/handlers/keycloak_yaml_handler.py`)

Processes YAML configurations and executes them via the Keycloak connector. The handler:
- Resolves variables using Jinja2-style `{{ variable }}` syntax
- Expands `forEach` loops to create multiple resources from templates
- Chains outputs between operations using the `as:` keyword
- Calls appropriate Keycloak connector methods for each section

### Processing Order

Sections are processed in this order to handle dependencies:

1. `realms:` - Create or configure realms
2. `identityProviders:` - Add SSO/OIDC identity providers
3. `authenticationFlows:` - Configure authentication flows
4. `clientScopes:` - Create custom client scopes
5. `platformClients:` - Create federation clients (for SSO)
6. `clients:` - Create OIDC clients
7. `realmRoles:` - Create realm-level roles
8. `groups:` - Create groups
9. `users:` - Create users with roles and group memberships

## Custom DSL Features

### Variables

Define reusable data at the top of your YAML file:

```yaml
variables:
  demo_groups:
    - name: "ICTU"
      path: "/ICTU"
    - name: "MIN_BZK"
      path: "/MIN_BZK"

  admin_roles:
    - "admin"
    - "all_groups"
```

Reference variables using `{{ variable_name }}`:

```yaml
realms:
  - name: "{{ realm_name }}"
    displayName: "{{ realm_display_name }}"
```

### forEach Loops

Create multiple resources from a list:

```yaml
groups:
  forEach: "{{ demo_groups }}"
  as: group
  group:
    name: "{{ group.name }}"
    path: "{{ group.path }}"
```

This creates one group for each item in `demo_groups`.

### Output Chaining

Capture results from one operation and use them in subsequent operations:

```yaml
platformClients:
  - as: platform_client  # Capture output
    clientId: "federation-client"
    realm: "{{ platform_realm_name }}"

identityProviders:
  - alias: "sso-provider"
    config:
      clientId: "{{ platform_client.client_id }}"      # Use captured output
      clientSecret: "{{ platform_client.client_secret }}"
```

The `as:` keyword stores the operation result, making it available to later sections.

## Keycloak API Conventions

Inside each configuration item, use standard Keycloak field names:

### Realm Configuration

```yaml
realms:
  - name: "my-realm"
    displayName: "My Application Realm"
    enabled: true
    registrationAllowed: true
    loginWithEmailAllowed: true
    resetPasswordAllowed: true
    verifyEmail: true
    bruteForceProtected: true
    loginTheme: "nl-design-system"
```

### Client Configuration

```yaml
clients:
  - clientId: "authentication-client"
    name: "My App - Authentication"
    enabled: true
    publicClient: true                    # Public client (no secret)
    protocol: "openid-connect"
    redirectUris:
      - "http://localhost:3000/*"
    webOrigins:
      - "+"
    standardFlowEnabled: true
    directAccessGrantsEnabled: true
    serviceAccountsEnabled: false
```

### Protocol Mappers

Protocol mappers use Keycloak's dotted-key convention:

```yaml
protocolMappers:
  - name: "app-realm-roles"
    protocol: "openid-connect"
    protocolMapper: "oidc-usermodel-realm-role-mapper"
    config:
      claim.name: "roles"                 # Dotted keys are Keycloak convention
      jsonType.label: "String"
      multivalued: "true"
      id.token.claim: "true"
      access.token.claim: "true"
      userinfo.token.claim: "true"
```

**Note:** The dots in keys like `claim.name` are part of the key name itself, not path separators. This is Keycloak's standard configuration format.

### User Configuration

```yaml
users:
  - username: "admin"
    enabled: true
    email: "admin@example.com"
    firstName: "Admin"
    lastName: "User"
    credentials:
      - type: "password"
        value: "secure-password"
        temporary: false
    realmRoles:                           # Assign realm roles
      - "admin"
      - "user"
    groups:                               # Add to groups
      - "Administrators"
    removeDefaultGroups: true             # Remove Keycloak's default group assignments
```

### Service Account Roles

For confidential clients with service accounts:

```yaml
clients:
  - clientId: "service-client"
    publicClient: false
    serviceAccountsEnabled: true
    authorizationServicesEnabled: true

    serviceAccountClientRoles:            # Assign roles to service account
      realm-management:
        - "manage-users"
        - "view-users"
        - "manage-clients"
```

### Client Roles and Access Restriction

Create client roles and restrict access to users with specific roles:

```yaml
clients:
  - clientId: "restricted-app"
    publicClient: true
    redirectUris:
      - "https://app.example.com/*"

    # Define client roles
    clientRoles:
      - name: "allowed-user"
        description: "Users allowed to access this application"

    # Restrict access to users with the role
    restrictAccess:
      enabled: true
      role: "allowed-user"
      errorMessage: "${accessDeniedNoPermission}"  # Theme message key in ${} format
```

This creates a custom authentication flow that:
1. Checks if the user has the specified client role
2. Denies access with a custom error message if they don't

**Note:** Users must be manually assigned the role after their first SSO login. See [Client Access Restriction](/features/client-access-restriction.md) for details.

## Configuration Examples

### Example 1: SSO-Only Realm

A realm that only allows SSO authentication (no local users):

```yaml
variables:
  platform_realm: "rig-platform"

realms:
  - name: "{{ project_realm_name }}"
    displayName: "{{ project_name }}"
    enabled: true
    registrationAllowed: false            # No local registration
    loginWithEmailAllowed: false          # No local login

platformClients:
  - as: platform_client
    realm: "{{ platform_realm }}"
    clientId: "{{ project_name }}-federation"
    redirectUris:
      - "{{ keycloak_url }}/realms/{{ project_realm_name }}/broker/sso/endpoint/*"

identityProviders:
  - alias: "sso-rijk"
    displayName: "SSO Rijk"
    providerId: "oidc"
    authenticateByDefault: true           # Auto-redirect to SSO
    config:
      clientId: "{{ platform_client.client_id }}"
      clientSecret: "{{ platform_client.client_secret }}"
      discoveryUrl: "{{ sso_discovery_url }}"
```

### Example 2: Application with Local Users and Roles

A realm for an application with local authentication, custom roles, and organization groups:

```yaml
variables:
  organizations:
    - name: "ICTU"
    - name: "MIN_BZK"

  admin_users:
    - username: "admin1"
      email: "admin1@example.com"
      groups: ["ICTU"]
    - username: "admin2"
      email: "admin2@example.com"
      groups: ["MIN_BZK"]

realms:
  - name: "{{ realm_name }}"
    enabled: true
    registrationAllowed: true
    loginWithEmailAllowed: true

realmRoles:
  - name: "admin"
    description: "Administrator role"
  - name: "editor"
    description: "Editor role"
  - name: "viewer"
    description: "Viewer role"

groups:
  forEach: "{{ organizations }}"
  as: org
  group:
    name: "{{ org.name }}"
    path: "/{{ org.name }}"

clients:
  - clientId: "web-app"
    publicClient: true
    redirectUris:
      - "http://localhost:3000/*"
    protocolMappers:
      - name: "roles-mapper"
        protocol: "openid-connect"
        protocolMapper: "oidc-usermodel-realm-role-mapper"
        config:
          claim.name: "roles"
          multivalued: "true"
          id.token.claim: "true"
          access.token.claim: "true"

users:
  forEach: "{{ admin_users }}"
  as: user
  user:
    username: "{{ user.username }}"
    email: "{{ user.email }}"
    credentials:
      - type: "password"
        value: "demo123"
        temporary: false
    realmRoles:
      - "admin"
    groups: "{{ user.groups }}"
    removeDefaultGroups: true
```

## Special Features

### Remove Default Groups

Keycloak automatically assigns users to default groups. To have clean group memberships:

```yaml
users:
  - username: "user1"
    removeDefaultGroups: true             # Remove all default group assignments
    groups:
      - "CustomGroup"                     # Only assign specified groups
```

### Group Membership without Slashes

For applications that need group names without path prefixes, use the group mapper:

```yaml
protocolMappers:
  - name: "app-groups"
    protocol: "openid-connect"
    protocolMapper: "oidc-group-membership-mapper"
    config:
      claim.name: "groups"
      full.path: "false"                  # Groups appear as "ICTU" not "/ICTU"
      id.token.claim: "true"
      access.token.claim: "true"
```

## Configuration File Locations

YAML configuration files are stored in:
```
operations-manager/python/opi/configs/keycloak/
├── bootstrap.yaml           # RIG Platform realm setup
├── sso-only.yaml           # Project realm with SSO-only auth
├── sso-support.yaml        # Project realm with SSO + local auth
└── algoritmeregister.yaml  # Application-specific realm
```

## Usage in Python Code

### Execute a Configuration

```python
from pathlib import Path
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler
from opi.connectors.keycloak import create_keycloak_connector

# Create connector
keycloak = await create_keycloak_connector()

# Build context with variables
context = {
    "realm_name": "my-realm",
    "realm_display_name": "My Application",
    "keycloak_url": "https://keycloak.example.com",
}

# Execute configuration
yaml_path = Path("opi/configs/keycloak/my-config.yaml")
handler = KeycloakYamlHandler(keycloak)
await handler.execute_config(yaml_path, context)
```

### Context Variables

Context variables are provided by the calling code and can include:
- `realm_name` - Target realm name
- `realm_display_name` - Human-readable realm name
- `keycloak_url` - Keycloak server URL
- `project_name` - Project identifier
- `cluster` - Cluster name (production, development, local)
- Any application-specific variables

## Implementation Details

### Connector Methods (`opi/connectors/keycloak.py`)

The Keycloak connector provides methods for all operations:

**Realm Operations:**
- `create_realm()` - Create/configure realms
- `realm_exists()` - Check if realm exists

**Client Operations:**
- `create_oidc_client()` - Create generic OIDC clients
- `create_deployment_client()` - Create deployment-specific clients
- `create_federation_client()` - Create federation clients for SSO
- `find_client_by_client_id()` - Find clients by ID

**Identity Provider Operations:**
- `add_identity_provider()` - Add OIDC/SAML identity providers
- `create_identity_provider_mapper()` - Add attribute mappers
- `ensure_standard_oidc_mappers()` - Add standard OIDC mappers

**Client Scope Operations:**
- `create_custom_client_scope()` - Create client scopes with mappers
- `assign_client_scope_as_realm_default()` - Set as realm default

**Authentication Flow Operations:**
- `configure_sso_redirect_flow()` - Configure SSO-only flows

**User & Group Operations:**
- `create_user()` - Create users
- `assign_realm_roles_to_user()` - Assign roles to users
- `join_user_to_group()` - Add users to groups
- `leave_all_groups()` - Remove user from all groups

**Role & Group Operations:**
- `create_realm_role()` - Create realm roles
- `create_group()` - Create groups

### Handler Processing (`opi/handlers/keycloak_yaml_handler.py`)

The handler processes each section:

1. **Variable Substitution** - `_substitute_variables()` replaces `{{ var }}` placeholders
2. **forEach Expansion** - `_expand_foreach()` expands loops into multiple items
3. **Output Storage** - Captures operation results using `as:` keyword
4. **Section Processing** - Calls appropriate `_process_*()` methods

Each section processor:
- Extracts configuration from YAML
- Substitutes variables
- Expands loops
- Calls connector methods
- Handles idempotency (409 Conflict errors)

## TODO: Future Improvements

- [ ] **Create comprehensive JSON schemas** for all configuration sections to enable validation
- [ ] **Add schema validation** to catch configuration errors early (before API calls)
- [ ] **Document all available Keycloak field names** and their meanings with examples
- [ ] **Add examples for more complex scenarios** (composite roles, fine-grained permissions, client roles)
- [ ] **Create testing utilities** for validating YAML configurations without applying them
- [ ] **Add support for updating/patching existing configurations** (not just creation/idempotent replace)
- [ ] **Document all available protocol mapper types** and their configurations with real-world use cases
- [ ] **Create configuration templates** for common use cases (SaaS app, internal tool, etc.)
- [ ] **Add conditional sections** (e.g., `if: "{{ environment == 'production' }}"`)
- [ ] **Support configuration composition** (import/extend other YAML files)
- [ ] **Add dry-run mode** to preview changes without applying them
- [ ] **Create migration tools** for converting existing Python bootstrap code to YAML
- [ ] **Add rollback capability** for failed deployments
- [ ] **Document error handling** and troubleshooting procedures

## Related Documentation

- [Keycloak Admin REST API](https://www.keycloak.org/docs-api/latest/rest-api/index.html)
- [python-keycloak Documentation](https://python-keycloak.readthedocs.io/)
- [Algoritmeregister Keycloak Setup](/keycloak-migration/KEYCLOAK_SETUP.md)

---

**Last Updated**: 2025-01-07
**Status**: Implemented and In Use
