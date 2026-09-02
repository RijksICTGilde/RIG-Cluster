# Keycloak verstuurt bevestigingsmail: één geprovisioneerd account voor alle realms, en het blueprint gaat eindelijk over de realm

Nu de mailrelay draait kan Keycloak e-mailadressen laten bevestigen. Dat vraagt drie dingen die er geen van drieën zijn: een mailaccount dat Keycloak mag gebruiken, een `smtpServer` op elke realm, en een blueprint dat werkelijk bepaalt wat er in die realm staat. Deze taak levert ze alle drie, met één regel die de opzet stuurt: **de mailconfiguratie van een realm is geprovisioneerd, niet instelbaar, en het wachtwoord staat er niet in.**

**Productie hoort erbij en `verifyEmail` gaat aan.** Uitgangspunt van deze taak is dat de upstream externe ontvangers accepteert. Dat was op 21 augustus 2026 nog niet zo (gemeten: een gmail-adres kreeg `550 #5.1.0 Address rejected`), en het is een uitgangspunt dat je bij de eerste testmail naar buiten meteen bevestigd of weerlegd ziet. Blijkt het niet te kloppen, dan is dat geen reden om de bouw af te breken maar wel om `verifyEmail` als laatste stap uit te laten en dat in het verslag te zetten.

## Wat er nu gebeurt, gemeten op 24 augustus 2026

Zes vondsten. De tweede is de gevaarlijkste, de zesde bepaalt of deze hele taak iets doet.

**1. Keycloak heeft geen mailconfiguratie, nergens.** `smtpServer` komt in de hele codebase niet voor. Er is dus geen realm die post kan versturen, en `verifyEmail` staat hardgecodeerd op `False` in `opi/connectors/keycloak.py:184`.

**2. Het blueprint bepaalt de realm niet, en dat dichten zet zelfregistratie aan.** `KeycloakYamlHandler._apply_realm_self_service()` (`opi/handlers/keycloak_yaml_handler.py:418`) leest precies twee sleutels uit het blueprint: `disabledRequiredActions` en `removeFromDefaultRoles`. Alle andere realmvelden komen uit de hardgecodeerde `realm_data` in `create_realm()` (`opi/connectors/keycloak.py:174-194`). `sso-support.yaml:37-41` zegt `registrationAllowed: true`, `loginWithEmailAllowed: true`, `resetPasswordAllowed: true` en `verifyEmail: true`, en geen van die vier gebeurt. `algoritmeregister.yaml:68-72` zegt hetzelfde.

Dat is niet alleen een gat, het is een gat waar de UI overheen liegt: `KeycloakTemplateOptionsProvider` (`opi/forms/visualizers/providers.py:461-467`) beschrijft de twee blueprints aan de gebruiker juist met het verschil in deze velden, "zodat iemand die dit kiest weet dat hij ook lokale accounts aanzet". Wie `sso-support` kiest krijgt vandaag geen zelfregistratie, geen wachtwoordherstel en geen inloggen op e-mailadres.

**Daarom is dit geen fix die je zomaar landt.** Zodra het blueprint wél gezaghebbend is, draait de eerstvolgende verwerking van elk bestaand `sso-support`-project zelfregistratie AAN op een productierealm. Deze taak zet alleen `verifyEmail` aan; de andere drie blijven beschrijven wat er vandaag werkelijk gebeurt. Zie stap 1.

**3. Het platformaccount van de relay is er wel, en de weg ernaartoe klopt al.** `MailManager.ensure_platform_account()` (`opi/manager/mail_manager.py:248`) maakt via de management-API een gewoon account aan. `ensure_account()` (`:99`) doet bij een bestaand account `update_principal(password=...)`, dus het is geen "aanmaken als het niet bestaat" maar "zorgen dat de relay dit wachtwoord draagt". Dat is precies wat een wachtwoord uit de bootstrap nodig heeft en het hoeft niet gebouwd te worden.

**4. Het netwerkpad ligt er, en de relay draait al op productie.** Het ingress-beleid van de relay laat `rig-system` (sandbox) en `rig-prd-operations` (productie) toe op poort 2525, en Keycloak draait in exact die namespaces. `MAIL_RELAY_API_URL` staat sinds 21 augustus ingevuld in de odcn-overlay van OPI en `argocd-application-ron-infrastructure.yaml` staat in de bootstrap-kustomization. Er hoeft dus geen relay te worden aangezet en er hoeft geen netwerkregel bij.

