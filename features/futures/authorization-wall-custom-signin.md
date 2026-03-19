# Authorization Wall: Custom Sign-in Page

**Status**: Planned
**Priority**: Low
**Created**: 2026-02-08

## Overview

Replace the default oauth2-proxy sign-in page with a custom template using the NL Design System (Rijkshuisstijl) styling, consistent with the Keycloak theme and the Operations Manager portal.

## Why

The default oauth2-proxy sign-in page uses generic Bulma CSS styling that looks out of place. A custom template using the same `rvo-theme` and `<c-page>` web components as the rest of the platform would provide a consistent user experience.

## Current State

The `sidecar-authorization-wall.yaml.jinja` template **already includes a custom sign-in template** via a ConfigMap:

```yaml
# Section: configmap
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ name }}-oauth2-signin
data:
  sign_in.html: |
    <!-- Current template with Rijksoverheid styling -->
```

The current template uses basic Rijksoverheid CSS. This feature spec focuses on upgrading it to the full NL Design System with proper `rvo-theme` components and adding multi-language support.

## Template Variables (oauth2-proxy)

Available Go template variables in `sign_in.html`:

- `.SignInMessage` - banner text from `--banner` flag
- `.ProxyPrefix` - OAuth2 proxy prefix path
- `.ProviderName` - provider name (e.g. "OpenID Connect")
- `.Redirect` - redirect URL after login
- `.Logo` - logo URL (via `--custom-sign-in-logo`)
- `.Footer` - footer text (via `--footer`)

---

## Implementation

### Phase 1: NL Design System Sign-in Template

**File**: `manifests/sidecar-authorization-wall.yaml.jinja` (modify, configmap section)

Replace the current `sign_in.html` template content with a proper NL Design System page:

```html
<!DOCTYPE html>
<html lang="{{ language | default('nl') }}" class="rvo-theme">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Inloggen - {{ app_name | default('Applicatie') }}</title>
    <link rel="stylesheet" href="https://unpkg.com/@nl-rvo/design-tokens/dist/index.css">
    <link rel="stylesheet" href="https://unpkg.com/@nl-rvo/assets/dist/index.css">
    <link rel="stylesheet" href="https://unpkg.com/@nl-rvo/components/dist/index.css">
    <style>
        body {
            margin: 0;
            font-family: 'RO Sans', 'Helvetica Neue', Arial, sans-serif;
            background-color: var(--rvo-color-wit);
            display: flex;
            flex-direction: column;
            min-height: 100vh;
        }
        .signin-container {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 2rem;
        }
        .signin-card {
            max-width: 480px;
            width: 100%;
            background: var(--rvo-color-wit);
            border: 1px solid var(--rvo-color-grijs-300);
            border-radius: 4px;
            padding: 2.5rem;
        }
        .signin-card h1 {
            color: var(--rvo-color-hemelblauw);
            font-size: 1.5rem;
            margin: 0 0 1rem 0;
        }
        .signin-card .message {
            color: var(--rvo-color-grijs-700);
            margin-bottom: 1.5rem;
            line-height: 1.5;
        }
        .signin-card .rvo-button {
            width: 100%;
            text-align: center;
            padding: 0.75rem 1.5rem;
            font-size: 1rem;
        }
        .rvo-header {
            background-color: var(--rvo-color-hemelblauw);
            color: var(--rvo-color-wit);
            padding: 1rem 2rem;
        }
        .rvo-header__title {
            font-size: 1.25rem;
            font-weight: 700;
        }
        .rvo-footer {
            background-color: var(--rvo-color-hemelblauw);
            color: var(--rvo-color-wit);
            padding: 1rem 2rem;
            text-align: center;
            font-size: 0.875rem;
        }
        .rvo-logo {
            height: 40px;
            margin-right: 1rem;
        }
    </style>
</head>
<body>
    <header class="rvo-header">
        <div style="display: flex; align-items: center;">
            {{ "{{" }} if .Logo {{ "}}" }}
            <img src="{{ "{{" }} .Logo {{ "}}" }}" alt="Logo" class="rvo-logo">
            {{ "{{" }} end {{ "}}" }}
            <span class="rvo-header__title">{{ app_name | default('Applicatie') }}</span>
        </div>
    </header>

    <main class="signin-container">
        <div class="signin-card">
            <h1>Inloggen</h1>

            {{ "{{" }} if .SignInMessage {{ "}}" }}
            <p class="message">{{ "{{" }} .SignInMessage {{ "}}" }}</p>
            {{ "{{" }} end {{ "}}" }}

            <form method="GET" action="{{ "{{" }} .ProxyPrefix {{ "}}" }}/start">
                <input type="hidden" name="rd" value="{{ "{{" }} .Redirect {{ "}}" }}">
                <button type="submit" class="rvo-button rvo-button--primary">
                    Inloggen met {{ "{{" }} .ProviderName {{ "}}" }}
                </button>
            </form>
        </div>
    </main>

    <footer class="rvo-footer">
        {{ "{{" }} if .Footer {{ "}}" }}
        <p>{{ "{{" }} .Footer {{ "}}" }}</p>
        {{ "{{" }} else {{ "}}" }}
        <p>Powered by de Rijksoverheid</p>
        {{ "{{" }} end {{ "}}" }}
    </footer>
</body>
</html>
```

