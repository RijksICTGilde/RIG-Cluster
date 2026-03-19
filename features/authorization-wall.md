# Authorization Wall

## What it is

The `authorization-wall` is a service that puts an authentication proxy in front of a web application. It is designed for static websites or applications that have no backend capable of handling OIDC authentication themselves. When enabled, unauthenticated users are redirected to Keycloak for login before they can access the application.

## How it works

An [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) sidecar container is added to the application's deployment. All incoming traffic is routed through the proxy (port 4180) instead of directly to the application. The proxy handles the complete OIDC flow with Keycloak:

```
Browser → ingress → service:4180 → oauth2-proxy (sidecar) → app (localhost:app_port)
```

1. User visits the application URL
2. oauth2-proxy checks for a valid session cookie
3. If no session: redirects to Keycloak login page
4. After login: Keycloak redirects back to `/oauth2/callback`
5. oauth2-proxy sets a session cookie and forwards the request to the application

## How to use it

### Prerequisites

The project must have `keycloak` configured in its services (the authorization wall reuses the project's Keycloak client credentials).

### Project YAML

1. Add `authorization-wall` to the project-level `services:` section
2. Add `authorization-wall` to the component's `uses-services` list

```yaml
services:
  - keycloak:
      config:
        template: sso-support
  - authorization-wall

components:
  - name: my-static-site
    ports:
      inbound: [8080]
    uses-services:
      - publish-on-web
      - authorization-wall
```

### Banner (optional sign-in page)

By default, the authorization wall redirects directly to Keycloak without showing an intermediate page. To show a sign-in page with a custom message before the Keycloak redirect, add a `banner` in the project-level `services:` config:

```yaml
services:
  - authorization-wall:
      config:
        banner: "Welcome to our application. Please log in with your SSO account."
```

When a banner is set, users see oauth2-proxy's built-in sign-in page with the banner text and a "Sign in" button. Without a banner, users are redirected to Keycloak automatically.

### What gets generated

When `authorization-wall` is enabled for a component:

- **Deployment**: An `authorization-wall` sidecar container is added alongside the `app` container
- **Service**: Traffic is routed to port 4180 (oauth2-proxy) instead of the application port
- **Cookie secret**: A SOPS-encrypted Kubernetes Secret with a random cookie encryption key

No ingress changes are needed - the ingress routes to the service as normal.

## Configuration

The authorization wall is configured automatically from the project's existing Keycloak service configuration:

| Setting | Source |
|---------|--------|
| OIDC Issuer URL | Keycloak realm discovery URL |
| Client ID | Project's Keycloak client ID |
| Client Secret | Project's Keycloak client secret (from envFrom) |
| Cookie Secret | Auto-generated random 32-byte key |
| Redirect URL | `https://<hostname>/oauth2/callback` |

## Dependencies

- `keycloak` service must be configured at the project level
- `publish-on-web` should be enabled for the component (otherwise there's no ingress to protect)