**5. SSO-gebruikers komen al geverifieerd binnen.** De identity providers worden aangemaakt met `trustEmail: True` (`opi/connectors/keycloak.py:1149` en `:1317`). Keycloak zet `emailVerified` dan op true zodra iemand via SSO Rijk binnenkomt. `verifyEmail` aanzetten levert voor die gebruikers dus GEEN bevestigingsmail en GEEN blokkade. Dat haalt de angel uit de grootste vrees bij deze wijziging: er komt geen stortvloed en er wordt niemand buitengesloten die via SSO werkt.

**6. Lokale gebruikers worden aangemaakt als al geverifieerd, en dat maakt de hele functie loos.** `create_user()` zet `emailVerified: False`, maar meteen daarna `emailVerified = True` zodra er een e-mailadres is meegegeven (`opi/connectors/keycloak.py:3605-3617`). Elke via de invite-weg aangemaakte gebruiker is dus vooraf geverifieerd zonder dat er ooit iets bevestigd is.

**Zonder hier iets aan te doen, verstuurt `verifyEmail` nooit een bericht.** SSO-gebruikers zijn geverifieerd via `trustEmail`, lokale gebruikers zijn het bij aanmaak, en dan blijft alleen het WIJZIGEN van een adres over als aanleiding. Dat is bijna nooit, en dan zou deze taak een mailketen opleveren die in de praktijk stil blijft. Zie stap 5; dit is de beslissing waar de waarde van het geheel aan hangt.

## Twee dingen die extern zijn nagezocht, zodat de uitvoerder dat niet nog eens doet

**Keycloak kent geen serverbrede SMTP.** Elke realm draagt zijn eigen `smtpServer`-map en er is geen instelling die over alle realms heen geldt ([Keycloak server administration guide](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/22.0/html/server_administration_guide/configuring_realms)). "Globaal" is hier dus geen Keycloak-functie maar een provisioningregel: OPI schrijft dezelfde map naar elke realm die het beheert, en de tenant kiest niets.