**Note**: The template contains both Jinja2 variables (for OPI template rendering: `{{ app_name }}`) and Go template variables (for oauth2-proxy runtime: `{{ "{{" }} .SignInMessage {{ "}}" }}`). The Jinja2 layer renders first during manifest generation; the Go template layer renders at request time in oauth2-proxy.

### Phase 2: Multi-Language Support

**Design Decision**: Use the `Accept-Language` header to detect language preference, with a fallback to the project's configured language.

oauth2-proxy does not natively support per-request template variable injection based on headers. The solution is to provide a single template with JavaScript-based language switching:

**Approach**: Embed translations as JSON in the template and switch text client-side:

```html
<script>
const translations = {
    nl: {
        title: "Inloggen",
        button: "Inloggen met",
        footer: "Powered by de Rijksoverheid"
    },
    en: {
        title: "Sign in",
        button: "Sign in with",
        footer: "Powered by the Dutch Government"
    }
};

const lang = navigator.language.startsWith('en') ? 'en' : 'nl';
document.documentElement.lang = lang;
const t = translations[lang];
document.querySelector('h1').textContent = t.title;
document.querySelector('.rvo-button').textContent =
    t.button + ' {{ "{{" }} .ProviderName {{ "}}" }}';
</script>
```

### Phase 3: Project-Level Configuration

**YAML changes**: Extend the authorization-wall service config to support template customization:

```yaml
components:
  - name: frontend
    uses-services:
      - authorization-wall:
          config:
            banner: "Welkom bij onze applicatie. Log in met uw SSO-Rijk account."
            app-name: "AMT"                    # Shown in header
            logo-url: "/static/logo.svg"       # Custom logo
            footer-text: "Ministerie van BZK"  # Custom footer
            default-language: "nl"             # nl | en
```

**File**: `manifests/sidecar-authorization-wall.yaml.jinja` (modify, container section)

Pass the new config values as oauth2-proxy flags:

```yaml
args:
  # ... existing args ...
  - --banner={{ authorization_wall.banner | default('') }}
  - --custom-sign-in-logo={{ authorization_wall.logo_url | default('') }}
  - --footer={{ authorization_wall.footer_text | default('') }}
```

### Phase 4: Wizard Integration

**File**: `opi/forms/visualizers/wizard_sections.py` (modify)

Add the new fields to the authorization wall section:

```python
AUTH_WALL_APP_NAME = Editable(
    name="app-name",
    yaml_path="services/authorization-wall/config/app-name",
    editable_type=EditableType.TEXT,
    label="Applicatienaam",
    help_text="Wordt getoond in de header van de inlogpagina",
    required=False,
    placeholder="Mijn Applicatie",
)

AUTH_WALL_LOGO_URL = Editable(
    name="logo-url",
    yaml_path="services/authorization-wall/config/logo-url",
    editable_type=EditableType.TEXT,
    label="Logo URL",
    help_text="URL naar een logo voor de inlogpagina (optioneel)",
    required=False,
)

AUTH_WALL_FOOTER = Editable(
    name="footer-text",
    yaml_path="services/authorization-wall/config/footer-text",
    editable_type=EditableType.TEXT,
    label="Footer tekst",
    help_text="Tekst onderaan de inlogpagina",
    required=False,
    placeholder="Powered by de Rijksoverheid",
)
```

---

## ConfigMap Size Consideration

The NL Design System CSS is loaded from unpkg CDN (not bundled in the ConfigMap), keeping the ConfigMap small (~3KB for the HTML template). This avoids the 1MB ConfigMap size limit.

If CDN access is not available in a restricted network:
1. Bundle the CSS as a second ConfigMap
2. Serve it from the oauth2-proxy static file serving (`--upstream=file:///etc/oauth2-proxy/static#/static/`)
3. Reference as `<link rel="stylesheet" href="/static/rvo-theme.css">`

## Fallback Strategy

If the custom template fails to load (ConfigMap mount failure):
- oauth2-proxy falls back to its built-in default template
- Users can still authenticate - just with the generic Bulma-styled page
- No functional impact, only visual

---

## Files Summary

### Modified Files

| File | Change |
|------|--------|
| `manifests/sidecar-authorization-wall.yaml.jinja` | Updated sign_in.html template with NL Design System + multi-language JS |
| `opi/forms/visualizers/wizard_sections.py` | Add app-name, logo-url, footer-text fields to AUTH_WALL_SECTION |

### No New Files

All changes are modifications to existing files.

---

## Dependencies

- Authorization wall feature (implemented)
- NL Design System CSS (available via unpkg CDN, already used in Operations Manager via `rvo-theme`)
- oauth2-proxy custom templates feature (already configured via `--custom-templates-dir`)

## Verification

1. **Visual**: Sign-in page uses Rijkshuisstijl colors and typography
2. **Banner**: Custom banner text from project YAML appears on the page
3. **Language**: Open in English browser, verify text switches to English
4. **Logo**: Set `logo-url`, verify logo appears in header
5. **Footer**: Set `footer-text`, verify footer text changes
6. **Fallback**: Delete the ConfigMap, verify oauth2-proxy shows default page (still functional)
7. **Redirect**: After clicking sign-in, user is redirected to Keycloak and back to the app
