# Keycloak verstuurt bevestigingsmail via een eigen verzender, en geen realm wijst nog een bestemming aan

Keycloak gaat e-mailadressen laten bevestigen. De post gaat via de mailrelay van het platform, met ÉÉN account voor heel Keycloak, en het wachtwoord daarvan reist niet langs de realm maar staat in de omgeving van de pod. Daarmee is er in geen enkele realm nog een bestemming die een tenant kan verzetten.

Dit vervangt RC-156, dat is gestopt. Die bouwde hetzelfde met `${vault.smtp-password}` in de `smtpServer` van elke realm, en strandde vier reviewrondes lang op dezelfde aanval via vier verschillende dragers van `manage-realm`: een projectbeheerder zet `smtpServer.host` op een luisteraar die hij beheert, Keycloak lost de vaultverwijzing pas op bij het VERSTUREN, en de luisteraar leest het wachtwoord van het hele platform. Elke reparatie sloot één dragersklasse; de volgende ronde vond er een die niemand had bedacht.

RC-158 heeft de uitweg gemeten. Die staat hieronder als vaststaand feit, niet als aanname.

## Wat RC-158 heeft gemeten, tegen een echte 25.0.6 in productiemodus

Het volledige verslag staat op de tak `meet-of-een-eigen-emailsender-spi-het-wint-van-de` in `docs/rc158-emailsender-spi-meting.md`, met de ruwe uitvoer ernaast. De vijf uitkomsten die dit plan dragen:

