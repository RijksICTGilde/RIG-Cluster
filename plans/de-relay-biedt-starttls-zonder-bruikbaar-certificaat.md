# De relay biedt STARTTLS aan zonder bruikbaar certificaat

**Status**: plan, nog niets gebouwd.
**Datum**: 2026-09-22
**Aanleiding**: nagemeten in productie op 22-09-2026, vanuit een pod van wies, tijdens het aanzetten van send-email voor dat project. Het gat zelf staat al opgeschreven in `plans/mailrelay.md` onder "Wat hiermee NIET is afgedekt": "Submission heeft geen TLS terwijl er PLAIN/LOGIN overheen gaat." Dit plan maakt daar een uitvoerbare stap van.

## Wat er gebeurt

De submission-listener van de relay adverteert STARTTLS, maar met het certificaat dat Stalwart zelf genereert als er geen is opgegeven. Gemeten vanuit `main-frontend` in `rig-prd-wies` naar `rig-mail-relay.rig-prd-ron.svc.cluster.local:587`:

```
EHLO -> 250, features: 8bitmime auth binarymime chunking enhancedstatuscodes
        no-soliciting pipelining requiretls size smtputf8 starttls
AUTH-mechanismen: PLAIN LOGIN

STARTTLS met verificatie -> ssl.SSLCertVerificationError:
        certificate verify failed: self-signed certificate

certificaat: subject=CN=rcgen self signed cert
             issuer =CN=rcgen self signed cert
             notBefore=Jan  1 00:00:00 1975 GMT
             notAfter =Jan  1 00:00:00 4096 GMT
             SAN: DNS:localhost

AUTH zonder STARTTLS -> 235 2.7.0 Authentication succeeded.
```

Daarmee liggen er drie uitkomsten voor een applicatie, en geen ervan is de bedoelde:

1. De client doet STARTTLS met verificatie, de standaard bij nodemailer en bij Python's `smtplib.starttls()` zonder eigen context. Die loopt vast op een onbekende uitgever en verstuurt niets. De naam klopt ook niet: het certificaat noemt alleen `localhost`, terwijl de client `rig-mail-relay.rig-prd-ron.svc.cluster.local` dialt.
2. De client zet verificatie uit. Dan is er versleuteling zonder dat vaststaat met wie, dus wel bescherming tegen meelezen, geen bescherming tegen een tussenpartij.
3. De client slaat STARTTLS over. Dan gaan gebruikersnaam en wachtwoord als base64 over het podnetwerk, en de inhoud van het bericht erachteraan. Dit is wat er vandaag feitelijk gebeurt, want niemand heeft ooit iets anders ingesteld.

De oorzaak staat in `infrastructure/bootstrap/infrastructure/mail/controller/base/config.toml`: `[server.listener.submission]` noemt alleen `bind` en `protocol`. Er is geen certificaat opgegeven, dus Stalwart maakt er zelf een met rcgen. Dat is geen storing maar de standaard van een mailserver die nog niets van zijn eigen identiteit weet.

Wat dit NIET raakt: de sprong van de relay naar `rmrmail.rijksweb.nl`. Die kant is wel dichtgezet, met `[remote.upstream.tls] implicit = false` en `allow-invalid-certs = false`, en gemeten op 19-08-2026 is dat een garantie en geen voorkeur: zonder STARTTLS bij de upstream bouncet Stalwart het bericht in plaats van het alsnog plat te versturen. Het gat zit dus uitsluitend op de eerste sprong, van applicatie naar relay.

## Even de termen, want certificaten bij e-mail lezen anders dan bij het web

Er zijn twee manieren om een SMTP-verbinding te versleutelen, en de verwarring komt doordat ze allebei "TLS" heten:

