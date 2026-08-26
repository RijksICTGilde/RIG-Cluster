# Keycloak relay email sender

De `EmailSenderProvider` waarmee Keycloak zijn post via de mailrelay van het platform
verstuurt. Hij leest de relay uit de **omgeving van de pod** en negeert de `smtpServer` van
de realm volledig.

Dat is de hele opzet, en het is een veiligheidseigenschap en geen implementatiedetail:
zolang de bestemming niet uit de realm komt, bestaat er in geen enkele realm een veld dat
een projectbeheerder kan omzetten naar een luisteraar die hij zelf beheert. Met de
standaardprovider kon dat wel, en dan komt het relaywachtwoord van het hele platform in
platte tekst bij die luisteraar terecht. Gemeten met de aanval erbij in
`docs/rc158-emailsender-spi-meting.md`.

De keten eromheen staat in `features/keycloak-mail.md`.

## Wat hij doet

Keycloak rendert het bericht zelf en geeft een AL GERENDERD onderwerp en lichaam mee, dus
de sjablonen en het thema blijven van Keycloak. Wat deze provider doet:

- de relay, de inloggegevens en het afzenderadres uit de **omgeving van de pod** lezen;
- verbinden met de relay, authenticeren, het bericht aanbieden;
- het merkteken `X-ZAD-Email-Sender: zad-relay/<versie>` op het bericht zetten.

Wat hij **niet** doet: de `smtpServer` van de realm lezen. Die map komt binnen als `config`
en wordt alleen geteld. Er komt geen enkele WAARDE uit die map in de log terecht - het is
tenantinvoer, en een host met nieuwe regels erin zou anders zijn eigen logregels schrijven.

## Omgevingsvariabelen

| Variabele | Betekenis | Standaard |
|---|---|---|
| `ZAD_MAIL_RELAY_HOST` | de relay binnen het cluster | verplicht |
| `ZAD_MAIL_RELAY_PORT` | submissiepoort | `587` |
| `ZAD_MAIL_RELAY_USERNAME` | het SASL-account (`zad-keycloak`) | verplicht |
| `ZAD_MAIL_RELAY_PASSWORD` | het wachtwoord daarvan | verplicht |
| `ZAD_MAIL_RELAY_FROM` | het afzenderadres in `From:` | verplicht |
| `ZAD_MAIL_RELAY_STARTTLS` | STARTTLS eisen | `true` |

Ontbreekt een verplichte waarde, dan logt het opstarten dat en gooit de eerste verzending.
Bewust in die volgorde: deze fabriek zit in het opstartpad van Keycloak zelf, dus hier
gooien zou een ontbrekende mailvariabele het inloggen van het hele platform laten platleggen.
Nu faalt alleen de mail, luid en zichtbaar.

**Er is geen knop die certificaatcontrole uitzet.** De proefversie had er een, omdat de
sink in de sandbox een zelfondertekend certificaat draagt. Zo'n schakelaar hangt aan een
omgevingsvariabele en reist dus mee naar een cluster waar TLS wel iets betekent.

**STARTTLS staat aan tenzij de pod hem uitzet.** De submission-listener van de relay biedt
in de sandbox geen STARTTLS aan (met `EHLO` nagemeten in RC-158), dus daar staat
`ZAD_MAIL_RELAY_STARTTLS: "false"` letterlijk in het manifest. Dat is dezelfde blootstelling
als elk project vandaag heeft met zijn `SMTP_HOST`/`SMTP_PORT` - platte AUTH binnen het
cluster - en geen nieuwe. STARTTLS op die listener aanzetten helpt alle tenants tegelijk en
is een eigen taak; zie `plans/mail-vervolgpunten.md`.

## Bouwen en testen

```bash
task build-keycloak-relay-email-sender   # mvn clean package + de jar naast het manifest zetten
task test-keycloak-relay-email-sender    # mvn test
task check-keycloak-relay-email-sender   # herbouwen en byte voor byte vergelijken
```

### De jar staat in git, en waarom

De pod krijgt deze jar uit een **ConfigMap**, niet van het netwerk:

```
infrastructure/bootstrap/infrastructure/keycloak/controller/base/providers/keycloak-relay-email-sender-1.0.0.jar
```

De twee andere jars in die pod (het thema en de SAML-mapper) worden bij elke podstart van
github.com gehaald. Voor deze mag dat niet: de startvlag `--spi-email-sender-provider`
zorgt dat Keycloak WEIGERT TE STARTEN als de provider er niet is - dat is precies wat een
stille terugval op de standaardprovider onmogelijk maakt - en dan zou een hapering bij
GitHub het inloggen van het hele platform platleggen.

De prijs is een gebouwd artefact in git, dat kan gaan afwijken van de bron ernaast. Dat is
afgedekt met **een CI-stap die de jar herbouwt en byte voor byte vergelijkt**
(`task check-keycloak-relay-email-sender`, job `keycloak-relay-email-sender` in
`.github/workflows/ci.yml`). Dat kan omdat de bouw reproduceerbaar is
(`project.build.outputTimestamp` in de `pom.xml`) en met dezelfde JDK-major draait; CI pint
Temurin 21, want het manifest van de jar noemt `Build-Jdk-Spec`.

**Bron en jar horen in dezelfde commit.**

### Een versiesprong raakt vier plekken

De versie zit in de bestandsnaam van de jar, dus hij staat op meer dan één plek:

1. `<version>` in `pom.xml`;
2. `RelayMailConfig.VERSION` (`RelayVersieTest` houdt die twee gelijk);
3. de bestandsnaam onder `.../keycloak/controller/base/providers/`, en de
   `configMapGenerator` die hem noemt;
4. het `cp`-commando in de initContainer van de Keycloak-deployment.

## Aanwijzen op Keycloak

De `emailSender`-SPI is **systeembreed**: er is er precies een voor de hele server en hij is
niet per realm in te stellen. De jar moet in `/opt/keycloak/providers/` staan voordat
Keycloak start, en de provider moet als standaardprovider worden aangewezen:

```
--spi-email-sender-provider=zad-relay
```

**Let op de vorm.** `--spi-emailSender-provider=` (camelCase) wordt STIL genegeerd en dan
verstuurt de standaardprovider - geen waarschuwing, geen enkel signaal. `RelayEmailSenderProviderFactoryTest`
pint de vorm daarom op `EmailSenderSpi.getName()` uit Keycloak zelf. Tegenover die val staat
een cadeau: een provider-id dat niet bestaat laat Keycloak **weigeren te starten**, dus een
pod die opkomt heeft zijn provider aantoonbaar gevonden.

`order()` wordt bewust NIET overschreven, zodat alleen die vlag de fabriek kan aanwijzen en
niet een volgorde die toevallig wint.

## Het is een INTERNE SPI

Keycloak zegt dat zelf bij het opstarten:

```
KC-SERVICES0047: zad-relay (nl.minbzk.rig.keycloak.email.RelayEmailSenderProviderFactory)
  is implementing the internal SPI emailSender.
```

Een interne SPI mag zonder aankondiging veranderen. De geplande upgrade naar Keycloak 26
vraagt dus een hertoets van dit hele mechanisme; dat staat in `features/keycloak-26-upgrade.md`.
