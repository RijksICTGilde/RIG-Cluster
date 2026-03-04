# ZAD External User Support with Local Accounts

## Status
TODO - Planned feature for future implementation

## Overview
Currently, ZAD (Operations Manager) uses SSO-only authentication via production Keycloak (sso-rijk). This feature tracks the addition of **local account support** allowing admins to create user accounts while users sign in locally (without self-registration capability).

## Current State
- **Authentication**: SSO-only via production RIG Platform Keycloak
- **Bootstrap Config**: `bootstrap-sandbox.yaml`
- **User Management**: Automatic user creation on first SSO login
- **Limitations**: No local accounts, no non-SSO users, requires production Keycloak connectivity

## Desired State
Support a hybrid authentication model:
- **Admin-created local accounts**: Admins create user accounts via Keycloak admin panel
- **Dual authentication**: Users can sign in via:
  1. SSO Rijk (for those with SSO-Rijk access)
  2. Local username/password (for admin-created accounts)
- **No self-registration**: Users cannot create their own accounts
- **Account management**: Admins manage users, passwords, and roles

## Implementation Plan

### Phase 1: Update Keycloak Bootstrap Configuration
Switch from `bootstrap-sandbox.yaml` to a modified `sso-support.yaml`:

**Location**: `operations-manager/python/opi/configs/keycloak/bootstrap-sandbox.yaml`

**Changes**:
1. Set `registrationAllowed: false` (disable user self-registration)
2. Keep `loginWithEmailAllowed: true` (allow login form)
3. Keep `resetPasswordAllowed: true` (users can reset passwords)
4. Keep `authenticateByDefault: false` on identity provider (show login form with SSO button)
5. Standard browser authentication flow (not forced SSO redirect)

**Result**: Login page shows both options:
- "Login with SSO Rijk" button
- "Login with local account" option (username/password)

### Phase 2: Admin User Management
Admins need tools to manage local user accounts:
- **Keycloak Admin Console**: Direct UI for creating/managing users
- **Operations Manager API** (optional future): Add endpoints for user creation/management
- **CLI tooling** (optional future): kubectl/direct Keycloak API access

### Phase 3: Testing & Documentation
- Test hybrid authentication flow in all cluster types (local, sandboxed-local, odcn-production)
- Document admin user creation process
- Document user sign-in process
- Test password reset flows for local accounts

## Related Files
- `operations-manager/python/opi/configs/keycloak/bootstrap-sandbox.yaml` - Main bootstrap config
- `operations-manager/python/opi/configs/keycloak/sso-support.yaml` - Reference implementation
- `operations-manager/python/opi/configs/keycloak/sso-only.yaml` - Current implementation

## Configuration Reference

### sso-support.yaml Key Settings (for local account support)
```yaml
registrationAllowed: false          # Disable self-registration
loginWithEmailAllowed: true         # Enable login form
resetPasswordAllowed: true          # Allow password resets
editUsernameAllowed: false          # Prevent username changes

identityProviders:
  authenticateByDefault: false      # Don't auto-redirect to SSO
```

This creates a standard browser login flow with both local and SSO options.

## Security Considerations
- Admins must have strong password management practices
- Local account passwords should enforce complexity requirements
- MFA support (optional future enhancement)
- Regular audit of admin-created accounts
- Consider integration with organization directory for bulk user creation

## Migration Path
This is backward compatible:
- Existing SSO users continue to work
- New local users can be created for non-SSO scenarios
- No impact on current deployment process

## Open Questions
- Should bulk user creation be supported (CSV import)?
- Should user provisioning from external directory be supported?
- What password policies for local accounts?
- Should be implemented alongside any changes to operations manager?
