# Client Access Restriction

Restrict access to Keycloak clients based on client roles. This feature allows you to control which users can access an application, even when using SSO (like SSO Rijk) for authentication.

## What It Is

This feature separates **authentication** (verifying identity via SSO) from **authorization** (granting access to specific applications). Users authenticate through SSO Rijk, but only users with a specific client role can access the application. Users without the role see a customizable error message.

## How It Works

When enabled, the system:

1. Creates a **client role** (or realm role) on your application's Keycloak client
2. Creates a **role gate browser flow** that checks for the role on every login
3. Sets that flow as an **authentication override** on your client, and on the `<client>-public` client of the same deployment
4. Removes the flow's predecessor, `browser-restricted-<client-id>` (see below)
5. Creates a **post-broker login flow** that checks for the role (for SSO logins)
6. Sets the post-broker login flow on all **identity providers** in the realm
7. Users without the role are denied access with a custom error message

### Browser Flow (`browser-role-gate-<client-id>`)

```
Role gate browser flow
├── Authenticate [REQUIRED]
│   ├── Cookie [ALTERNATIVE]
│   ├── Identity Provider Redirector [ALTERNATIVE]
│   └── Forms [ALTERNATIVE]
│       ├── Username Password Form [REQUIRED]
│       └── Browser - Conditional OTP [CONDITIONAL]
│           ├── Condition - user configured [REQUIRED]
│           └── OTP Form [REQUIRED]
└── Deny If No Access [CONDITIONAL]
    ├── Condition - User Role [REQUIRED] (negated)
    └── Deny Access [REQUIRED]
```

The shape carries the security, so two things about it are worth stating plainly.

**The role check sits beside the authenticators, not inside them.** Until August 2026 it hung
in the Forms sub-flow, and the Cookie step is an alternative that runs before Forms. A user
who already had a session in the realm therefore reached the application without ever being
checked. Such a session is easy to come by: every other client in the realm uses the default
browser flow, including Keycloak's built-in `account-console` and the `<client>-public`
client that OPI creates alongside each deployment. Reproduced on Keycloak 25.0.6: a local
account without the role signed in on the account console and then obtained an authorization
code for the protected client, which is exactly what the authorization wall redeems.

**The `Authenticate` wrapper is not decoration.** Keycloak ignores ALTERNATIVE executions as
soon as a REQUIRED one sits at the same level. Putting the role check directly beside Cookie
and Forms therefore disables every way of signing in and denies everyone, role holders
included. Wrapping the authenticators in one REQUIRED sub-flow keeps them alternatives among
themselves and leaves the conditional role check beside them. The order matters too: the
check runs after the wrapper, because before it there is no user to check and the deny would
fire on everybody. That order comes from the order the sub-flows are created in: Keycloak gives
a new sibling the highest existing priority plus one. Executions carry an explicit priority,
the same way `ensure_auto_link_first_broker_login_flow` does it. What does lose the ordering is
a hand-written, partial PUT on an execution's requirement, which resets its priority to 0; the
`_ensure_execution_in_flow` and `_ensure_subflow` helpers send the fetched object back whole
and therefore do not.

**What this does not cover.** Other clients in the same realm keep the default browser flow:
Keycloak's built-in `account-console`, the invite client (deliberately, or nobody could redeem
an invitation), and anything created through `additional-clients`. Signing in there without the
role still yields a realm session and tokens *for that client*. Since the role check moved next
to the authenticators, such a session no longer opens the protected deployment, but the
application behind that other client has no role check of its own. Adding a new client to the
realm therefore falls outside the gate until someone binds it here as well.

**Migration.** The flow was replaced rather than edited, under a new alias: a half-converted
flow locks everyone out. The first task that touches a project builds
`browser-role-gate-<client-id>`, points the clients at it, and only then deletes the old
`browser-restricted-<client-id>`.

### Post-Broker Login Flow (for SSO logins)

This flow runs after SSO authentication through an identity provider and checks if the user has the required role:

```
Post-Broker Login Flow (restricted)
└── Require Client Role [REQUIRED]
    (Custom authenticator that checks role and returns success/failure)
```

The post-broker login flow uses a custom `RequireClientRoleAuthenticator` SPI instead of conditional sub-flows. This is necessary because Keycloak's built-in conditional flows don't work correctly for post-broker flows - when the condition is skipped (user has the role), the flow has nothing to complete and fails with a generic error.

