# Keycloak Realm-Admin Shared OTP

## What it is

Auto-provisioned Keycloak realm-admin accounts now require OTP (TOTP) at the
`/admin/` console login. Because these are **shared** service accounts (one
master-realm admin user per project realm), the TOTP seed is a value managed by
ZAD: OPI generates it, stores it AGE-encrypted in the project file alongside the
admin password, and surfaces it in the portal. Every project admin loads the
same seed into their password manager or authenticator app, so they all produce
identical codes and shared realm access keeps working.

No realm-wide policy change is needed: Keycloak's stock `browser` flow (used by
the master `/admin/` console) contains a *Conditional OTP Form* that requires
OTP for any user who has an OTP credential and skips it otherwise. Provisioning
the credential is therefore sufficient to enforce OTP for that user.

## Two OTPs, two purposes - do not conflate them

This is the OTP *in Keycloak*. There is a second, related OTP *in ZAD* (on the
user table), and they exist for different reasons. See also
[opi-keycloak-service-account.md](opi-keycloak-service-account.md), the
prerequisite that makes hardening the `admin` account possible.

- **OTP in Keycloak (this feature)** protects the Keycloak console. It makes a
  leaked or forwarded admin password worthless and blocks brute force at the
  login screen. That is the whole goal, and for it, storing the seed the same way
  as the other secrets is enough. It is explicitly **not** protection against
  someone who can already read the secret store - that is a different threat model
  with a different control. Because the account is *shared*, OPI has to invert the
  normal flow (OPI generates the seed and pushes it into Keycloak, rather than a
  single person scanning a QR), otherwise only the first person to scan could log
  in.
- **OTP in ZAD** (the other plan, `plans/otp-en-verhoogde-rechten.md`) is the real
  second factor. It is bound to a person, not a shared account, and the elevated
  rights hang off it. That is where "you are doing this deliberately and you are
  really you" gets enforced.

The end state where the Keycloak side *also* gets a person-bound factor is a
personal master account per admin, where Keycloak generates the seed and nothing
shared is stored. The OPI service account is what makes that reachable, because
after it the shared `admin` account is no longer a daily workhorse.

## How to use it

1. Open a project's detail page in the portal (admin/owner role). Under the
   **Keycloak** section, each realm with OTP shows a **Toon code** button. It
   fetches the code of that moment plus how long it stays valid, and a **Nieuwe
   code** button refetches once it runs out.
2. Log in at `{keycloak}/admin/` with the admin username + password; Keycloak
   prompts for the OTP code.

The seed itself is deliberately not shown. It never leaves the server, so what a
viewer of the page can obtain is one code with a lifetime of at most one period,
instead of the ability to generate codes forever. That also keeps the page free
of a third-party script: rendering a scannable QR of the `otpauth://` URI needed
one, and there is nothing left to scan.

When per-user seeds arrive (people registering their own OTP on a phone or in a
password manager), that flow does need the URI and a QR. Render the QR
server-side then (an inline SVG via `segno`), rather than reintroducing a CDN
script into a page that carries secrets.

## Enablement (single gate, default off)

The whole feature is gated by `KEYCLOAK_ENFORCE_ADMIN_OTP` (default `false`).

- **`false` (default):** no OTP is generated, stored, shown, or enforced
  anywhere. Deploying this code changes nothing, so it is safe to ship ahead of
  an actual rollout. This is covered by a test
  (`test_ensure_admin_otp_noop_when_disabled`).
- **`true`:** newly created realms get a shared OTP credential at creation, and
  existing realms are retrofitted (seed stored + shown in the portal, OTP
  enforced) the next time the project is processed. Note that processing happens
  not only on team-initiated deploys but also on automatic reprocesses (e.g. the
  nightly auto-tuner), so flipping the gate rolls OTP out gradually as projects
  are touched.

Set it in the cluster overlay configmap (it is a non-secret boolean). It is
enabled in the sandboxed-local overlay so the feature is exercised there;
production stays off until a deliberate rollout.

## Configuration

The seed is stored under each realm entry of the keycloak service config
(RC-5 relocated these from the old project-level `config.keycloak` to
`services/keycloak/config/realms`):

```yaml
services:
  - keycloak:
      config:
        realms:
          - host: https://keycloak.example.com
            realm: my-project-local
            username: my-project_local_admin
            password: |
              -----BEGIN AGE ENCRYPTED FILE-----
              ...
            totp_secret: |
              -----BEGIN AGE ENCRYPTED FILE-----
              ...
```

`totp_secret` is AGE-encrypted with the project public key (same as `password`)
and accepts a `plain:` prefix for manual values. It is a typed optional field on
the `KeycloakRealm` config model, so pre-OTP realms (no `totp_secret`) keep
validating.

## Scope and retrofit

Both paths below only run when `KEYCLOAK_ENFORCE_ADMIN_OTP` is `true`.

- **New realms** get the OTP credential at creation time
  (`KeycloakManager._setup_project_keycloak_realm`).
- **Existing realms** (no `totp_secret`) are retrofitted on the next
  deploy/reconcile by `KeycloakManager._ensure_admin_otp`, called in the
  already-exists branch next to `_ensure_realm_self_service` and
  `_create_additional_clients` (the spot where reconciliation of an *existing*
  realm happens - `create_realm()` only runs for a new realm). Keycloak 25 can
  only import OTP credentials at user-creation time, so the admin user is deleted
  and recreated - reusing its existing password (no rotation) - with the OTP
  credential, then realm-management roles are re-assigned. This runs at most once
  per realm; once `totp_secret` is stored, subsequent deploys short-circuit.
  - **Lockout note:** after retrofit, OTP is required at the admin's next login.
    The seed is visible in the portal, so admins must fetch it before logging in
    again. Partial failures self-heal on the next reconcile (the encrypted
    password stays in the project file).

## Security note

The seed is encrypted into the same project file as the password and decrypted
with the same project key, so anyone who can already decrypt the file gets both
factors. The value is defense-in-depth + compliance (MFA on admin accounts at
the login boundary, blocking password-only compromise), not protection against
an adversary who already holds the project file. See the "two OTPs" section
above for why that is acceptable here and where the person-bound factor lives.

## Key files

- `opi/utils/totp.py` - secret generation, credential representation, otpauth URI,
  and `totp_now` for the code of this moment (stdlib only; no new dependency).
- `opi/connectors/keycloak.py` - `create_user(..., totp_secret=...)` imports the
  OTP credential alongside the password.
- `opi/manager/keycloak_manager.py` - `_setup_project_keycloak_realm` (new realm)
  and `_ensure_admin_otp` (retrofit).
- `opi/web/router.py` (`keycloak_otp_code_web`) +
  `opi/services/catalog/keycloak/section-detail.html.j2` + `otp-code.html.j2` -
  the on-demand code endpoint and the button/fragment that show it.
- `opi/services/catalog/keycloak/config_model.py` + `keycloak.v1.0.json` -
  `totp_secret` field on the `KeycloakRealm` model + regenerated schema fragment.

## Dependencies

- Python: stdlib only (`secrets`, `base64`, `json`, `urllib.parse`, `hmac`,
  `hashlib`, `struct`, `time`).
- Frontend: none beyond htmx, which the portal already loads.
- Keycloak 25.x conditional-OTP browser flow (default).
