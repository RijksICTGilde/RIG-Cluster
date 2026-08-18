# Invites

Invite users into a project's Keycloak realm through a shareable link. An invite is a
platform **service** (`opi/services/catalog/invite/`): a project admin creates and manages
invitations from the self-service portal, no git access to the projects repo required.

## What it is

Each invite is one entry with a **key**. The key is part of a public URL
(`https://<portal>/invite/<key>`). Anyone who opens that link can create (or SSO-link) an
account in the project's Keycloak realm and receive the realm roles the invite grants.

**The link is the secret.** There is no other access barrier: possession of the link is what
authorises account creation. So share it deliberately, and prefer the generated key (below)
over a self-chosen, guessable one. Invite keys are unique across *all* projects on the
platform (the redemption route resolves a key against every project), which the wizard
enforces when you save.

## How to use it

1. In the create/edit wizard, select the **Keycloak** service (invites require it -- an
   invite assigns a realm role). The **Uitnodiging** service auto-selects Keycloak and cannot
   be chosen without it.
2. Select the **Uitnodiging** service. A configuration step appears.
3. Add one or more invitations under *Actieve uitnodigingen*. For each, either type a key or
   leave it blank to have a secure random key generated on save.
4. Pick the realm role(s) to grant. The picker lists the roles from the Keycloak config
   (custom `realm-roles` plus the authorization-wall role, default `allowed-user`). Choose
   *Geen rol toekennen* to create an account with no extra rights.
5. Save. Editing invites does **not** trigger a deploy (nothing in the manifests changes).
6. On the project-details page, admins and owners see an **Uitnodigingen** block with the full
   link and a copy button for each invite.

## The generated key

Leaving the key blank fills it with `secrets.token_urlsafe(16)` -- 22 characters, ~128 bits.
The link becomes semi-secret (like an unlisted video: unguessable, but permanent, since there
is no expiry). A self-chosen key is kept exactly as typed. Keys allow letters, digits,
hyphens and underscores, 3-64 characters, and must start with a letter or digit.

## Configuration

Invites live under the invite service config in the project file:

```yaml
services:
  - publish-on-web
  - keycloak:
      config:
        restrict-access:
          enabled: true
          realm-role: allowed-user
  - name: invite
    config:
      default-language: nl
      active:
        - key: welcome-to-docs
          realm-roles:
            - allowed-user
          restrict-domain: rijksoverheid.nl
          contact-email: beheer@example.nl
          application-target:
            deployment: production
            component: docs
          auth-methods:
            - sso
            - local
          message:
            nl: Welkom bij het documentatieportaal
            en: Welcome to the documentation portal
          success-title:
            nl: Account aangemaakt
            en: Account created
          success-button:
            nl: Ga naar de applicatie
            en: Go to the application
```

| Field | Meaning |
|---|---|
| `default-language` | Language of the invite pages when the browser gives no preference (`nl`/`en`). |
| `active[].key` | The link secret. Blank at save time = generated. |
| `active[].realm-roles` | Keycloak realm roles granted on redemption. Empty = account only. |
| `active[].restrict-domain` | Only e-mail addresses of this domain may redeem (with or without a leading `@`). |
| `active[].contact-email` | Shown to the user as a contact, and on the role-not-assigned error page. |
| `active[].application-target` | Where the success-page button points, as a `{deployment, component, path}` choice within this project. |
| `active[].application-url` | Where the success-page button points, as a fixed address. For a destination outside this project. |
| `active[].auth-methods` | `sso` and/or `local`. Empty = both allowed (subject to the realm). |
| `active[].message` / `success-title` / `success-button` | `{nl, en}` texts for the invite pages. |

### Where the success button points

Two shapes, and at most one of them per invitation (both at once is rejected: nothing would
decide which the button follows). Neither is also fine -- then the page simply shows no
button.

**`application-target`** -- a destination inside this project, named as the CHOICE:

```yaml
application-target:
  deployment: production
  component: frontend
  path: /api          # only needed when the component publishes more than one path
```

The address is worked out when the success page is rendered, from the same derivation the
project-details page lists (`publish-on-web`). So it keeps pointing at the right place when
the subdomain, the domain format or the cluster changes. Use this for anything the project
itself publishes; the portal's picker writes exactly this, offering the addresses by name
(`production / frontend`).

`path` is only needed where it distinguishes: a component MAY publish several paths and
those are that many addresses. Publishes it one, leave `path` out.

If the target stops resolving -- the component was removed, publish-on-web was switched off,
the deployment is gone -- the success page shows NO button. A button pointing somewhere
wrong is worse: the user only finds out after clicking it.

**`application-url`** -- a fixed address, for a destination OUTSIDE this project:

```yaml
application-url: https://ergens.anders.example.nl/
```

Nothing is derived, so nothing follows a change either. The portal's picker only offers this
project's own addresses and therefore cannot write this field; set it through the API, the
CLI or by hand. A save through the portal leaves it alone, unless you pick a destination
there -- then the choice you just made wins and the fixed address is dropped.

Because the picker does not own that field, an invitation that stores a fixed address shows
*Geen knop tonen* in the picker while the success page does show its button. The form
framework hands a field's converter the whole project file but not which sequence item it is
rendering, so the picker cannot read a sibling field of its own entry. Nothing is lost by it
-- a save never touches the address -- but to change or remove a fixed address, use the API
or the CLI rather than the portal.

Older files that store a derivable `application-url` keep working unchanged. The service's
config migration (v1.0 -> v1.1) converts such an address into the target behind it where it
can match one, and leaves it exactly as it is where it cannot.

Advanced pass-through fields (`groups`, `client-roles`, and the deprecated `roles`, an alias
for `realm-roles`) validate but are not offered in the UI. Keys are hyphenated on disk; the
service model also accepts the underscore spelling that predates this service.

## Configuring via the API

Because the service owns a config model (`InviteConfig`), it is configurable through the
unified per-service REST endpoint without any dedicated code -- the route is generated from
the registry with the model as its typed body:

```
PUT /api/v2/projects/{project}/services/invite/config/project     # body = InviteConfig
GET /api/v2/projects/{project}/services/invite/config             # read current config
```

The body is validated by the same `InviteConfig` the wizard save runs through, so a rejected
value fails at request time (422). A write reconciles the project like any other config
change; the invite contributes no manifests, so that reconcile is a no-op for its own
resources. See `features/service-config-api.md` for the shared endpoint.

## What happens when a role disappears

If a realm role that an invite grants is later removed from the Keycloak config:

- **In the form**, the picker still shows the stored role, flagged *(bestaat niet meer)*, so
  saving never silently swaps it for a different role.
- **On redemption**, if the named role cannot be granted, the user sees an error page (not the
  success page). It states the account *was* created, that retrying will not work (a second
  attempt hits "user already exists"), and shows the invite's contact address. A deliberately
  role-less invite is unaffected -- it never attempts a role assignment.

## Dependencies

- **Keycloak service** (`requires: services/keycloak`): the realm the invite onboards into.
- The redemption flow (public `/invite/<key>` pages) talks to Keycloak via the Keycloak
  connector; the realm user is created at redemption time, outside the deploy cycle.

## Notes

- Removing an invite does **not** delete the realm users it already created -- once they
  exist, they are legitimate users independent of the invitation.
- There is no expiry: an invite link is valid until the invite is removed.
- Migration: projects that used the old top-level `invites:` block are moved to
  `services/invite/config` automatically (schema v2.5 -> v2.6).
- The invite service's own config schema is at **v1.1** (`invite.v1.1.json`). v1.1 added
  `application-target`; the step from v1.0 is forward-only and idempotent.
