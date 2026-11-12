# Keycloak: alleen lokale accounts (local-only)

## Wat het is

`local-only` is een vierde realmblauwdruk naast `sso-only`, `sso-support` en `sso-support-otp`. Het realm federeert niet met SSO Rijk: er is geen identity provider en geen platform-client. Alleen lokale accounts uit het realm van het project kunnen inloggen.

Wat een gebruiker niet zelf kan:

- een account aanmaken (geen zelfregistratie);
- een wachtwoord herstellen of wijzigen;
- het eigen profiel beheren (`manage-account` staat uit).

Accounts worden aangeleverd door het project, of komen via een [uitnodiging](invites.md). De uitnodiging zet het wachtwoord via de admin-API, dus die werkt ondanks de uitgezette `UPDATE_PASSWORD`.

| Template | Inlogscherm | SSO Rijk | Lokaal account |
|---|---|---|---|
| `sso-only` | geen, meteen door naar SSO Rijk | ja | geen weg naar binnen |
| `sso-support` | keuze | ja | wachtwoord |
| `sso-support-otp` | keuze | ja | wachtwoord en OTP |
| `local-only` | alleen het wachtwoordformulier | nee | wachtwoord |

## Gebruik

In het portaal bij de configuratie van de dienst `keycloak`: "Alleen lokale accounts". Of in het projectbestand:

```yaml
services:
  - name: keycloak
    config:
      template: local-only
```

Bij een uitnodiging valt de keuze voor de inlogmethode weg: er is maar één antwoord, een lokaal account.

## Een bestaand project omzetten

Het verwerken van een realm voegt normaal alleen toe. Bij een wissel naar `local-only` verwijdert OPI daarom zelf de identity provider `rig-platform-oidc` uit het project-realm en de platform-client `<project>-<cluster>-platform` uit het platform-realm (`KeycloakManager._ensure_realm_identity_providers`). Terug naar een SSO-template maakt beide opnieuw aan.

Let op: wie eerder via SSO Rijk binnenkwam heeft geen wachtwoord. Die accounts blijven staan, maar hebben na de wissel geen weg naar binnen tot ze een lokaal account krijgen, bijvoorbeeld via een uitnodiging. OPI verwijdert geen gebruikers.

## Configuratie

- Blauwdruk: `operations-manager/python/opi/configs/keycloak/local-only.yaml`
- Regels per template: `opi/services/catalog/keycloak/templates.py` (`allows_local_accounts`, `allows_sso`)

## Afhankelijkheden

- [Invites](invites.md) om lokale accounts aan te maken.
- [Keycloak YAML Templates](keycloak-yaml-templates.md) voor hoe een blauwdruk verwerkt wordt.
