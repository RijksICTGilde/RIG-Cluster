# Authorization Wall: Custom Sign-in Page

## Overview

Replace the default oauth2-proxy sign-in page with a custom template using the NL Design System (Rijkshuisstijl) styling, consistent with the Keycloak theme and the Operations Manager portal.

## Why

The default oauth2-proxy sign-in page uses generic Bulma CSS styling that looks out of place. A custom template using the same `rvo-theme` and `<c-page>` web components as the rest of the platform would provide a consistent user experience.

## Approach

oauth2-proxy supports `--custom-templates-dir` for custom Go HTML templates. The `sign_in.html` template can be:

1. Created as a static Go template with NL Design System CSS (same `rvo-theme` used in the Operations Manager portal)
2. Packaged as a ConfigMap in the deployment
3. Mounted into the authorization-wall sidecar container
4. Activated via `--custom-templates-dir=/etc/oauth2-proxy/templates`

The banner text (`.SignInMessage`) is already available as a Go template variable from the `--banner` flag.

## Template Variables (oauth2-proxy)

- `.SignInMessage` — banner text from `--banner` flag
- `.ProxyPrefix` — OAuth2 proxy prefix path
- `.ProviderName` — provider name (e.g. "OpenID Connect")
- `.Redirect` — redirect URL after login
- `.Logo` — logo URL (via `--custom-sign-in-logo`)
- `.Footer` — footer text (via `--footer`)

## Multi-language Support

The current banner text is a single string in the project YAML. For a proper multi-language experience, the project file needs a generic localization solution so that user-facing text (like the banner) can be provided in multiple languages.

Current approach (single language):
```yaml
services:
  - authorization-wall:
      config:
        banner: "Welkom bij onze applicatie. Log in met uw SSO-Rijk account."
```

A future solution should support language variants at the project level in a generic way that can be reused across services, not just the authorization wall. This would allow the sign-in page (and potentially other user-facing elements) to render in the user's preferred language.

## Dependencies

- Authorization wall feature (implemented)
- NL Design System CSS (already used in Operations Manager templates via `rvo-theme`)