**Het wachtwoord hoeft niet in de realm te staan.** Keycloak lost `${vault.smtp-password}` in het wachtwoordveld op via de Vault SPI. Normaal plakt hij de realmnaam voor de sleutel om lekken tussen realms te voorkomen; de resolver `KEY_ONLY` negeert de realmnaam, zodat alle realms hetzelfde bestand lezen ([vault administration](https://docs.redhat.com/en/documentation/red_hat_build_of_keycloak/26.0/html/server_administration_guide/vault-administration)). Eén account voor heel Keycloak en één bestand voor alle realms vallen daarmee samen. Een realmbeheerder met `manage-realm` ziet letterlijk die tekenreeks en verder niets.

Dat lost meteen de reconcile-vraag op: OPI kan het veld gewoon uitlezen en vergelijken, want wat er staat is de verwijzing en niet het geheim. Of Keycloak 25.0.6 het wachtwoordveld maskeert bij uitlezen (`StripSecretsUtils`) doet er voor de driftdetectie dan niet meer toe.

## De opzet

Eén account voor heel Keycloak, `zad-keycloak`, naast het bestaande `zad-platform` van OPI zelf.

Het wachtwoord komt uit de BOOTSTRAP, niet uit OPI. Dat is de omkering ten opzichte van `zad-platform` en de regel die de twee uit elkaar houdt is deze: **een geheim dat een derde partij nodig heeft, wordt in de bootstrap gegenereerd; een geheim dat alleen OPI gebruikt, genereert OPI zelf.** Keycloak kent OPI niet en hoort niet op OPI te wachten, dus het wachtwoord moet kunnen bestaan zonder dat OPI ooit gedraaid heeft. `zad-platform` wordt door niemand anders gelezen en blijft daarom zoals het is.

Die regel moet worden opgeschreven in `infrastructure/bootstrap/infrastructure/secrets/templates/mail-relay-secret.yaml`, want daar staat vandaag de omgekeerde redenering ("er staat hier dus geen wachtwoord van het ZAD-account, dat bestaat nog niet als dit bestand wordt gegenereerd"). Die tekst klopt voor `zad-platform` en verbiedt wat hier gebouwd wordt.

De keten wordt dan: de generatie zet een willekeurig wachtwoord in een Secret, Keycloak mount dat als bestand voor de vault, OPI leest hetzelfde Secret en zorgt dat de relay dat wachtwoord draagt, en elke realm krijgt een `smtpServer` waarin alleen de verwijzing staat.

## Wat er moet gebeuren

De volgorde is niet vrij: `verifyEmail` (stap 1) mag pas op een realm landen als die realm post kan versturen (stap 6). Bouw in de volgorde hieronder en zet stap 1 als LAATSTE aan, of laat stap 1 de waarde uit een blueprint lezen die pas aan het eind omgaat. Een realm die verifieert en niet kan mailen, sluit gebruikers buiten.

**1. Het blueprint gezaghebbend maken, en alleen `verifyEmail` omzetten.** Twee helften, en de volgorde is de hele veiligheid van deze stap.

Eerst: zet in elk blueprint de waarden die de LIVE toestand van vandaag beschrijven, dus `registrationAllowed: false`, `loginWithEmailAllowed: false` en `resetPasswordAllowed: false` waar die vandaag hardgecodeerd zo uitpakken. Schrijf er per blueprint bij waarom, want in `sso-support.yaml` en `algoritmeregister.yaml` betekent dit dat er iets ANDERS komt te staan dan er nu staat, en dat is expres: er stond een belofte, er komt een beschrijving. `verifyEmail` blijft in die twee blueprints op `true` staan en gaat dus wél om.

Dan pas: `create_realm()` en `_apply_realm_self_service()` laten lezen wat het blueprint zegt over deze vier velden, op de createweg én op de replayweg (de 409-tak in `create_realm()` past nu alleen `session_settings` en `event_settings` toe).

De assertie van deze stap is dus scherp: aan een bestaande realm verandert precies EEN veld, en dat is `verifyEmail`. Kun je dat niet aantonen, dan is de stap niet af.

Wat hierdoor zichtbaar wordt en NIET in deze taak wordt opgelost: de UI beschrijft de blueprints nog steeds als "sso-support geeft lokale accounts en wachtwoordherstel" (`providers.py:461-467`). Die tekst klopt dan aantoonbaar niet meer met het blueprint. Noteer dat in het verslag en laat de tekst staan; of `sso-support` echt zelfregistratie hoort te geven is een productbeslissing en geen onderdeel hiervan.

**2. Het geheim in de bootstrap.** Nieuw template `keycloak-mail-secret.yaml` in `infrastructure/bootstrap/infrastructure/secrets/templates/`, met een regel erbij in de `kustomization.yaml` ernaast, in de namespace waar OPI en Keycloak allebei draaien (`RIG_NAMESPACE`, dus één rendering, anders dan bij `mail-relay-secret.yaml` dat twee namespaces bedient).

De sleutel heet `smtp-password` en niet `SMTP_PASSWORD`, want deze sleutel wordt een BESTANDSNAAM in de vault-map en moet gelijk zijn aan de sleutel in `${vault.smtp-password}`. Zet dat als waarschuwing in het bestand: hernoemt iemand hem later, dan breekt de realmverwijzing zonder dat er bij het opstarten iets misgaat, want de vault lost pas op als er een bericht verstuurd wordt. Het wachtwoord zelf via `@secret-gen:random:24`, zoals de andere templates.

Genereer het voor BEIDE clusters: sandbox met `security/sandbox-key.txt` en productie met `security/key.txt`.

**3. Het vault-pad in de Keycloak-deployment.** De file-vault aanzetten met de resolver `KEY_ONLY`, en de Secret uit stap 2 mounten als MAP op de vault-map, in de basis zodat elk cluster hem krijgt.

Geen `subPath`: een `subPath`-mount krijgt bijgewerkte Secret-inhoud nooit te zien, en dat is dezelfde les die de configmap van de relay twee keer heeft opgeleverd. Als map ververst de kubelet binnen ongeveer een minuut en leest de vault het bestand op het moment dat hij het nodig heeft, dus een rotatie vraagt geen herstart.

**MEET de vlagvorm tegen 25.0.6** (`infrastructure/bootstrap/infrastructure/keycloak/controller/base/deployment.yaml:53`). De documentatie van 26.x schrijft `--spi-vault--file--key-resolvers` met dubbele streepjes, 25.0.6 kent de oude vorm, en een SPI-optie die Keycloak niet herkent wordt genegeerd. Hij valt dan stil terug op de realm-gebonden resolver, en dan werkt het voor precies nul realms. Schrijf in de PR welke vorm het is geworden en hoe je hebt vastgesteld dat hij is opgepikt.

Let ook op een afsluitende nieuwe regel in het bestand: die is onderdeel van het wachtwoord. Met `stringData` komt hij er niet bij, maar dit is het soort ding dat als "authenticatie mislukt" terugkomt.

**4. Het account op de relay.** OPI leest het Secret uit stap 2 en roept `ensure_account` aan voor `zad-keycloak`, met `is_platform_account=True`. Erbij: `MAIL_KEYCLOAK_ACCOUNT` en `MAIL_KEYCLOAK_MESSAGES_PER_DAY` in `opi/core/config.py` naast de twee bestaande `MAIL_PLATFORM_`-instellingen, met 2000 als dagbudget.

`_refuse_platform_account()` (`opi/manager/mail_manager.py:69`) kent één naam en wordt een verzameling, anders bewaakt de projectweg straks één van de twee platformaccounts. Structureel kan een project er niet bij (projectaccounts dragen het voorvoegsel `project-`), maar die guard bestaat juist omdat accountnamen ook uit een projectbestand kunnen komen dat ouder of gerepareerd is.

Anders dan bij `zad-platform` genereert OPI hier NIETS en schrijft het NIETS terug: het geheim komt uit git en OPI is de reconciler, niet de bron. Ontbreekt het Secret, dan is dat een cluster zonder Keycloak-mail en geen fout die de start moet tegenhouden; log het en ga door, zoals de bestaande weg doet als er geen relay is ingesteld.

**5. Een nieuwe gebruiker is niet vanzelf geverifieerd.** Dit is de stap die bepaalt of de rest iets doet, en hij hangt aan vondst 6.

`create_user()` zet `emailVerified` op true zodra er een adres is meegegeven. Zolang dat zo blijft, verstuurt `verifyEmail` nooit iets. Het voorstel is: die waarde volgt de realm. Verifieert de realm (`verifyEmail: true`), dan wordt een nieuwe gebruiker aangemaakt met `emailVerified: false` en bevestigt hij zijn adres bij de eerste login. Verifieert de realm niet, dan blijft het gedrag zoals het is.

Gevolg dat je expliciet moet opschrijven in `features/`: iemand die via de invite-weg een account krijgt, moet voortaan eerst een bevestigingsmail aanklikken voordat hij binnen is. Dat is het punt van de functie en geen bijwerking, maar het verandert wel wat een projectbeheerder ziet gebeuren.

Raak `trustEmail` op de identity providers NIET aan. SSO-gebruikers horen geverifieerd binnen te komen; hun adres komt uit de bron en niet uit een formulier.

**6. `smtpServer` op elke door OPI beheerde realm.** Host, poort, gebruikersnaam, `from`, `auth`, en als wachtwoord letterlijk `${vault.smtp-password}`. Bij create én bij reconcile, zodat een handmatige wijziging vanzelf terugdraait.

De `from` die Keycloak meestuurt wordt door de relay overschreven, dus die is beschrijvend en niet sturend; zet er de waarde neer die er werkelijk uit komt, anders leest de volgende beheerder hem als de waarheid. `replyTo` raakt de relay NIET aan en is dus wel echt: dat is de plek waar een realm iets eigens kan hebben.

Bij een rotatie moeten alle realms in één veegactie mee. Stapsgewijs bijwerken laat een deel achter met een verwijzing die klopt maar een relay die het oude wachtwoord draagt.

**MEET of 25.0.6 het wachtwoordveld maskeert bij uitlezen**, en of een partial export van een realm het meeneemt. Met de verwijzing erin lekt er niets, maar dit bepaalt hoe erg het is als iemand ooit alsnog een letterlijk wachtwoord in een realm zet.

**7. Het afzenderadres per account.** `zad-keycloak` begint niet met `project-`, dus de relay geeft het vandaag het kale `noreply-rijksapp@rijksoverheid.nl` zonder naam, hetzelfde adres als `zad-platform`. Login-post zou dan naamloos vertrekken en een bounce zou niet te onderscheiden zijn van de post van de portal.

De weg ligt er: de weergavenaam is al per account, via de sleutels `zad.afzender.naam.<account>` en het sieve-script `zad-afzenders` dat OPI daaruit rendert (`opi/connectors/mail.py:62-100` en `render_sender_table`). Daar komt een tweede sleutelreeks voor het ADRES bij, met een tweede global in hetzelfde script; de afleiding uit de accountnaam blijft de terugval voor elk account dat er geen heeft.

De envelope in `[session.mail] rewrite` blijft de afgeleide waarde houden en dat is een bewuste keuze: DMARC-uitlijning kijkt naar het DOMEIN en niet naar het lokale deel, dus de uitlijning blijft heel, en het plusdeel blijft de bounce dragen. Schrijf dat op bij de regel, want het is precies het soort verschil waarvan de volgende lezer denkt dat het een vergissing is.

De validatie van een adres is een andere dan die van een naam: `_controleer_naam` weigert onder meer de `@` die een adres juist nodig heeft. Er komt dus een eigen controle, met een harde lijst van toegestane domeinen. Een instelbaar afzenderadres is een spoofingknop, en de enige reden dat dit hier mag is dat alleen het platform hem bedient.

**8. Productie.** De onderdelen komen langs ArgoCD (`infrastructure/`), dus er is geen handmatige bootstrapstap nodig; wat er wel moet is het geheim genereren voor het odcn-cluster (stap 2) en verifiëren dat de vault-mount en de `smtpServer` op de productierealms staan.

De echte toets is een verstuurde bevestigingsmail: één naar een `rijksoverheid.nl`-adres en één naar een adres buiten de Rijksoverheid. De tweede is meteen de proef op het uitgangspunt van deze taak. Komt daar een `550` op de RCPT TO terug, zet dan `verifyEmail` in de blueprints terug op `false`, laat de rest staan, en schrijf de gemeten weigering in het verslag: dan is de keten af en wacht alleen de schakelaar op het mailteam.

**9. De teksten.** `features/send-email.md` beschrijft vandaag alleen de dienst voor projecten; er komt een paragraaf bij over het platformaccount van Keycloak, of een eigen document in `features/` als het te veel wordt. Daarin ook het gevolg uit stap 5 voor de invite-weg. En de toelichting in `mail-relay-secret.yaml` uit "De opzet" hierboven.

## Valkuilen

**Een realm die verifieert en niet kan mailen, sluit mensen buiten.** Daarom staat `verifyEmail` als laatste aan en niet als eerste. Binnen deze taak betekent dat: bouw stap 2 tot en met 8, en laat het blueprint pas als sluitstuk omgaan.

**De blast radius van `verifyEmail` is klein, maar niet nul.** SSO-gebruikers komen geverifieerd binnen (`trustEmail: True`) en bestaande lokale gebruikers zijn bij aanmaak op geverifieerd gezet. Geraakt worden dus: nieuwe lokale gebruikers na stap 5, en iedereen die zijn e-mailadres wijzigt. Meet vóór het omzetten hoeveel bestaande gebruikers `emailVerified` op false hebben staan en zet dat getal in de PR; is het niet nul, dan zijn dat mensen die bij hun volgende login een bevestiging moeten doen.

**De rotatievolgorde is niet stuurbaar.** Verandert het geheim in git, dan ziet Keycloak het nieuwe bestand zodra de kubelet de mount ververst, terwijl OPI het pas bij zijn volgende verwerking op de relay zet. Daartussen faalt de authenticatie. Dat is te overzien, maar het hoort in de documentatie te staan als bekend gedrag in plaats van ontdekt te worden tijdens een rotatie.

**Alle realms delen één dagbudget.** Een resetlus in één project kan de login-post van alle andere projecten stilleggen. Dat is de prijs van één account en die betalen we bewust; noteer in het verslag of er een melding op moet, maar bouw die hier niet.

**Per-realm branding kan niet.** Eén account is één afzender, dus login-post van project A en B is voor de ontvanger niet te onderscheiden. `replyTo` is het enige dat per realm echt aankomt. Wie daar later anders over denkt, komt uit bij een account per realm, en dat is een andere taak.

**`manage-realm` blijft zoals het is.** Een projectbeheerder mag zijn realmsettings beheren; de eis is alleen dat hij het wachtwoord niet kan lezen. Wijzigt hij host of gebruikersnaam, dan zet stap 6 het terug. Haal geen rollen weg.

**Bounces verdwijnen vandaag stil.** Mislukt een bezorging, dan adresseert de relay de DSN aan het envelope-adres, weigert de upstream dat ook, en gooit de relay het bericht weg als dubbele bounce. Een bevestigingsmail die niet aankomt, is dus onzichtbaar voor iedereen. Bouw hier geen postbus (dat is een apart openstaand punt), maar houd er rekening mee bij het toetsen: "geen foutmelding" is geen bewijs van aankomst. Kijk in de sink of in de postbus, niet in de logs.

**De sandbox bewijst niets over egress.** Kind dwingt netwerkbeleid niet af, en een geblokkeerde uitgang levert hier geen foutmelding op maar een wachttijd. Dat is twee keer eerder misgegaan bij deze relay. Alles wat over bereikbaarheid gaat, moet op ODCN gemeten worden of als onbewezen worden opgeschreven.

## Wat hier buiten valt

- **De vraag of `sso-support` zelfregistratie hoort te geven.** Stap 1 legt de huidige toestand vast voor drie van de vier velden; wat het zou moeten zijn is een productbeslissing.
- **Een bounce-postbus, het spamfilter, de burst-limiter en de MTA-STS-lookup.** Vier openstaande punten bij de relay, alle vier in `plans/mail-vervolgpunten.md`. Kom je een gegeven tegen dat er iets over zegt, schrijf het op in het verslag en verander niets.
- **`zad-platform` verhuizen naar het bootstrapmodel.** Kan, hoeft niet, en de regel in "De opzet" legt uit waarom niet.
- **De e-mailsjablonen.** `emailTheme` staat niet ingesteld en of de nl-design-system-jar er een meebrengt is niet nagekeken. Meet het, zet het in het verslag, en zet het alleen om als het er evident is; anders sturen we de standaardteksten en is dat een eigen taak.

## Verifieerbaar

- Een bestaande `sso-support`-realm op de sandbox verschilt na stap 1 en na een tweede verwerking in precies EEN veld met de toestand ervoor, en dat veld is `verifyEmail`. Zet de `GET /admin/realms/<realm>` van voor en na in de PR.
- Een blueprintwaarde die je met de hand omzet, is na een verwerking terug te zien op de realm, ook op een realm die al bestond. Dat is de assertie dat de replayweg meedoet.
- `task generate-infrastructure-secrets-for-cluster` levert het versleutelde `keycloak-mail-secret.yaml` voor sandbox én productie, en `kustomize build` van beide overlays komt erdoor met de SOPS-aanroep uit `CLAUDE.md`.
- De Keycloak-pod heeft het bestand op de vault-map staan, en de gekozen vlagvorm is aantoonbaar opgepikt (niet: hij stond in de command line).
- `zad-keycloak` staat op de relay met het wachtwoord uit de Secret. Een tweede start van OPI verandert niets. Een gewijzigde waarde in de Secret bereikt de relay bij de volgende verwerking.
- Een nieuwe gebruiker in een verifiërende realm komt binnen met `emailVerified: false`, krijgt bij zijn eerste login een bevestigingsmail, en is na het aanklikken binnen. Een nieuwe gebruiker in een niet-verifiërende realm gedraagt zich als vandaag.
- Een SSO-login levert GEEN bevestigingsmail op en geen blokkade.
- De bevestigingsmail komt in de Mailpit-sink aan (https://mailsink.sandbox.rijksapp.dev) met het eigen afzenderadres en de eigen naam van `zad-keycloak`, terwijl een testmail van een project zijn eigen adres houdt. Zet de kopregels uit de sink in de PR.
- Op productie: een bevestigingsmail naar een `rijksoverheid.nl`-adres komt aan, en een naar een adres buiten de Rijksoverheid komt aan of levert een gemeten SMTP-antwoord op dat in het verslag staat.
- Een `replyTo` die op de realm staat, komt ongewijzigd aan.
- Een met de hand gewijzigde `smtpServer.host` staat na de volgende verwerking weer goed.
- Het aantal bestaande gebruikers met `emailVerified: false` staat in de PR, per cluster.
- `scripts/mail_identity_check.py` slaagt op de sandbox; plak de uitvoer in de PR.
- De twee metingen uit stap 3 en 6 staan in de PR, ook als de uitkomst saai is.
- `uv run pytest tests/ -q` groen, plus `ruff check .`, `ruff format .` en `pyright`.
