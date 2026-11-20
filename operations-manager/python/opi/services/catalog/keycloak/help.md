# Keycloak Authentication

Je project krijgt een eigen afgeschermde omgeving (een realm) in Keycloak waarin je gebruikers inloggen. Dat kan met hun rijksaccount via SSO Rijk, en met lokale accounts die alleen in jouw realm bestaan, bijvoorbeeld voor mensen zonder rijksaccount.

## Wanneer gebruik je dit?

- Je applicatie moet weten wie de gebruiker is
- Je wilt inloggen met een rijksaccount (SSO Rijk) zonder dat zelf te bouwen
- Je wilt ook mensen buiten de Rijksoverheid toegang geven met een lokaal account
- Je wilt rollen gebruiken om te bepalen wat iemand mag

## Wat wordt er ingesteld?

Er wordt een realm aangemaakt met een client voor je applicatie. Je component krijgt de OIDC-gegevens als variabelen: **OIDC_DISCOVERY_URL**, **OIDC_CLIENT_ID**, **OIDC_CLIENT_SECRET**, **OIDC_URL** en **OIDC_REALM**. Je applicatie praat daarmee zelf met Keycloak.

In de configuratie kies je een template (welke inlogmanieren aanstaan), extra redirect-URI's, eigen realm-rollen, of toegang beperkt wordt tot gebruikers met een bepaalde rol, en een logout-pad voor je applicatie.

**Let op:** deze service vereist **Publiceren op het web**, dat automatisch wordt meegeselecteerd. Wil je niet zelf het inloggen in je applicatie bouwen, kijk dan naar de **Authorization Wall**.

## Wat je in het scherm instelt

- **Template** -- welke inlogmanieren aanstaan
- **Extra redirect URI's** -- adressen waar je applicatie na het inloggen op terugkomt, naast de deployment-URL's zelf
- **Toegangsbeperking** -- alleen gebruikers met een realm-rol of clientrol mogen de applicatie openen, met de foutmelding die de rest te zien krijgt
- **Account koppelen** -- wat er gebeurt als iemand via SSO Rijk inlogt terwijl er al een account met hetzelfde e-mailadres in de realm bestaat
- **Logout-pad** -- het pad waarmee je applicatie haar eigen sessie beëindigt, zodat Keycloak het bij uitloggen kan aanroepen
- **Extra Keycloak clients** -- voor externe applicaties of microservices die dezelfde realm delen