1. **Een eigen `EmailSenderProvider` wordt in productiemodus daadwerkelijk gebruikt.** De terugval uit [keycloak#14522](https://github.com/keycloak/keycloak/issues/14522) trad niet op.
2. **De vlagvorm is `--spi-email-sender-provider=<id>`** (of `KC_SPI_EMAIL_SENDER_PROVIDER`). De camelCase-vorm `--spi-emailSender-provider=` wordt STIL genegeerd en dan verstuurt de standaardprovider. Tegenover die val staat een cadeau: **een provider-id dat niet bestaat laat Keycloak weigeren te starten.** Een pod die opkomt heeft zijn provider dus aantoonbaar gevonden.
3. **De provider wordt ook bereikt op een realm zonder `smtpServer`**, op de admin-API, bij zelfregistratie en bij wachtwoord vergeten. Eén uitzondering: `IdpEmailVerificationAuthenticator` grendelt op `realm.getSmtpConfig().isEmpty()` vóór de SPI in beeld komt. Het gemeten minimum om die stap te laten werken is een `smtpServer` met precies één sleutel, `from`, zonder host.
4. **De omleidingsvector is weg.** Met de standaardprovider lukte de aanval nog gewoon en ving de luisteraar het wachtwoord in platte tekst. Met de SPI aan, dezelfde realm en dezelfde luisteraar, bevat diens volledige log één regel: zijn eigen startregel.
5. **Het faalt dicht**, zolang geen enkele realm een `host` draagt: een stille terugval geeft dan een zichtbare 500 en een `SEND_VERIFY_EMAIL_ERROR`, en er gaat niets uit.

## De opzet

```
Keycloak rendert het bericht (sjablonen en thema blijven van Keycloak)
   |
onze verzender  ->  negeert de smtpServer van de realm, verbindt met de relay
   |               op gegevens uit de omgeving van de POD
mailrelay (Stalwart)  ->  zet de From: vast, herschrijft de envelope, telt het budget
   |
upstream (sandbox: de Mailpit-sink)
```

Het wachtwoord volgt de regel die voor dit platform al geldt: **het wordt als geheim aangemaakt door de generatie, het bestaat dus al voordat Keycloak start, en OPI leest het alleen om de relay hetzelfde wachtwoord te laten dragen.** Geen vault, geen bestand, geen verwijzing in een realm.

`manage-realm` blijft bij de projectbeheerder. Er valt niets meer te verzetten dat ergens toe leidt.

## Wat er moet gebeuren

**1. De proefjar wordt een echte.** Beginpunt is `keycloak-migration/relay-email-sender/` van de RC-158-tak. Wat eraf moet en waarom, alle vier gemeld door de veiligheidsreview van die meting:

- `TRUST_ALL` zet certificaat- en hostnaamcontrole uit. Dat mag in een proef en niet hier.
- Loginjectie: de provider logt de host die hij negeert, en die waarde komt van de tenant. Ontsmetten of niet loggen.
- Het merkteken `X-ZAD-Email-Sender` met de proefwaarde: beslis of het blijft. Het is goedkoop en het maakt in de sink zichtbaar welke code een bericht verstuurde. Ik zou het houden en de waarde laten meebewegen met de versie.
- Naam en versie zijn nu `-proef`. Kies de echte, en bedenk dat de provider-id in de startvlag staat: hernoemen betekent dat de vlag mee moet, en een niet-bestaande id laat Keycloak niet starten.

**STARTTLS is hier een expliciete beslissing en geen detail.** Gemeten in de sandbox biedt de submission-listener van de relay GEEN STARTTLS aan, dus de proef draaide met platte AUTH binnen het cluster. Dat is precies wat elk project vandaag ook doet met zijn `SMTP_HOST`/`SMTP_PORT`, dus het is geen nieuwe blootstelling. Twee wegen: dit aanvaarden en opschrijven, of STARTTLS op die listener aanzetten, wat alle tenants tegelijk helpt en een eigen taak is. Voorstel: aanvaarden, opschrijven, en de listener als vervolgpunt noteren. Wat NIET mag is `TRUST_ALL` laten staan "omdat er toch geen TLS is": staat TLS er ooit, dan moet verificatie aan staan.

**2. De jar komt zonder netwerk in de pod, en dat hangt samen met de vlag.** De vlag `--spi-email-sender-provider=<id>` MOET gezet worden: dat is wat een stille terugval op de standaardprovider onmogelijk maakt. Gevolg is dat Keycloak niet start als de jar er niet is.

Vandaag halen twee initContainers hun jars bij elke podstart van github.com (`deployment.yaml`, `keycloak-theme-puller`). Die twee samen betekenen: een hapering bij GitHub legt het inloggen van het hele platform plat. Dat risico bestaat nu al voor het thema en de mapper, maar wij mogen het niet vergroten met een derde download waar bovendien een startvlag aan hangt.

Onze jar hoeft daarvoor NIET in een eigen image. Alle afhankelijkheden staan op `provided`, ook `jakarta.mail`, want Keycloak gebruikt die zelf al voor zijn eigen verzender. Er wordt niets geschaduwd, dus wat overblijft zijn drie klassen plus het registratiebestand: een handvol kilobytes, ruim binnen de 1 MiB van een ConfigMap.

Lever hem zo: een `configMapGenerator` met het jar-bestand, en de bestaande initContainer kopieert hem uit die mount naar de providersmap naast de twee andere jars. Geen netwerk, geen nieuwe image, geen registry. Bijkomend voordeel is dat de generator een hash van de inhoud in de naam zet, dus een nieuwe jar is vanzelf een rollout. Dat is precies het patroon dat `config.toml` van de relay gebruikt, en daar is twee keer op misgegaan toen het ontbrak.

Wat je daarmee wel binnenhaalt is een gebouwd artefact in git, dat kan gaan afwijken van de bron ernaast. Dek dat af met een CI-stap die de jar herbouwt en vergelijkt, of, als dat nu te veel is, met een taak in de Taskfile plus een regel in de README dat bron en jar in dezelfde commit horen. Noteer welke van de twee het is geworden.

Pin de vlagvorm met een toets. De camelCase-val is gemeten en geeft geen enkel signaal.

**3. Het geheim en het account.** Een template bij de bestaande generatie (`infrastructure/bootstrap/infrastructure/secrets/templates/`), met het wachtwoord via `@secret-gen:random:24`, in de namespace waar OPI en Keycloak allebei draaien. De Keycloak-deployment leest het als omgevingsvariabelen (`ZAD_MAIL_RELAY_HOST`, `_PORT`, `_USERNAME`, `_PASSWORD`, `_FROM`).

OPI leest datzelfde geheim en zorgt dat de relay het account draagt, via `MailManager.ensure_account` met `is_platform_account=True`. Dat pad bestaat en doet al `update_principal` op een bestaand account, dus een gewijzigd wachtwoord in git bereikt de relay vanzelf. Erbij: `MAIL_KEYCLOAK_ACCOUNT` en `MAIL_KEYCLOAK_MESSAGES_PER_DAY` in `opi/core/config.py`, en `_refuse_platform_account()` wordt een verzameling in plaats van één naam.

Werk de toelichting in `mail-relay-secret.yaml` bij: daar staat vandaag de redenering dat het ZAD-accountwachtwoord niet in de bootstrap kan omdat het account nog niet bestaat. Dat klopt voor `zad-platform` en niet voor dit account. De regel die de twee scheidt: **een geheim dat een derde partij nodig heeft voordat OPI iets gedaan heeft, komt uit de bootstrap; een geheim dat alleen OPI gebruikt, genereert OPI zelf.**

**Rotatievolgorde**: het geheim verandert in git, Keycloak krijgt het via zijn omgeving pas bij een herstart, en OPI zet het op de relay bij zijn volgende verwerking. Schrijf op wat daar de volgorde is en wat er in de tussentijd faalt.

**Uitdrukkelijk buiten deze taak, maar noteer het als vervolgpunt**: mooier zou zijn als de relay dit soort platformaccounts zelf declaratief kende, zodat OPI er helemaal niet aan te pas komt. Stalwart kan principals in de configuratie dragen via een directory van het type `memory`, maar `[session.auth]` wijst één directory aan en de projectaccounts leven in de interne directory in de database. Of dat per LISTENER te scheiden is (een tweede submission-listener voor platformcomponenten, met een eigen `directory`) is NIET gemeten. Dat is de meting die deze wens zou openen; doe hem niet in deze taak.

**4. Het minimale veld op elke realm.** `smtpServer` met precies `from`, zonder host, zodat `IdpEmailVerificationAuthenticator` niet op zichzelf grendelt. Geschreven door OPI bij create en bij reconcile, en met een toets die pint dat er nooit een `host` in terechtkomt. Die toets is niet decoratief: de eigenschap "het faalt dicht" geldt alleen zolang er nergens een bestemming staat.

**5. Wat er van RC-156 terugkomt.** Die tak is bewaard (`keycloak-verstuurt-bevestigingsmail-n-geprovisione`, tip `49d28787`) en het volgende is goedgekeurd werk dat losstaat van het credentialmodel. Neem het over in plaats van het opnieuw te bedenken:

- Het blueprint bepaalt de realm, op de createweg én op de replayweg, voor `registrationAllowed`, `loginWithEmailAllowed`, `resetPasswordAllowed` en `verifyEmail`. Let op de volgorde van landen: de drie eerste velden BESCHRIJVEN wat er vandaag gebeurt (alle drie false), alleen `verifyEmail` gaat om. De assertie blijft dat er aan een bestaande realm precies één veld verandert.
- `merge_user_variables()`, dat de gebruikersvariabelen onder de platformcontext legt en daarmee een hele cross-tenantklasse sluit.
- Het afzenderadres per account in het gegenereerde sieve-script, zodat inlogpost niet naamloos vanaf het kale adres vertrekt.
- Een nieuwe gebruiker is niet vanzelf geverifieerd: `create_user()` zet `emailVerified` nu op true zodra er een adres is, en dan verstuurt `verifyEmail` nooit iets. Die waarde volgt de realm.

**De runtimegrendel uit RC-156 moet HERZIEN worden en niet overgenomen.** Daar betekende "de realm kan mailen" dat er een `smtpServer` stond. Dat is nu geen maat meer, want elke realm draagt dat minimale veld. De maat wordt of het platform een relay heeft (`MAIL_RELAY_API_URL` gevuld). Dat is niet theoretisch: die schakelaar is op 21 augustus 2026 op productie echt uitgezet tijdens de crashlus van de relay, en een realm die dan verifieert zonder te kunnen mailen sluit gebruikers buiten.

**6. De Test-connection-knop is misleidend.** Gemeten: hij geeft 204 terwijl het bericht via de relay ging en de opgegeven host niets kreeg. Dat is geen gat maar het liegt tegen een tenantbeheerder. Beschrijf het in `features/`, en beslis of de knop verborgen kan worden.

**7. Productie.** De relay draait al op productie en `MAIL_RELAY_API_URL` staat ingevuld sinds 21 augustus. Wat erbij komt is het geheim voor het odcn-cluster, de jar en de vlag in de deployment, en het minimale veld op de bestaande realms. De echte toets is een verstuurde bevestigingsmail, één naar een `rijksoverheid.nl`-adres en één naar een adres daarbuiten.

**8. De teksten.** Een document in `features/` over de mailketen van Keycloak: de opzet, waarom er geen wachtwoord in een realm staat, wat een projectbeheerder wel en niet kan, de rotatievolgorde, en het gevolg voor de uitnodigingsweg uit punt 5 (wie een account krijgt, bevestigt voortaan eerst zijn adres).

## Valkuilen

**De vlag in de verkeerde vorm geeft geen enkel signaal.** camelCase wordt stil genegeerd en dan verstuurt de standaardprovider. Dat is dezelfde klasse val als de sleutels in de relayconfiguratie en de vault-resolver van RC-156.

**Fail-loud en beschikbaarheid grijpen in elkaar.** De vlag maakt een stille terugval onmogelijk en maakt tegelijk dat Keycloak niet start zonder de jar. Dat is de goede ruil, maar alleen als de jar niet aan een netwerkfetch hangt. Zie stap 2.

**Het is een INTERNE SPI.** Keycloak zegt dat zelf bij het opstarten (`KC-SERVICES0047 ... is implementing the internal SPI emailSender`). Er staat een upgrade naar 26 gepland; die vraagt een hertoets van dit hele mechanisme, en dat hoort in `features/keycloak-26-upgrade.md` genoteerd te worden zodra dit landt.

**`verifyEmail` mag pas aan als er post uit kan.** Zie de herziene grendel in stap 5.

**De blast radius van `verifyEmail` is klein maar niet nul.** SSO-gebruikers komen geverifieerd binnen (`trustEmail: True` op de identity providers) en bestaande lokale gebruikers zijn bij aanmaak op geverifieerd gezet. Geraakt worden nieuwe lokale gebruikers en iedereen die zijn adres wijzigt. Meet vóór het omzetten hoeveel bestaande gebruikers `emailVerified` op false hebben en zet dat getal per cluster in de PR.

**Bounces verdwijnen stil.** Mislukt een bezorging, dan weigert de upstream ook het envelope-adres en gooit de relay het bericht weg als dubbele bounce. "Geen foutmelding" is dus geen bewijs van aankomst; kijk in de sink of de postbus.

**De sandbox bewijst niets over egress.** Kind dwingt netwerkbeleid niet af. Voor de aanvalsproef is dat gunstig, voor bereikbaarheidsuitspraken waardeloos.

## Uitgangspunt dat nog niet bewezen is

Dat de upstream externe ontvangers accepteert. Op 21 augustus 2026 was dat niet zo (gemeten: `550 #5.1.0 Address rejected` op een gmail-adres). Deze taak gaat ervan uit dat het inmiddels geregeld is en toetst het bij stap 7. Blijkt het niet zo, dan is de keten af en blijft alleen `verifyEmail` uit; schrijf de gemeten weigering dan in het verslag.

## Wat hier buiten valt

- STARTTLS op de submission-listener van de relay. Aparte taak, helpt alle tenants.
- Declaratieve platformaccounts in de relay (het `memory`-directoryidee uit stap 3). Vraagt eerst een meting.
- Een eigen Keycloak-image met de provider erin gebakken. Niet nodig, zie stap 2, en alleen interessant als je ooit de ongeveer tien seconden augmentatie per start wilt wegnemen met `--optimized`.
- De twee bestaande jars die bij elke podstart van github.com komen. Die fragiliteit bestaat al en wordt hier niet vergroot, maar hij is een eigen taak waard.
- Een bounce-postbus, het spamfilter, de burst-limiter en de MTA-STS-lookup. Alle vier open in `plans/mail-vervolgpunten.md`.
- De vraag of `sso-support` echt zelfregistratie hoort te geven. Stap 5 legt de huidige toestand vast; wat het zou moeten zijn is een productbeslissing.

## Verifieerbaar

- **De aanvalsproef opnieuw, op de echte deployment**: een testrealm met `smtpServer.host` naar een luisteraar, een bevestigingsmail eroverheen, en de volledige log van die luisteraar in de PR. Leeg, op zijn eigen startregel na.
- De controle erbij: met de vlag in de camelCase-vorm lukt de aanval wél. Zonder die tegenproef zegt een lege log niets.
- Een bevestigingsmail komt in de Mailpit-sink aan met het afzenderadres en de naam van het Keycloak-account, terwijl projectpost zijn eigen adres houdt.
- Een pod met een niet-bestaand provider-id start NIET. Dat is de canarie en die hoort in een toets.
- Een bestaande realm verschilt na stap 5 in precies één veld met de toestand ervoor, en dat veld is `verifyEmail`.
- Geen enkele realm draagt een `host` in `smtpServer`, gepind met een toets.
- Een nieuwe gebruiker in een verifiërende realm komt binnen met `emailVerified: false` en is na het aanklikken van de bevestiging binnen. Een SSO-login levert geen bevestigingsmail op.
- Met `MAIL_RELAY_API_URL` leeg krijgt een realm geen `verifyEmail`, en dat is met een toets vastgelegd.
- Het aantal bestaande gebruikers met `emailVerified: false` staat in de PR, per cluster.
- Op productie: een bevestigingsmail naar een `rijksoverheid.nl`-adres komt aan, en een naar een adres daarbuiten komt aan of levert een gemeten SMTP-antwoord op dat in het verslag staat.
- `uv run pytest tests/ -q` groen, plus `ruff check .`, `ruff format .` en `pyright`. De Java-kant met `mvn package` groen.
