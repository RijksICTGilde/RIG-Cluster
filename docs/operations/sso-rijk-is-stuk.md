# SSO Rijk is stuk, wat nu

Niemand kan meer nieuw inloggen: niet op ZAD en niet op een projectapplicatie met SSO. De
uitwijk is de **storingsstand** van `rig-platform`, beschreven in
[features/sso-rijk-uitwijk.md](../../features/sso-rijk-uitwijk.md). Dit is de volgorde waarin je
hem gebruikt.

Zet de storingsstand niet als eerste aan. Hij lost een storing bij BZK op, maar niet een
storing van ons, en in dat tweede geval verbergt hij het probleem een dag.

## 1. Stel eerst vast hoe het stuk is

```bash
# De metadata-aanvraag die ONS eigen probleem is geweest: Keycloak haalt de metadata van
# sso-rijk op en krijgt een 403 van buiten het Rijksnetwerk.
kubectl --context odcn-rig-production -n rig-system logs deploy/keycloak --tail=200 \
  | grep -i "sso-rijk\|metadata\|403"
```

Drie uitkomsten, drie verschillende acties:

| Wat je ziet | Wat het is | Wat je doet |
|---|---|---|
| Een 403 of een time-out op de **metadata-URL** van `sso-rijk` | Ons eigen probleem: `useMetadataDescriptorUrl` laat Keycloak de metadata zelf ophalen, en dat pad loopt vanuit het cluster naar buiten | Repareer dat pad. De storingsstand is hier niet voor. |
| De SAML-redirect naar BZK komt niet aan, of BZK antwoordt met een foutpagina | Storing bij BZK | Door naar stap 2. |
| Inloggen werkt wel vanuit Citrix, maar niet van buiten | Zoals 22 oktober 2026: BZK weigert verzoeken van buiten het Rijksnetwerk | Door naar stap 2. De knop "Toch inloggen met SSO Rijk" blijft in de storingsstand staan, dus wie via Citrix werkt kan gewoon door. |

## 2. Zet de storingsstand aan

In ZAD: **Beheer > Platformacties**, knop "Storingsstand aanzetten". Het vraagt een verse
step-up met je OTP.

Kom je niet bij ZAD omdat er geen sessie meer loopt, gebruik dan de noodweg:
`https://<zad-host>/auth/login/beheer`, met je lokale beheerdersaccount in `rig-platform`
(wachtwoord plus OTP). Die route staat niet in het menu en hangt niet van SSO Rijk af.

Meld daarna aan de gebruikers: inloggen gaat nu met een code per e-mail, de code is 5 minuten
geldig, en alleen voor wie al een account heeft.

## 3. Zet hem weer uit zodra SSO Rijk werkt

Zelfde pagina, knop "Storingsstand uitzetten". Sessies die in de storingsstand zijn begonnen
blijven doorlopen: een browser-flow bepaalt alleen wat een NIEUWE login doet.

## Waar het mis kan gaan

- **De flow bestaat niet in Keycloak.** Dan draait er een Keycloak-image zonder de
  magic-link-authenticator. Bouw en pin het `keycloak-zad`-image volgens
  [features/keycloak-eigen-image.md](../../features/keycloak-eigen-image.md) en
  [uitrollen.md](uitrollen.md).
- **Er komt geen mail aan.** De keten loopt via de eigen verzender en de mailrelay; zie
  [features/keycloak-mail.md](../../features/keycloak-mail.md) voor hoe je die keten naloopt.
  Het inlogscherm zegt niets over een onbekend adres, dus "ik krijg niets" kan ook betekenen
  dat het adres geen account heeft of geen gekoppelde SSO Rijk-identiteit.
- **Je hebt geen lokaal beheerdersaccount.** Dat is een handstap per omgeving en geen
  onderdeel van een release; `features/sso-rijk-uitwijk.md` beschrijft hem. Richt hem in
  voordat je hem nodig hebt.