- **Impliciete TLS**, ook SMTPS, doorgaans poort 465. De verbinding is vanaf de eerste byte versleuteld, precies zoals HTTPS. RFC 8314 beveelt dit aan voor submission.
- **STARTTLS**, doorgaans poort 587. De verbinding begint plat, de client vraagt met het commando `STARTTLS` om een upgrade, en daarna gaat de sessie opnieuw open binnen TLS. Even veilig als impliciete TLS zodra de upgrade heeft plaatsgevonden, maar de client moet hem wel eisen: doet hij dat niet, dan kan een tussenpartij de STARTTLS-regel uit de EHLO-lijst knippen en gaat alles alsnog plat de lijn over. Dat heet stripping.

Poort 25 is een derde geval en speelt hier geen rol: dat is server-naar-server. Daar is TLS historisch *opportunistisch* en worden certificaten meestal niet gevalideerd, omdat post weigeren erger werd gevonden dan post meelezen. Dat is precies waarom e-mail de naam heeft dat certificaten er niet toe doen, en waarom er later DANE en MTA-STS bij zijn verzonnen om die sprong alsnog te kunnen vertrouwen.

Submission is het makkelijke geval, en dat is het goede nieuws. Onze applicatie weet exact welke server hij bedoelt, hij heeft een gebruikersnaam en wachtwoord bij zich, en hij mag gewoon weigeren als het certificaat niet klopt. Dus: behandel het als HTTPS. Het is geen mailprobleem, het is hetzelfde probleem als een interne API zonder certificaat.

## Wat anderen doen

Drie families, en wij zitten in de derde.

**Een publieke aanbieder** (Gmail, Microsoft, Postmark) heeft een gewoon publiek certificaat op `smtp.provider.nl` en klaar. Werkt hier niet: onze relay heeft geen publieke naam en hoort die ook niet te krijgen, want hij is uitsluitend binnen het cluster bereikbaar. Een Let's Encrypt-certificaat vraagt een naam in een publieke zone en een ACME-uitdaging die van buiten bereikbaar is, en die twee halen een dienst naar buiten die nu veilig binnen staat.

**Een relay binnen een cluster** krijgt een certificaat van de interne CA van dat cluster, op de servicenaam die de clients dialen, en de clients krijgen die CA in hun vertrouwensbundel. Dit is de standaardoplossing en het is wat wij gaan doen.

**Een servicemesh** (Istio, Linkerd) doet het zonder dat de applicatie of de relay iets merkt: de sidecars zetten mTLS op tussen elke twee pods. Mooi antwoord, verkeerde schaal. Wij hebben geen mesh en dit is geen reden om er een te introduceren.

De vierde mogelijkheid is eerlijk benoemen dat het in-clusterverkeer plat gaat en dat het NetworkPolicy de enige afscherming is. Dat is vandaag onze feitelijke situatie. Het is verdedigbaar voor een dienst zonder inloggegevens, maar hier gaan er wachtwoorden overheen, en berichten die persoonsgegevens kunnen bevatten.

## De keuze: cert-manager, op elk clustertype dezelfde weg

Een eigen CA per cluster, uitgegeven door cert-manager, en daaruit een certificaat voor de relay op de servicenaam die de applicaties al dialen.

Waarom deze en niet de makkelijkere: wij draaien drie clustertypen. ODCN is OpenShift, `local` en `sandboxed-local` zijn Kind. Een oplossing die alleen op OpenShift werkt levert twee mechanismen op die hetzelfde bedoelen, en daarmee een sandbox die iets anders bewijst dan productie doet. Dat is precies de valkuil waar het laatste open punt hieronder al over gaat. Eén weg die overal werkt is hier meer waard dan een weg die op één cluster iets minder werk is.

cert-manager staat er al. Op `local` draait hij met een CA-uitgever (`kind-ca-issuer` in `infrastructure/bootstrap/infrastructure/cert-manager/config/overlays/local/`), en op productie bestaan de CRD's en mogen wij erin schrijven (`auth can-i create certificates.cert-manager.io -n rig-prd-ron` zegt ja, gemeten 22-09-2026). Het patroon is het standaardpatroon: een SelfSigned-uitgever maakt een langlevend CA-certificaat, dat CA-certificaat wordt een Issuer, en die geeft het certificaat van de relay uit met een korte looptijd die cert-manager zelf vernieuwt.

