# Keycloak relay email sender — PROEF

**Dit is een meetopstelling, geen productiecode.** Hij hoort bij RC-158: de vraag of een
eigen `emailSender`-SPI op Keycloak 25.0.6 in PRODUCTIEMODUS wint van de
`smtpServer`-configuratie van een realm. De uitkomst van die meting staat in
`docs/rc158-emailsender-spi-meting.md`.

Alles in dit mapje draagt het woord `proef`: het artefact
(`keycloak-relay-email-sender-proef-0.1.0-proef.jar`), het provider-id (`zad-relay-proef`)
en het merkteken dat elk bericht meekrijgt (`X-ZAD-Email-Sender: zad-relay-proef/0.1.0`).
Dat merkteken is er met opzet: zonder is "de mail kwam aan" niet te onderscheiden van "de
standaardprovider deed het".

## Wat hij doet

`RelayEmailSenderProvider` implementeert `org.keycloak.email.EmailSenderProvider`. Keycloak
geeft hem een AL GERENDERD onderwerp en lichaam mee, dus de sjablonen blijven van Keycloak.
Wat de provider zelf doet:

- de relay, de inloggegevens en het afzenderadres uit de **omgeving van de pod** lezen;
- verbinden met de relay, authenticeren, het bericht aanbieden;
- het merkteken op het bericht zetten.

Wat hij **niet** doet: de `smtpServer` van de realm lezen. Die map komt binnen als
`config`, wordt geteld en gelogd, en verder genegeerd. Dat is de hele opzet — zolang de
bestemming niet uit de realm komt, is er geen veld dat een projectbeheerder kan omzetten
naar een luisteraar die hij zelf beheert.

## Omgevingsvariabelen

| Variabele | Betekenis | Standaard |
|---|---|---|
| `ZAD_MAIL_RELAY_HOST` | de relay binnen het cluster | verplicht |
| `ZAD_MAIL_RELAY_PORT` | submissiepoort | `587` |
| `ZAD_MAIL_RELAY_USERNAME` | het SASL-account | verplicht |
| `ZAD_MAIL_RELAY_PASSWORD` | het wachtwoord daarvan | verplicht |
| `ZAD_MAIL_RELAY_FROM` | het afzenderadres in `From:` | verplicht |
| `ZAD_MAIL_RELAY_STARTTLS` | STARTTLS eisen | `true` |
| `ZAD_MAIL_RELAY_TRUST_ALL` | elk certificaat accepteren (alleen sandbox: de sink draagt een zelfondertekend certificaat) | `false` |

Ontbreekt een verplichte waarde, dan faalt `create()` hard. Bewust: een halve configuratie
die stil terugvalt op iets anders is precies de klasse fout die deze meting moet uitsluiten.

## Bouwen en testen

```bash
task build-keycloak-relay-email-sender-proef   # mvn clean package
task test-keycloak-relay-email-sender-proef    # mvn test
```

Uitvoer: `keycloak-migration/relay-email-sender/target/keycloak-relay-email-sender-proef-0.1.0-proef.jar`

## Aanwijzen op Keycloak

De `emailSender`-SPI is **systeembreed**: er is er precies een voor de hele server en hij is
niet per realm in te stellen. De jar moet in `/opt/keycloak/providers/` staan voordat
Keycloak start, en de provider moet als standaardprovider worden aangewezen:

```
--spi-email-sender-provider=zad-relay-proef
```

`order()` wordt bewust NIET overschreven, zodat alleen die vlag de fabriek kan aanwijzen en
de meting de VLAG meet en niet een volgorde die toevallig wint.

Welke vlagvorm 25.0.6 werkelijk oppikt, of er een `kc.sh build` bij hoort, en wat er gebeurt
als een realm geen `smtpServer` heeft: dat is precies wat `docs/rc158-emailsender-spi-meting.md`
meet, met de uitvoer erbij.