This dual-flow approach ensures that both direct logins and SSO logins are subject to the same role-based access restriction.

### Important: Session Behavior

The browser flow's role check runs on every login through the protected client, including
the one that reuses an existing realm session over the Cookie step. The post-broker login
flow is different: it **only runs when the user authenticates through the IdP**. It does NOT
run on every access attempt. This means:

- **First SSO login**: Post-broker flow runs, role is checked
- **Subsequent requests with valid session**: User is already authenticated, no role check occurs
- **After session expires**: User re-authenticates via IdP, post-broker flow runs again

**Implication**: If you remove a user's role while they have an active session, they can continue to access the application until their session expires. To immediately revoke access:

1. Remove the user's role
2. Invalidate their session(s) in Keycloak Admin Console (Users > Sessions > Sign out)
3. Or wait for the session to expire naturally (configurable in realm settings)

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
      errorMessage: "${accessDeniedNoPermission}"
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
| `errorMessage` | string | No | `${accessDeniedNoPermission}` | Theme message key in ${} format for Keycloak resolution |

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

The default error message is `${accessDeniedNoPermission}`. The `${...}` syntax is required for Keycloak to resolve the message key from the theme's properties files. To use a custom message, add the key to the nl-design-system theme and reference it with `${yourCustomKey}`.

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
      services:
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
              errorMessage: "${accessDeniedNoPermission}"
```

## Troubleshooting

### User sees "Invalid username or password" instead of access denied message

This indicates the custom RequireClientRoleAuthenticator SPI is not installed. Ensure:
- The `keycloak-saml-nameid-mapper-*.jar` is present in `/opt/keycloak/providers/`
- Keycloak was restarted after adding the JAR
- Check Keycloak logs for any SPI loading errors

### Role is not being checked (direct login)

Ensure:
- The client role exists on the correct client
- The `restrictAccess.role` matches the client role name exactly
- The flow override is set on the client, and points at `browser-role-gate-<client-id>` (check Client > Advanced > Authentication Flow Overrides). An override still pointing at `browser-restricted-<client-id>` is the old flow, which misses logins that reuse an existing realm session
- The same override is set on `<client-id>-public`

### Role is not being checked (SSO login)

If users can bypass the role check when using SSO but not when using direct login:
- Verify the post-broker login flow exists (check Authentication > Flows for `post-broker-restricted-<client-id>`)
- Verify the identity provider has the post-broker login flow set (check Identity Providers > your IdP > Post Broker Login Flow)
- Ensure there are identity providers configured in the realm before enabling `restrictAccess`
- Verify the RequireClientRoleAuthenticator is configured with the correct client ID and role name

### User can still access after role removal

This is expected behavior. The post-broker login flow only runs when the user authenticates through the IdP, not on every request. If the user already has an active session:
- Their session remains valid until it expires
- To immediately revoke access: go to Users > [user] > Sessions and click "Sign out" to invalidate their sessions
- Alternatively, configure shorter session timeouts in realm settings

### Custom error message not showing

- Verify the message key exists in the theme's `messages_*.properties` files
- Check that the theme is properly deployed to Keycloak
- The error message must use `${messageKey}` format (e.g., `${accessDeniedNoPermission}`)
- Check the authenticator config in Authentication > Flows > your flow > RequireClientRoleAuthenticator config

## Technical Implementation

### Custom Authenticator SPI

The access restriction uses a custom Keycloak authenticator SPI (`require-client-role-authenticator`) that:
1. Checks if the authenticated user has a specific client role
2. Calls `success()` if the user has the role (allowing the flow to complete)
3. Calls `failure()` with a custom error page if the user lacks the role

This approach was chosen because Keycloak's built-in conditional sub-flows don't work correctly for post-broker login flows - when a condition is skipped, the flow has nothing to complete and fails with a generic error.

## Dependencies

- Keycloak 21.0.0+
- Custom RequireClientRoleAuthenticator SPI (included in keycloak-saml-nameid-mapper JAR)
- nl-design-system theme with custom error messages

## Related Documentation

- [Keycloak YAML Configuration](../docs/keycloak-yaml-configuration.md)
- [Keycloak Server Admin - Authentication Flows](https://www.keycloak.org/docs/latest/server_admin/#_authentication-flows)