Twee eigenschappen die goed uitkomen:

- De naam die het certificaat draagt, `rig-mail-relay.rig-prd-ron.svc.cluster.local`, is letterlijk de waarde die `get_mail_relay_host()` al in `SMTP_HOST` zet. Geen enkele applicatie hoeft een ander adres te gaan gebruiken.
- De CA is langlevend en het blad eronder rouleert. Wat de applicaties moeten vertrouwen verandert dus jarenlang niet, terwijl het certificaat van de relay wel gewoon ververst.

Het raakt de upstream-kant niet. Die blijft staan zoals hij staat.

Afgewogen en niet gekozen:

- **Het servicecertificaat van OpenShift.** De annotatie `service.beta.openshift.io/serving-cert-secret-name` levert een certificaat op de juiste namen en de bijbehorende CA staat al in élke namespace, in de ConfigMap `openshift-service-ca.crt` (aanwezig in `rig-prd-wies`, 399 dagen oud, dus de operator draait). Dat scheelt zowel CA-beheer als het verspreiden van de bundel, en het is eerlijk om te zeggen dat het op ODCN de kortste weg is. We nemen hem niet, omdat hij de TLS van de relay vastpint op één clusterplatform en de twee Kind-clusters dan iets anders doen. Blijft beschikbaar als cert-manager op enig cluster in de weg blijkt te zitten, en dan als expliciete uitzondering met de reden erbij, niet als stilzwijgende tweede weg.
- **Overstappen op impliciete TLS op 465.** Lost het certificaatprobleem niet op, dat is hetzelfde probleem met een ander startsein, en het breekt wel `SMTP_PORT` voor elk bestaand project. Wat het wél zou oplossen is stripping, maar dat lossen we hieronder op door de client te laten eisen in plaats van vragen.
- **trust-manager erbij halen** om de bundel naar elke namespace te kopiëren. Een component erbij voor iets wat de dienst zelf al kan, zie stap 3.

## Wat we bouwen

**1. Meet hoe Stalwart v0.11.8 een certificaat op een listener wil hebben.**

Niet overslaan en niet uit de documentatie overschrijven. De configmap waarschuwt er zelf voor: "De sleutelnamen zijn versiegebonden. Een sleutel die Stalwart niet kent, wordt STIL genegeerd, er komt geen foutmelding en de regel doet niets." Een verkeerd gespelde sleutel geeft dus geen fout maar het huidige gedrag, en dat is niet van het juiste te onderscheiden zonder te meten.

Verifieer: op de sandbox een certificaat mounten, de relay starten, en van buitenaf EHLO plus STARTTLS doen met verificatie tegen de bijbehorende CA. Geslaagd is: de handshake komt door en de naam in het certificaat is de servicenaam. Leg de gemeten sleutelnamen vast in `config.toml` op de manier waarop de rest van dat bestand het doet, met de meting erbij.

**2. Een CA per cluster en een certificaat op de relay.**

In `infrastructure/bootstrap/infrastructure/mail/controller/` komt naast de bestaande resources een Issuer plus een Certificate. Op `local` kan de bestaande `kind-ca-issuer` de uitgever zijn; op de andere clusters hoort er een eigen CA bij de relay, gemaakt met het standaardpatroon SelfSigned-uitgever, CA-certificaat, CA-uitgever. De namen zijn voorstellen, geen afspraken: `rig-mail-relay-ca` voor de CA en `rig-mail-relay-tls` voor het Secret met het certificaat.

Het Certificate draagt alle drie de namen waarop een client kan dialen, net zoals de sink dat al doet in zijn openssl-regel: `rig-mail-relay.<ns>.svc.cluster.local`, `rig-mail-relay.<ns>.svc` en `rig-mail-relay`. De namespace verschilt per cluster (`rig-ron` versus `rig-prd-ron`), dus dat deel hoort in de overlays en niet in de basis.

