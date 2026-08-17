# Expose Component on Bare Domain

> **Waar dit wordt opgeslagen (schemaversie 2.7):** de velden hieronder staan in het
> projectbestand onder `deployments[].services[publish-on-web].config`, niet meer los in de
> wortel van de deployment. Zie
> [webadres-onder-de-dienst.md](../../../features/webadres-onder-de-dienst.md). De
> YAML-fragmenten in dit document tonen de velden zonder dat omhulsel, om over hun betekenis
> te gaan en niet over hun plek.

## Overview

Allows a deployment to serve traffic on the bare/apex domain (e.g., `voorbeeld.nl`) alongside the prefixed domain (e.g., `www.voorbeeld.nl`). Only available for custom (own) domains, not platform domains.

## Configuration

Set `expose-component-on-bare-domain` on the deployment to the component name that should serve the bare domain:

```yaml
deployments:
  - name: productie
    subdomain: www
    base-domain: voorbeeld.nl
    expose-component-on-bare-domain: frontend
    issuer: letsencrypt
    components:
      - reference: frontend
        image: nginx:latest        # serves www.voorbeeld.nl AND voorbeeld.nl
      - reference: api
        image: myapi:latest        # serves www-api.voorbeeld.nl only
```

## How It Works

When `expose-component-on-bare-domain` is set:

1. An additional Kubernetes Ingress is created for the bare domain (`voorbeeld.nl`), pointing to the selected component's service
2. The bare domain gets its own TLS certificate via cert-manager
3. The bare domain is registered in the subdomain registry (as `@.voorbeeld.nl`) to prevent other projects from claiming it
4. Keycloak redirect URIs include the bare domain

## Wizard

In the create wizard and domain edit modal, a dropdown appears when a custom domain is selected:

- **Label**: "Bereikbaar op kaal domein"
- **Options**: Component names + "Niet bereikbaar op kaal domein" (none)
- **Visibility**: Only shown when base-domain is a custom domain

The URL preview updates to show the bare domain when a component is selected.

## Constraints

- Only works with custom domains (own domains like `voorbeeld.nl`), not platform domains (`rijksapp.nl`, `rijksapp.dev`, etc.)
- Only one component can serve the bare domain per deployment

## DNS Requirements

The client must configure an **A record** for the bare/apex domain pointing to the ingress controller's IP address. Standard DNS does not allow CNAME records on apex domains. Some DNS providers support ALIAS or ANAME records as an alternative.

## Subdomain Registry

The bare domain is tracked in the subdomain registry with `@` as the subdomain value (DNS convention for apex). This prevents multiple projects from claiming the same bare domain.

When the bare domain component is deselected (set to none), the registry entry is automatically cleaned up.
