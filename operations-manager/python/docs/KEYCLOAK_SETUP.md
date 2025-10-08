# Keycloak Setup for Operations Manager

This document provides step-by-step instructions for configuring Keycloak to work with the Operations Manager SSO system, including custom attribute passthrough.

## Prerequisites

- Keycloak instance running and accessible
- Admin access to Keycloak Admin Console
- Understanding of OIDC/OAuth2 flows

## 1. Create Custom Client Scope for Attributes

### Step 1.1: Create Client Scope

1. Navigate to **Keycloak Admin Console**
2. Go to **Client Scopes** in the left menu
3. Click **Create client scope**
4. Configure the client scope:
   - **Name**: `custom_attributes_passthrough`
   - **Description**: `Passes custom user attributes (organization info) to tokens`
   - **Type**: `Default`
   - **Protocol**: `openid-connect`
   - **Display on consent screen**: `OFF` (for seamless UX)
   - **Include in token scope**: `ON`

### Step 1.2: Add Protocol Mappers

Navigate to the **Mappers** tab of the `custom_attributes_passthrough` client scope.

#### Organization Name Mapper

Click **Add mapper** → **By configuration** → **User Attribute**

```
Name: Organization Name Passthrough
Mapper Type: User Attribute
User Attribute: organization.name
Token Claim Name: organization.name
Claim JSON Type: String
Add to ID token: ON
Add to access token: ON
Add to userinfo: ON
Multivalued: OFF
Aggregate attribute values: OFF
```

#### Organization Number Mapper

Click **Add mapper** → **By configuration** → **User Attribute**

```
Name: Organization Number Passthrough
Mapper Type: User Attribute
User Attribute: organization.number
Token Claim Name: organization.number
Claim JSON Type: String
Add to ID token: ON
Add to access token: ON
Add to userinfo: ON
Multivalued: OFF
Aggregate attribute values: OFF
```

#### Optional: Organization Role Mapper

If you need organization roles:

```
Name: Organization Role Passthrough
Mapper Type: User Attribute
User Attribute: organization.role
Token Claim Name: organization.role
Claim JSON Type: String
Add to ID token: ON
Add to access token: ON
Add to userinfo: ON
```

## 2. Create Operations Manager Client

### Step 2.1: Basic Client Configuration

1. Go to **Clients** in the left menu
2. Click **Create client**
3. Configure the client:
   - **Client type**: `OpenID Connect`
   - **Client ID**: `rig-platform-operations-manager`
   - **Name**: `RIG Platform Operations Manager`
   - **Description**: `Operations Manager for RIG Platform`

### Step 2.2: Client Settings

Configure the following settings:

```
General Settings:
- Client ID: rig-platform-operations-manager
- Name: RIG Platform Operations Manager
- Enabled: ON

Access settings:
- Root URL: http://127.0.0.1:9595
- Valid redirect URIs: 
  * http://127.0.0.1:9595/*
  * http://localhost:9595/*
  * http://operations-manager.kind/*
- Valid post logout redirect URIs: 
  * http://127.0.0.1:9595/
  * http://localhost:9595/
  * http://operations-manager.kind/
- Web origins: 
  * http://127.0.0.1:9595
  * http://localhost:9595
  * http://operations-manager.kind

Capability config:
- Client authentication: ON (for confidential clients)
- Authorization: OFF
- Standard flow: ON
- Implicit flow: OFF
- Direct access grants: OFF
- OAuth 2.0 Device Authorization Grant: OFF
- OIDC CIBA Grant: OFF

Login settings:
- Login theme: (leave empty for default)
- Consent required: OFF
- Display client on screen: OFF
```

### Step 2.3: Assign Client Scopes

1. Go to the **Client scopes** tab of your client
2. Click **Add client scope**
3. Add the following scopes as **Default**:
   - `openid`
   - `profile`
   - `email`
   - `custom_attributes_passthrough` ← **This is crucial!**

## 3. Configure User Attributes

For each user that should have organization information:

1. Go to **Users** → Select user → **Attributes** tab
2. Add the following attributes:
   - Key: `organization.name`, Value: `Rijksoverheid`
   - Key: `organization.number`, Value: `00000001003214345000`
   - Key: `organization.role`, Value: `developer` (optional)

## 4. Environment Configuration

Update your Operations Manager environment configuration:

```env
# OIDC Configuration
OIDC_CLIENT_ID=rig-platform-operations-manager
OIDC_CLIENT_SECRET=<your-client-secret>
OIDC_DISCOVERY_URL=http://keycloak.kind/realms/rig-platform/.well-known/openid-configuration
```

## 5. Testing the Configuration

### Step 5.1: Test Token Content

You can verify that custom attributes are being passed by checking the token response logs or using the `/auth/user` endpoint.

Expected token structure:
```json
{
  "sub": "user-id",
  "email": "user@example.com",
  "name": "User Name",
  "organization.name": "Rijksoverheid",
  "organization.number": "00000001003214345000",
  "organization.role": "developer"
}
```

### Step 5.2: Verify in Operations Manager

1. Log into the Operations Manager
2. Check that your full name appears in the navigation
3. Verify organization info is available via API: `GET /auth/user`

## 6. Troubleshooting

### Common Issues

**Custom attributes not appearing in tokens:**
- Verify client scope is assigned to the client as **Default**
- Check that mappers are configured correctly
- Ensure user has the custom attributes set

**Login redirects failing:**
- Verify redirect URIs match exactly (including ports)
- Check that client authentication is properly configured

**Token parsing errors:**
- Verify OIDC discovery URL is accessible
- Check client secret is correct
- Review Operations Manager logs for detailed error messages

### Debug Commands

Check token content:
```bash
# Decode JWT token (without signature verification)
echo "your-jwt-token" | cut -d'.' -f2 | base64 -d | jq .
```

Test OIDC endpoints:
```bash
# Test discovery endpoint
curl http://keycloak.kind/realms/rig-platform/.well-known/openid-configuration

# Test userinfo endpoint (with valid access token)
curl -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
     http://keycloak.kind/realms/rig-platform/protocol/openid-connect/userinfo
```

## 7. Production Considerations

### Security
- Use HTTPS in production environments
- Configure proper CORS settings
- Use strong client secrets
- Enable proper session management

### Scalability
- Consider client scope inheritance for multiple clients
- Use realm-level settings where appropriate
- Document attribute naming conventions

### Maintenance
- Regular backup of Keycloak configuration
- Version control for client configurations
- Monitor token expiration settings