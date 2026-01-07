# Client Access Restriction

Restrict access to Keycloak clients based on client roles. This feature allows you to control which users can access an application, even when using SSO (like SSO Rijk) for authentication.

## What It Is

This feature separates **authentication** (verifying identity via SSO) from **authorization** (granting access to specific applications). Users authenticate through SSO Rijk, but only users with a specific client role can access the application. Users without the role see a customizable error message.

## How It Works

When enabled, the system:

1. Creates a **client role** on your application's Keycloak client
2. Creates a **restricted browser flow** that checks for the role (for direct logins)
3. Sets the browser flow as an **authentication override** on your client
4. Creates a **post-broker login flow** that checks for the role (for SSO logins)
5. Sets the post-broker login flow on all **identity providers** in the realm
6. Users without the role are denied access with a custom error message

### Browser Flow (for direct username/password logins)

```
Browser Flow (restricted)
├── Cookie [ALTERNATIVE]
├── Identity Provider Redirector [ALTERNATIVE]
└── Forms [ALTERNATIVE]
    ├── Username Password Form [REQUIRED]
    └── Deny If No Access [CONDITIONAL]
        ├── Condition - User Role [REQUIRED] (negated)
        └── Deny Access [REQUIRED]
```

### Post-Broker Login Flow (for SSO logins)

This flow runs after every successful SSO authentication and ensures SSO users are also checked for the required role:

```
Post-Broker Login Flow (restricted)
└── Deny If No Access [CONDITIONAL]
    ├── Condition - User Role [REQUIRED] (negated)
    └── Deny Access [REQUIRED]
```

This dual-flow approach ensures that both direct logins and SSO logins are subject to the same role-based access restriction.

## How to Use

### Configuration in Project YAML

Add `clientRoles` and `restrictAccess` to your client configuration:

```yaml
clients:
  - clientId: "my-application"
    publicClient: true
    redirectUris:
      - "https://my-app.example.com/*"

    # Define client roles
    clientRoles:
      - name: "allowed-user"
        description: "Users allowed to access this application"

    # Enable access restriction
    restrictAccess:
      enabled: true
      role: "allowed-user"
      errorMessage: "accessDeniedNoPermission"
```

### Configuration Options

#### clientRoles

A list of client roles to create on the client.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Name of the role |
| `description` | string | No | Description of the role |

#### restrictAccess

Configuration for access restriction.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | boolean | Yes | - | Enable/disable access restriction |
| `role` | string | Yes (if enabled) | - | Client role name that grants access |
| `errorMessage` | string | No | `accessDeniedNoPermission` | Theme message key for denial message |

### Granting Access to Users

After a user authenticates via SSO for the first time, they will be created in the project realm but will not have access. To grant access:

1. **Via Keycloak Admin Console:**
   - Go to your project realm
   - Navigate to Users
   - Find the user
   - Go to Role Mappings tab
   - Select your client from the dropdown
   - Add the `allowed-user` role

2. **Via Keycloak API:**
   ```bash
   # Get user ID
   USER_ID=$(curl -s -H "Authorization: Bearer $TOKEN" \
     "$KEYCLOAK_URL/admin/realms/$REALM/users?email=user@example.com" | jq -r '.[0].id')

   # Get client ID
   CLIENT_UUID=$(curl -s -H "Authorization: Bearer $TOKEN" \
     "$KEYCLOAK_URL/admin/realms/$REALM/clients?clientId=my-application" | jq -r '.[0].id')

   # Get role
   ROLE=$(curl -s -H "Authorization: Bearer $TOKEN" \
     "$KEYCLOAK_URL/admin/realms/$REALM/clients/$CLIENT_UUID/roles/allowed-user")

   # Assign role
   curl -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d "[$ROLE]" \
     "$KEYCLOAK_URL/admin/realms/$REALM/users/$USER_ID/role-mappings/clients/$CLIENT_UUID"
   ```

## Custom Error Messages

The error message shown to denied users is controlled by the `errorMessage` field, which references a key in the Keycloak theme's message properties.

### Default Message Key

The default key is `accessDeniedNoPermission`. To use a custom message, you need to add it to the nl-design-system theme.

### Adding Custom Messages to Theme

In the [keycloak-theme](https://github.com/MinBZK/keycloak-theme) repository:

1. Add to `src/main/resources/theme/nl-design-system/login/messages/messages_en.properties`:
   ```properties
   accessDeniedNoPermission=You do not have permission to access this application. Please contact the administrator to request access.
   ```

2. Add to `src/main/resources/theme/nl-design-system/login/messages/messages_nl.properties`:
   ```properties
   accessDeniedNoPermission=Je hebt geen toegang tot deze applicatie. Neem contact op met de beheerder om toegang aan te vragen.
   ```

3. Build and release a new version of the theme

4. Update the theme version in `infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml`

## Complete Example

Here's a complete project configuration example:

```yaml
apiVersion: rig.odc-noord.nl/v1alpha1
kind: Project
metadata:
  name: my-secure-app
spec:
  services:
    - publish-on-web
    - keycloak:
        config:
          template: sso-support
    - postgresql-database

  helm-charts:
    - name: my-app
      repo-url: https://charts.example.com
      chart: my-application
      version: 1.0.0
      namespace: rig-my-secure-app
      uses-services:
        - publish-on-web
        - keycloak
        - postgresql-database

      keycloak-config:
        clients:
          - clientId: "my-secure-app-client"
            publicClient: true
            redirectUris:
              - "https://my-app.kind/*"
            webOrigins:
              - "+"

            clientRoles:
              - name: "app-user"
                description: "Users allowed to access the application"

            restrictAccess:
              enabled: true
              role: "app-user"
              errorMessage: "accessDeniedNoPermission"
```

## Troubleshooting

### User sees "Invalid username or password" instead of access denied message

This can happen if:
- The authentication flow was not properly configured
- The conditional sub-flow is not set to CONDITIONAL requirement

Check the authentication flow in Keycloak Admin Console under Authentication > Flows.

### Role is not being checked (direct login)

Ensure:
- The client role exists on the correct client
- The `restrictAccess.role` matches the client role name exactly
- The flow override is set on the client (check Client > Advanced > Authentication Flow Overrides)

### Role is not being checked (SSO login)

If users can bypass the role check when using SSO but not when using direct login:
- Verify the post-broker login flow exists (check Authentication > Flows for `post-broker-restricted-<client-id>`)
- Verify the identity provider has the post-broker login flow set (check Identity Providers > your IdP > Post Broker Login Flow)
- Ensure there are identity providers configured in the realm before enabling `restrictAccess`

### Custom error message not showing

- Verify the message key exists in the theme
- Check that the theme is properly deployed to Keycloak
- The error message format should be `${messageKey}` in the Deny Access authenticator config

## Dependencies

- Keycloak 21.0.0+ (for fixed Deny Access authenticator)
- nl-design-system theme with custom error messages

## Related Documentation

- [Keycloak YAML Configuration](../docs/keycloak-yaml-configuration.md)
- [Keycloak Server Admin - Authentication Flows](https://www.keycloak.org/docs/latest/server_admin/#_authentication-flows)