Daarna het Secret als volume in het Deployment en de verwijzing erheen in `config.toml`, met de sleutelnamen uit stap 1. Let op twee dingen die anders stil misgaan:

- Mount het volume als map, niet met `subPath`. Een `subPath`-mount ziet een bijgewerkt Secret nooit, en dit certificaat wordt automatisch vernieuwd. De config zelf wordt wél met `subPath` gemount en dat blijft zo, want die verandert alleen via een nieuwe ConfigMap-hash en dus via een rollout.
- Zoek uit of Stalwart het certificaat herleest als het bestand verandert, of dat er een herstart nodig is. Zo ja, dan hoort daar iets bij dat die herstart uitlokt bij vernieuwing, of anders een aantekening dat de relay eens per certificaatperiode herstart moet worden. Een certificaat dat ververst terwijl de relay het oude blijft aanbieden is een storing die pas na de eerste verlenging zichtbaar wordt.

Verifieer: vanuit een projectpod STARTTLS met volledige verificatie tegen de CA, gevolgd door AUTH. Geslaagd is `235`, zonder dat er ergens verificatie is uitgezet. Doe dezelfde meting op de sandbox en op productie, en verwacht hetzelfde antwoord: dat is meteen het bewijs dat de ene weg ook echt één weg is.

**3. De clientkant: de CA bij de applicatie, en het pad ernaartoe.**

Een certificaat op de relay is niet genoeg. De TLS-bibliotheek van de applicatie moet de uitgever kennen, want de systeembundel van de container kent onze CA niet, en hij moet weten waar die ligt. Dit is opgelost werk: VLAM doet precies dit al in `opi/services/catalog/vlam/__init__.py`. Die dienst mount een CA-bundel via `contribution.secret_mounts`, schrijft hem als Secret per deployment via `build_secret_files`, en zet er `VLAM_CA_BUNDLE_PATH` bij, met als expliciete redenering dat een adres zonder de uitgever een "unknown issuer" oplevert en een bundel zonder pad niets doet.

Neem dat patroon over voor send-email, inclusief de redenering waarom de bundel een platformgegeven is en geen projectgegeven: identiek voor elke afnemer, en als bijlage zou hetzelfde publieke bestand AGE-versleuteld in elk projectbestand landen.

Waar de bytes vandaan komen is de enige echte keuze hier, en het is er een tussen twee bestaande manieren:

- **Zoals VLAM**: het CA-certificaat als bestand in het dienstpakket, met een verwijzing per cluster in `cluster_config.py`, net als `vlam.ca_bundle`. Geen nieuwe rechten nodig, want OPI leest een bestand uit zijn eigen image. Rotatie van de CA is dan een bestandswijziging, en dat is precies de afweging die bij VLAM al is gemaakt en opgeschreven. Met een langlevende CA en een rouleerend blad eronder is dat hooguit eens in de jaren.
- **Uit het cluster lezen**: OPI haalt `ca.crt` uit het Secret van stap 2. Rotatie volgt dan vanzelf, maar OPI leest vandaag niets uit de relay-namespace (hij kent die naam alleen als peer in een NetworkPolicy), dus dit vraagt nieuwe rechten over een namespacegrens heen voor precies één bestand.

Voorstel: de eerste. Het is minder macht, het is het patroon dat er al ligt, en het voordeel van de tweede verdwijnt zodra de CA langlevend is.

De variabelenaam is een voorstel: `SMTP_CA_FILE`, naast de bestaande vijf in `opi/services/catalog/send_email/variables.py`. Alternatief is de bestaande `ca_config`-weg uitbreiden en de bundel op het OpenSSL-hashpad hangen, waarmee alles wat OpenSSL gebruikt hem vanzelf vindt en er geen variabele bij hoeft. Dat is eleganter voor Python en curl en doet niets voor Node, dat zijn eigen wortels meedraagt en `NODE_EXTRA_CA_CERTS` wil. Mijn voorstel is de variabele, omdat die in alle talen werkt en omdat hij zichtbaar maakt dat er iets te vertrouwen valt. Dit is een open beslissing, zie hieronder.

