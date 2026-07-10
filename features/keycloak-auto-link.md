# Keycloak auto-link of pre-created users to their SSO identity

## What it is

ZAD lets you pre-create users in a project's Keycloak realm (by email, plus a role such as
`allowed-user` for the authorization wall) so they are authorized before they ever log in.
Previously, when such a pre-created account shared its email with the identity a user brought
in via SSO, Keycloak's stock **"first broker login"** flow routed them through
**"Confirm Link Existing Account" → "Verify Existing Account By Email / Re-authentication"**.
That needs an email round-trip or a password the pre-created account does not have, so the SSO
identity and the pre-created account could not bind without a manual step (the "account
collision").

This feature replaces that branch with a custom first-broker-login flow that uses Keycloak's
built-in **`idp-auto-link`** authenticator ("Automatically Set Existing User"). On first SSO the
brokered identity is linked to the matching pre-existing account **automatically** (no email, no
password), optionally after a single confirmation screen.

Only the "an account with this email already exists" branch changes. Brand-new users (no
pre-existing account) and already-linked users are unaffected.

## How to use it

1. In the project's Keycloak realm, create the user up front with **username = the email** the
   IdP will assert, and grant whatever they need (e.g. the `allowed-user` realm role for the
   authorization wall, group memberships, attributes).
2. The user logs in via SSO. Their brokered identity is auto-linked to that account.

No invite link, email verification, or password step is required.

## Configuration

Auto-link is **opt-in per project realm** (each ZAD project is one realm). It is a project-only
feature: the platform realm always keeps Keycloak's stock flow.

In the project file under the Keycloak service:

```yaml
services:
  keycloak:
    config:
      restrict-access:
        realm-role: allowed-user
      account-link: automatic   # automatic | confirm | verify
```

`account-link` accepts three modes:

| Value | Behaviour |
|---|---|
| `automatic` | Link a brokered SSO identity to a pre-existing account silently (no page). |
| `confirm` | Same, after one "confirm link" screen. UX clarity, **not** a security control (the user can just click "yes"). |
| `verify` | Keycloak's stock flow: the user proves ownership of the existing account by email link or re-authentication. This is also what you get when `account-link` is **omitted**. |

Omitting `account-link` is identical to `account-link: verify`.

### Switching modes on an existing project

You can change `account-link` at any time. On the next reconcile the change takes effect:

- The custom first-broker-login flow is reconciled in place (e.g. `automatic` -> `confirm`
  adds the confirmation screen; `confirm` -> `automatic` removes it) by
  `ensure_auto_link_first_broker_login_flow`, which is idempotent.
- The IdP's `firstBrokerLoginFlowAlias` is re-pointed by the connector's 409-diff-update:
  `verify` -> `automatic`/`confirm` points it at the custom flow; the reverse points it back at
  the stock `"first broker login"` flow (the custom flow is left in place, unreferenced).

The setting is configured via the project YAML (like `template`, `variables`, and
`additional-clients`); a wizard toggle is a possible follow-up.

## Security

Auto-link means anyone the trusted IdP authenticates with email X inherits the pre-created
account for X. This is safe **because SSO-Rijk / BZK authoritatively verifies the email**
(the IdP is created with `trustEmail=true`, and realms use `duplicateEmailsAllowed=false`, which
`idp-create-user-if-unique` needs to match an existing account by email). For a low-trust IdP,
auto-linking by email would be an account-takeover vector.

## How it works

`KeycloakConnector.ensure_auto_link_first_broker_login_flow` (idempotently) builds this flow:

```
first broker login auto-link                      (top-level, basic-flow)
|-- idp-review-profile                   DISABLED
+-- ... user creation or linking         REQUIRED    (subflow)
    |-- idp-create-user-if-unique        ALTERNATIVE
    +-- ... handle existing account      ALTERNATIVE (subflow)
        |-- idp-confirm-link             REQUIRED    (only when account-link=confirm)
        +-- idp-auto-link                REQUIRED
```

`idp-create-user-if-unique` must precede the handle-existing subflow: it creates the account for
brand-new users, or (when a match exists) stashes the existing user for `idp-auto-link` to bind.
If the order is wrong, `idp-auto-link` runs with no existing-user context and linking a
pre-created account fails with **"Invalid username or password"**.

### Execution ordering (important)

Building a flow via the Keycloak admin API does NOT give a deterministic order: without an
explicit priority every execution defaults to priority 0, so siblings tie and Keycloak orders
them arbitrarily (keycloak#43016). The `index` in a PUT is ignored (keycloak#8726) and
`raise-priority` is a no-op on equal priorities. The reliable fix (as keycloak-config-cli does,
Keycloak >= 25) is to send an **explicit `priority` in the execution create body**:
`idp-create-user-if-unique` is created with priority 10, so the handle-existing subflow lands at
`getNextPriority` = 11 and always sorts after it. `_ensure_execution_in_flow` passes this priority.

To inspect or repair the flow order on a live realm, use
`operations-manager/python/scripts/keycloak_flow_tool.py` (`inspect <realm>`, `rebuild <realm>`,
`inspect-all`), which talks to Keycloak directly with the same python-keycloak library. `rebuild`
deletes and recreates the flow with explicit priorities. Run it from `operations-manager/python`
with `KEYCLOAK_ADMIN_PASSWORD` set (see `operations-manager/python/scripts/README.md`).

The IdP creators (`add_identity_provider`, `add_saml_identity_provider`) default
`firstBrokerLoginFlowAlias` to the stock `"first broker login"`. Only
`KeycloakYamlHandler._process_identity_providers` opts a **project** realm into the custom flow
(constant `AUTO_LINK_FIRST_BROKER_LOGIN_FLOW`) when its `account-link` is `automatic`/`confirm`:
it ensures the flow exists (before the IdP loop) and passes the alias to the IdP creators. The
connector's idempotent 409-diff-update converges an existing IdP onto whichever alias is passed,
so switching modes re-points it either way. The platform realm never sets `account_link`, so its
BZK broker keeps the stock flow.

This flow is independent of the `allowed-user` authorization wall: linking happens in
**first-broker-login** (once, on first SSO), the wall enforces the role in the separate
**post-broker-login** flow (every login).

## Attribute sync on linked accounts

A pre-created account is never "created" by the IdP, so with the mappers' original
`syncMode: IMPORT` (import once, at creation) its attributes were never populated from SSO. To
populate and keep them in sync, the project federation IdP (`rig-platform-oidc`) attribute
mappers in `sso-only.yaml` and `sso-support.yaml` use **`syncMode: FORCE`** (email, first/last/
full name, organization number/name, sso-rijk-userid/-lowercase). `FORCE` re-applies the mappers
on every login, so a linked account's profile tracks the IdP.

`email-to-username` deliberately stays `INHERIT` (effective `IMPORT`) so usernames stay stable
and linking by username is not disturbed.

Tradeoffs of `FORCE`: the IdP becomes the source of truth for those fields (local edits are
overwritten each login), and if the IdP omits a claim on a login the corresponding attribute can
be cleared. This is acceptable when the IdP reliably sends the claims. There is no built-in
"populate only if empty" mode in Keycloak IdP mappers; it is `IMPORT` (once) or `FORCE` (every
login).

## Note: the "Review profile" link on the confirm screen

In `confirm` mode, Keycloak's stock `login-idp-link-confirm.ftl` renders a "Review profile" link
alongside "Add to existing account". Because this flow sets `idp-review-profile` to `DISABLED`,
that link has no target and appears to do nothing (a flow reset that returns to the same page);
"Add to existing account" is the working action. Hiding the link would require overriding that
template in the `nl-design-system` Keycloak theme (a separate artifact), not in this repo.

## Dependencies

- Keycloak built-in authenticators: `idp-review-profile`, `idp-create-user-if-unique`,
  `idp-confirm-link`, `idp-auto-link`.
- Realm `duplicateEmailsAllowed=false` (set by the SSO setup) so email matching works.
- The `email-to-username` IdP mapper (`${CLAIM.email}`) on project realms makes the brokered
  username deterministic for matching.

## Rollback

The stock `"first broker login"` flow is left untouched; a new flow is added alongside it.
Rollback = repoint `firstBrokerLoginFlowAlias` back to `"first broker login"`.