Verifieer: een deployment die send-email gebruikt draait met de variabele en de mount, en de test uit stap 2 slaagt met precies dat pad.

**4. Zeg het in de uitleg.**

`opi/services/catalog/send_email/help.md` zegt nu niets over TLS. Daar hoort te staan: gebruik STARTTLS, eis hem, en wijs je bibliotheek naar `SMTP_CA_FILE`. Met een voorbeeldregel voor de twee talen die onze projecten gebruiken, want "zet de CA in je trust store" is voor de meeste lezers geen uitvoerbare instructie. `features/send-email.md` krijgt dezelfde regel bij de beschrijving van wat een component krijgt.

**5. Later en apart: STARTTLS verplicht stellen.**

Zodra er een bruikbaar certificaat staat, kan de relay AUTH weigeren op een platte verbinding. Dat is de enige echte bescherming tegen stripping, want tot die tijd blijft een client die STARTTLS overslaat gewoon geaccepteerd worden, precies zoals wies vandaag doet.

Bewust niet in dezelfde stap: dit breekt elk bestaand project dat nu plat inlogt, en op het moment van schrijven is dat elk project dat de dienst gebruikt. Dit hoort na een aangekondigde overgang, met een meting vooraf van wie er nog plat inlogt. Die meting kunnen we nu al niet doen, zie het open punt hieronder.

## Wat er NIET in zit

- De management-API blijft plat HTTP met Basic auth binnen het cluster, afgeschermd door NetworkPolicy. Zelfde soort gat, andere listener, eigen afweging. `plans/mailrelay.md` noemt het al en dit plan raakt het niet.
- De sprong naar de upstream verandert niet.
- Er komt geen publieke naam en geen publiek certificaat voor de relay.

## Open punten

- **Waar de CA vandaan komt** (stap 3). Mijn voorstel staat er, de keuze is niet gemaakt.
- **Variabele of hashpad** (stap 3). Idem.
- **Draait cert-manager overal waar de relay draait?** In productie mogen wij Certificates maken en op `local` staat de uitgever klaar. Voor `sandboxed-local` is dat nog niet nagemeten, en daar draait de relay via een eigen ArgoCD-app. Toets dat vóór stap 2, want als het antwoord nee is, is de vraag niet "dan maar OpenShift" maar "dan cert-manager erbij op dat cluster".
- **Herleest Stalwart een vernieuwd certificaat?** Onbekend, bepaalt of stap 2a een herstartmechanisme nodig heeft.
- **Wie logt er nu plat in?** Voor stap 5 wil je dat weten, en de relay logt dat vandaag niet op een manier waar dat uit te halen is. Uitzoeken of Stalwart dat kan laten zien voordat stap 5 begint.
- **Sandbox versus productie wijken af.** De Keycloak-mailprovider draagt de aantekening dat de submission-listener op de sandbox géén STARTTLS aanbiedt (RC-158, met EHLO nagemeten), terwijl productie hem wel adverteert. Dat verschil is niet verklaard. Zoek het uit tijdens stap 1, want een meting op de sandbox die niet representatief is voor productie is erger dan geen meting.

## Voor nu, zolang dit niet gebouwd is

Een project dat vandaag mail wil versturen heeft twee werkende opties en één die niet werkt. Werkt niet: STARTTLS met verificatie. Werkt wel: STARTTLS met verificatie uit, of helemaal geen STARTTLS. De tweede is eerlijker over wat er gebeurt, de eerste beschermt tegen passief meelezen. Zeg er in beide gevallen bij dat het tijdelijk is, zodat het opruimen ervan niet vergeten wordt zodra stap 3 er staat.
