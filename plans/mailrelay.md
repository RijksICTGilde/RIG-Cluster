# Plan: eigen SMTP-relay voor ZAD-projecten

**Status**: ontwerp, wacht op akkoord
**Datum**: 2026-08-03
**Scope**: elke applicatie op het platform, inclusief ZAD zelf, mail laten versturen via één eigen relay die per project een account uitgeeft en naar één upstream mailserver relayt

## Wat we bouwen

```
  rig-prd-<project>            rig-prd-mail                       RON
+---------------------+   +--------------------------+   +--------------------+
| app                 |   | SMTP-relay               |   | upstream mailserver|
| SMTP_HOST=...       |-->| :587 submission, AUTH    |-->| :25 STARTTLS       |
| SMTP_USER=<project> |   | rate limit per account   |   | geen auth, ons IP  |
| SMTP_PASS=<secret>  |   | From-policy + DKIM       |   | staat toegelaten   |
+---------------------+   | header sanitatie         |   +--------------------+
     ClusterIP, geen      | egressGatewayPolicy:     |
     RON-egress nodig     |   rig-ron                |
                          +--------------------------+
```

Eén centrale relay, geen relay per project. Een project krijgt een SMTP-account, een toegestaan afzenderadres en een eigen limiet.

## Twee aannames uit de opdracht die ik anders zie

**"We don't relay for you" is niet het risico dat je denkt.** Die melding (`550 5.7.1 Relaying denied`) krijg je van een server als je *ongeauthenticeerd* verbindt en mail aanbiedt voor een domein waar die server niet verantwoordelijk voor is. Met SASL-authenticatie op een submission-poort treedt dat pad niet op. De echte afwijzingen die ons gaan raken zijn andere: `Sender address rejected: not owned by user` als onze envelope-afzender niet bij het geauthenticeerde account hoort, tempfails door rate limiting bovenop hun kant, en op termijn reputatieschade als er rommel doorheen gaat. Het plan hieronder mitigeert die drie, niet de melding uit de vraag.

**"De upstream mag niet zien dat we relayen" is de verkeerde formulering van een terechte wens.** Techniek die verbergt wat er gebeurt, is broos en gaat een keer stuk op een header die je vergat. De robuuste variant is dat het geen relay *is* in de betekenis die de upstream interesseert: elk bericht heeft een afzender in ons eigen domein, is ondertekend met onze DKIM-sleutel, en komt van één geauthenticeerd account. Dan is het gewoon uitgaande mail van onze organisatie, en de vraag "relayen jullie voor derden" is feitelijk met nee te beantwoorden. Dat het intern meerdere applicaties zijn, is onze zaak, precies zoals jij zegt. Header-sanitatie doen we daarbovenop, maar als privacymaatregel (interne topologie lekt niet naar buiten), niet als verhullingstruc. Dat is standaardgedrag van elke submission-server.

Wat daar wél bij hoort: **dit één keer expliciet afstemmen met het mailteam.** Niet om toestemming te vragen voor techniek, maar om vast te leggen dat één account namens meerdere interne applicaties in ons eigen domein verstuurt, met een afgesproken volume. Dan is er later geen discussie. Het alternatief (hopen dat het niet opvalt) is precies het scenario dat je wilt vermijden, want dan komt de vraag op het slechtst mogelijke moment.

## Gemeten op 17 augustus 2026, en dit wijkt af van wat hieronder is aangenomen

De koppeling werkt en een testbericht is aangenomen. Vier correcties op de tekst hieronder, volledig uitgeschreven in `docs/ron-koppeling.md`:

- **Uitleveren op poort 25**, niet 587 of 465, die zijn dicht.
- **Geen authenticatie.** De upstream adverteert geen `AUTH` en vertrouwt op ons uitgaande IP `145.21.227.140`. Waar hieronder "credentials" of "één account" staat, lees "ons IP staat in hun toelating".
- **STARTTLS** wordt aangeboden en moet gebruikt worden.
- **30 MB** is hun grens (`SIZE 31457280`).

Dat maakt de paragraaf hierna niet minder waar, maar juist dwingender: zonder authenticatie aan de andere kant is er niets dat een applicatie tegenhoudt behalve ons eigen netwerkbeleid.

## Waarom niet gewoon de upstream langs elke app openzetten

Dat is de nul-optie en die moet je expliciet verwerpen, anders bouw je iets duurs zonder reden:

- Elke namespace zou `rig-ron` egress nodig hebben, en die annotatie neemt maar één waarde. Een project met RON-mail kan dan niet meer bij internet. Met een centrale relay heeft alleen die ene namespace RON nodig.
- Er is niet eens een wachtwoord om te delen: de upstream vertrouwt op het bronadres. Elke pod die dat adres op poort 25 kan bereiken, mailt namens onze organisatie zonder limiet, zonder From-policy, zonder DKIM en zonder log. Eén gecompromitteerde app brandt daarmee de hele relatie met het mailteam af, en is achteraf niet eens aan te wijzen.
- Die grendel zit er structureel al: de enige externe egressregel staat hardgecodeerd op 443 en 80 in `manifests/tenant-baseline-network-policy.yaml.jinja`, en `ports.outbound` uit het projectbestand wordt nergens naar egress vertaald. Wat ontbreekt is een **regressietest die dat vastlegt**, want nu is het een stilzwijgende eigenschap waar de hele mailbeveiliging op rust.
- Geen limiet. Een bug in een retry-loop stuurt tienduizend berichten voordat iemand het merkt.
- Geen attributie. Bij een klacht weet je niet welk project het was.
- Het mailteam moet elk egress-IP kennen. Met een centrale relay is dat er precies één, en dat is het adres uit `145.21.227.140/30` dat we al doorgeven (zie `docs/ron-koppeling.md`).

## Productkeuze

Eisen: SMTP only, accounts per project via een API, limieten per account, controle op uitgaande rommel, smarthost met credentials, en envelope- en headerherschrijving.

| | Stalwart MTA | Postfix + rspamd + postfwd | Postal | Haraka | maddy / chasquid |
|---|---|---|---|---|---|
| SMTP only, geen mailboxen | ja | ja | ja | ja | ja |
| Accounts aanmaken via API | REST management API | nee, bestanden of SQL/LDAP zelf vullen | ja, eigen API | zelf schrijven | nee |
| Limiet per geauthenticeerd account | ja, throttle op `authenticatedAs` | postfwd of policyd erbij | ja, per credential | zelf schrijven | nauwelijks |
| Uitgaande inhoudscontrole | ingebouwd | rspamd erbij | SpamAssassin/rspamd koppelbaar | plugins | nee |
| Smarthost met SASL | ja, secret uit env of file | ja | ontworpen voor directe bezorging | ja | ja |
| Envelope- en headerherschrijving | regels op listener, sender, ontvanger | header_checks, canonical | beperkt | plugins | beperkt |
| Onderdelen om te draaien | 1 | 3 tot 4 | Rails + MariaDB + RabbitMQ | 1 | 1 |

**Voorstel: Stalwart MTA (Community, AGPL-3.0).**

De doorslag geeft dat het als enige alle vier de dingen in één proces heeft die wij per project nodig hebben: een account, een limiet op dat account, een afzenderpolicy op dat account, en een API om dat aan te maken. Bij Postfix moet ik dat uit drie componenten samenstellen en de accounts zelf beheren, wat neerkomt op zelfbouw-provisioning in configbestanden. Dat past slecht bij hoe OPI de rest doet (Keycloak, Postgres en MinIO worden allemaal via een API van de dienst zelf ingericht).

Wat we nodig hebben zit volledig in Community. Enterprise voegt de LLM-spamclassifier, webhooks, delivery-history en per-domein directory-backends toe. Voor webhooks is er in Community MTA Hooks, wat voor bouncemeldingen volstaat. AGPL-3.0 is voor ons geen bezwaar zolang we niets aanpassen en het intern draaien; wijzigen we het wel, dan moeten we die wijzigingen publiceren, wat voor een overheidsorganisatie sowieso de lijn is.

Afgevallen, met reden:

- **Postal** is een verzendplatform met multi-tenancy, suppressielijsten en click tracking. Het is ontworpen om zelf te bezorgen met eigen IP-reputatie. Alles door één upstream duwen gaat tegen de korrel in, en we slepen Rails, MariaDB en RabbitMQ mee voor functies die we niet gebruiken.
- **Postfix plus rspamd plus postfwd** is de veiligste keuze qua bedrijfszekerheid en de meest bewezen. Het is de terugvaloptie als Stalwart in de proef tegenvalt. De prijs is drie componenten, drie configtalen en handmatig accountbeheer.
- **Haraka** betekent dat je het beleid in JavaScript schrijft. Dat is precies de flexibiliteit die we niet nodig hebben, met onderhoud dat we wel krijgen.
- **maddy en chasquid** zijn prettig klein, maar hebben geen limieten per account en geen uitgaande inhoudscontrole. Dan mist het de helft van de opdracht.

## De identiteitsregels: dit is de kern

Dit bepaalt of mail aankomt én of de upstream-vraag ooit opkomt. Alles hieronder wordt op de relay afgedwongen, niet aan de applicatie overgelaten.

1. **Envelope-afzender (`MAIL FROM`) wordt herschreven** naar één adres in ons domein, per project herkenbaar, bijvoorbeeld `bounce+<project>@<maildomein>`. De upstream ziet daardoor altijd hetzelfde domein bij hetzelfde account, wat hun sender-policy tevreden houdt, en bounces komen bij ons terug en zijn herleidbaar tot een project.
2. **De `From:`-header wordt vastgezet** op een adres binnen ons maildomein. Een project mag zijn weergavenaam kiezen, niet zijn domein. Dit is geen betutteling: een `From:` in een vreemd domein haalt DMARC nooit, dus die mail komt toch niet aan.
3. **DKIM-ondertekening met onze eigen sleutel**, op de relay. De handtekening overleeft de upstream-hop, dus DMARC slaagt op DKIM-alignment ongeacht wat de upstream met de envelope doet.
4. **SPF van ons maildomein moet de uitgaande IP's van de upstream autoriseren**, niet die van ons. Wij praten nooit rechtstreeks met het internet; de upstream doet de eindbezorging. Dit is een DNS-actie die we bij het mailteam moeten ophalen en makkelijk over het hoofd te zien is.
5. **`Received`-headers van binnen het cluster worden verwijderd** en vervangen door één regel van de relay. Interne pod-IP's, namespace-namen en het SASL-account horen niet buiten het cluster. Dit is de privacymaatregel, en meteen de reden dat het aan de buitenkant geen keten is.
6. **`Message-ID` wordt herschreven** naar ons maildomein als de applicatie er een zet met een interne hostnaam erin.
7. **Verklikkerheaders eruit**: `X-Originating-IP`, `X-Authenticated-Sender`, `X-Mailer` en wat de applicatie verder meestuurt.

## Afzenderdomein: global of per project

Technisch kan het allebei. De relay kan meerdere afzenderdomeinen bedienen, elk met een eigen DKIM-sleutel. De kosten zitten niet in de mailserver maar in DNS, en voor een domein dat wij niet beheren is dat een ronde met de DNS-beheerder van die organisatie, precies zoals bij een eigen webdomein in `domein.md`.

Wat het goedkoop maakt, is de envelope-herschrijving uit punt 1 hierboven. **SPF geldt voor het envelope-domein, niet voor de `From:`-header.** Omdat wij de envelope altijd op ons eigen maildomein zetten, hoeft SPF alleen op óns domein te kloppen, ook als de `From:` een projectdomein toont. DMARC slaagt al als één van SPF en DKIM aligned is, en DKIM ondertekenen wij zelf met een sleutel voor dat domein.

Daarmee valt de rekening zo uit:

| | Eenmalig | Per extra afzenderdomein |
|---|---|---|
| SPF | één TXT op ons maildomein, autoriseert de upstream-IP's | niets |
| DKIM | één TXT op ons maildomein | één TXT in de zone van de organisatie |
| DMARC | één TXT op ons maildomein | niets van ons, hun beleid geldt |

Een projectdomein kost dus **één DKIM-record in hun zone**, geen volledige set. Dat is een aanzienlijk kleinere vraag dan de CAA- en CNAME-ronde die we voor een webdomein al doen.

Het lokale deel is nog goedkoper: `noreply@`, `support@` of `<project>@` per project instellen raakt DNS niet, dat is een policyregel op het account.

**Voorstel: nu alleen het platformdomein bouwen, met het lokale deel per project instelbaar.** Het configmodel krijgt wel een optioneel domeinveld zodat we later niet hoeven te verbouwen, maar het inrichtingspad voor eigen domeinen bouwen we pas als een project erom vraagt. Dan hergebruiken we de bestaande eigen-domein-flow.

## Limieten en uitgaande controle

- **Per account, op submission**: berichten per uur en per dag, gelijktijdige verbindingen, en een maximum aantal ontvangers per bericht. Stalwart kan de inbound throttle keyen op `authenticatedAs`, dus dat is precies het project.
- **Globaal plafond** bovenop de som van de projecten, zodat een fout in de provisioning nooit de afspraak met het mailteam kan overschrijden.
- **Inhoudscontrole**: de spamfilter van Stalwart draait ook op submission. Wees eerlijk over wat dit oplevert: het vangt een gecompromitteerde of stuk geconfigureerde applicatie, niet veel meer. De limieten doen het echte werk.
- **Berichtgrootte en bijlagen** begrenzen, lager dan wat de upstream accepteert, zodat een afwijzing bij ons zichtbaar wordt en niet bij hen.
- **Bounces** komen op ons bounce-adres terug en moeten ergens landen waar iemand ze ziet. Zonder dit merk je een blokkade pas als een gebruiker klaagt. Zie de volgende paragraaf.

## Bounces terugtonen in ZAD

Dit zijn twee verschillende dingen, en alleen het eerste is gratis.

**Directe afwijzingen zien we zelf.** Wat de upstream bij het aanbieden weigert en wat wij zelf tegenhouden (limiet, verkeerde `From:`, te groot) is een synchroon SMTP-antwoord op onze eigen relay. Community heeft MTA Hooks, HTTP-callbacks op berichtverwerking. Die laten we naar een OPI-endpoint posten, we schrijven het weg in rig-db en tonen per project een maillog op de projectpagina: verstuurd, geweigerd, met reden. Dat dekt de fouten die een ontwikkelaar zelf kan oplossen, en dat is het merendeel. (De kant-en-klare delivery-history-UI van Stalwart is Enterprise, maar die hebben we niet nodig, we hebben de data nodig.)

**Echte bounces zijn inkomende mail, en dat botst met "alleen uitgaand".** Een DSN over een onbekende ontvanger of een volle mailbox komt minuten later terug als bericht aan onze envelope-afzender. Om die in ZAD te tonen moeten we mail kunnen ontvangen. Drie opties, oplopend in werk:

1. **Niets doen in fase 1.** We zien de directe afwijzingen wel en de asynchrone niet. Eerlijk, maar dan is een fout adres onzichtbaar.
2. **Een mailbox bij het mailteam die OPI over IMAP leegtrekt** en de DSN's parseert. Het `bounce+<project>@` adres maakt de koppeling naar het project meteen duidelijk. Dit vraagt alleen een account, geen inkomend SMTP en geen ingress. Dit is mijn voorstel.
3. **De upstream routeert bounces terug naar onze relay** over de ingresskant van de RON-koppeling (`145.21.227.136/30`). Netjes, maar het vraagt inkomende connectiviteit en werk bij het mailteam, en dat maakt van "alleen uitgaand" een half mailplatform.

Optie 2 in fase 2, en de maillog uit de hooks in fase 1.

## Wat OPI moet bouwen

Volgens `instructions/services.md`, met de bestaande services als model. Dichtstbijzijnde analogie is `minio-storage`: centrale dienst, per project een account en credentials in de projectsecrets.

- `ServiceType` erbij, plus `ServiceDefinition` en een regel in `registry.py`. Naam: zie hieronder.
- `opi/connectors/mail.py`: de enige plek die met de management API van de relay praat.
- `opi/manager/mail_manager.py`: account aanmaken, wachtwoord roteren, limiet zetten, opruimen bij projectverwijdering.
- `opi/services/catalog/mail/`: configmodel (afzendernaam, limiet, aan/uit), editables en visualizers voor het projectscherm.
- Variabelen naar de app: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`. Wachtwoord via SOPS in de projectsecrets, net als de database.
- NetworkPolicy: projectnamespaces mogen naar de relay op 587, en verder niets.
- Opruimen bij verwijderen van een project hoort in `delete_project_manager`, anders krijgen we dezelfde weesaccounts als bij de service-orphan-sweep.

## Infrastructuur

- Eigen namespace, bijvoorbeeld `rig-prd-mail`, met `egress.projectcalico.org/egressGatewayPolicy: rig-ron`. Alleen deze namespace heeft RON nodig.
- Kustomize onder `infrastructure/bootstrap/infrastructure/mail/` met `base` plus overlays per clustertype, zoals de andere componenten.
- Opslag in PostgreSQL in plaats van een PVC. Dat scheelt de RWO-PVC-problematiek (`strategy: Recreate`), maakt meerdere replicas mogelijk en past bij wat we al draaien.
- De DKIM-sleutel als SOPS-secret, aan de pod gegeven als file of env. Upstream-credentials zijn er niet, zie de correcties bovenaan.
- Geen publieke ingress. De relay is uitsluitend intern bereikbaar.

## Gefaseerd, met verifieerbare uitkomst per stap

1. **Afstemmen met het mailteam.** Vastleggen: het account, het toegestane afzenderdomein, het volume, en hun uitgaande IP's voor onze SPF. Verifieerbaar: schriftelijke bevestiging plus de SPF-waarde.
2. **Bereikbaarheid bewijzen.** Vanuit een pod met `rig-ron` een TCP-verbinding en `EHLO` naar de upstream. Verifieerbaar: een SMTP-banner terug, wat meteen de oude `Network is unreachable` afsluit.
3. **Relay draaien in de sandbox** met een testupstream. Verifieerbaar: bericht van A naar B, en in de headers aan de ontvangende kant staat geen enkele interne hostnaam of pod-IP.
4. **Identiteitsregels aanzetten** (punt 1 tot en met 7 hierboven). Verifieerbaar: een testbericht met een expres foute `From:` en een verklikkerheader komt aan met onze `From:`, onze DKIM-handtekening en zonder die header.
5. **Limieten aanzetten.** Verifieerbaar: een account dat over zijn limiet gaat krijgt een tempfail, een ander account merkt daar niets van.
6. **OPI-service bouwen.** Verifieerbaar: project met de service aan krijgt werkende credentials, kan mailen, en na verwijderen van het project bestaat het account niet meer.
7. **ZAD zelf als eerste gebruiker.** Verifieerbaar: een uitnodiging komt aan.
8. **Naar productie**, één project tegelijk.

## Wat dit ontgrendelt

ZAD heeft nu geen SMTP, en dat is zichtbaar in het product: `resetPasswordAllowed` staat hardgecodeerd op `False` en "wachtwoord vergeten" bestaat niet, precies omdat er geen mailserver is (zie `features/futures/keycloak-sso-bypass-voorkomen.md`). Ook Keycloak-verificatiemails en uitnodigingen leunen hierop. Dit plan is dus niet alleen een dienst voor projecten, het haalt een blokkade weg in de authenticatie van het platform zelf.

## De naam van de service

`sendmail` raad ik af, en dat is de enige naam waar ik echt bezwaar tegen heb. Sendmail is een bestaande MTA en `/usr/sbin/sendmail` is een bekende aanroep-interface. Een service die zo heet, laat lezers denken dat we die MTA draaien of dat het om dat commando gaat, en die verwarring blijft jaren hangen.

Je onderliggende punt klopt wel: `smtp-mail` zet een protocolnaam in iets dat gebruikersgericht is, en dat doen we nergens anders. `publish-on-web` heet niet `https-ingress`, `persistent-storage` heet niet `pvc`.

**Voorstel: `send-email`.** Dezelfde werkwoordsvorm als `publish-on-web`, zegt wat het doet, zegt meteen dat het eenrichting is, en geen protocol- of productnaam. Tweede keus is `outgoing-mail`. Blijf je bij `smtp-mail`, dan is dat prima werkbaar, het is alleen minder consistent.

## Openstaande beslissingen

1. **Bevestigen: `mail.rijksapp.nl` als platform-maildomein.** Let op het enkelvoud. `rijksapps.nl` is de zone van ODC-Noord zelf (`docs.`, `rcr.`, `cluster-api.apps.prd1.gn2.`), daar kunnen wij niets aanmaken en applicatiemail versturen vanuit het domein van onze platformleverancier is sowieso onverstandig. `rijksapp.nl`, `rijks.app` en `rijksapp.dev` zijn wel van ons en worden al door external-dns beheerd (`opi/core/cluster_config.py`). Een subdomein in plaats van de kale zone houdt een eventueel reputatieprobleem weg bij het domein waar de applicaties op draaien. Die twee namen schelen één letter en hebben verschillende eigenaren; benoem dat expliciet in de mail naar het mailteam.
2. **Kan external-dns de losse TXT- en MX-records zetten** (DNSEndpoint-CRD), of gaat dat via het DNS-beheerpaneel? Geen blokkade, wel bepalend voor wie het doet.
3. **Bounce-mailbox**: krijgen we een account bij het mailteam dat OPI over IMAP mag legen? Nodig voor optie 2 hierboven.
4. **Alleen verzenden, of later ook ontvangen?** Ik ga nu uit van alleen verzenden. Ontvangen is een wezenlijk ander product en moet niet stiekem meegroeien via de bounce-afhandeling.
5. **De servicenaam** (zie hierboven).

Beslist in dit ontwerp, niet meer open: het lokale deel is per project instelbaar, het afzenderdomein is voorlopig één platformdomein met een optioneel veld in het configmodel, en de maillog in ZAD komt uit MTA Hooks.

---

# Aanvulling, 15 augustus 2026

Vier dingen die na het oorspronkelijke ontwerp zijn gemeten of besloten. De rest van het plan hierboven blijft staan; waar deze aanvulling een openstaande beslissing invult, staat dat erbij.

## 1. Bereikbaarheid: gedeeltelijk bewezen, nog niet rond

Stap 2 van de uitrol is uitgevoerd vanuit `rig-prd-vlam-wt8` op productie, de namespace die `egress.projectcalico.org/egressGatewayPolicy: rig-ron` draagt. Gemeten met `nc` in de bestaande `productie-vlam-proxy`-pod, dus zonder iets uit te rollen:

- **DNS klopt.** `rmrmail.rijksweb.nl` resolvet naar `145.21.161.201`, precies het adres dat we hebben gekregen.
- **De route bestaat.** De oude `Network is unreachable` is weg. Dat is nieuw en het sluit de bevinding in `docs/ron-koppeling.md` gedeeltelijk af.
- **Er komt niets terug.** Poort 25, 587 en 465 lopen alle drie in een timeout zonder banner en zonder weigering. Het adres van de egressgateway zelf gedraagt zich hetzelfde.

Dat patroon (uitgaand vertrekt, niets keert terug, geen ICMP-weigering) wijst op een firewall die stil laat vallen of op retourverkeer dat de weg terug niet vindt. Dat het op drie poorten identiek is, pleit tegen "poort 25 staat dicht".

**Wat er nog moet gebeuren, en het is geen bouwwerk maar een vraag:** welk bronadres uit `145.21.227.140/30` ziet de tegenpartij werkelijk, staat dat adres in hun toelating naar `145.21.161.201`, en is het retourpad ingericht. `docs/ron-koppeling.md` waarschuwt al dat welk adres uit de pool het SNAT-proces pakt niet bij ons vastligt, en houdt datzelfde punt als openstaand. Wij kunnen het zelf niet zien: de namespace `quattro-egress-gateway` is van ODCN en niet leesbaar met onze rechten.

**Deze stap blijft blokkerend voor alles wat daarna komt.** Zolang die banner er niet is, bouwen we op een aanname.

## 2. De namespace: `rig-prd-ron`, en waarom niet `rig-prd-operations`

Het ontwerp noemde `rig-prd-mail` als voorbeeld. Het wordt `rig-prd-ron`: een namespace voor RON-gebonden diensten in het algemeen, met mail als eerste bewoner en de VLAM-gateway als de volgende die er thuishoort. De naam volgt de eis van ODCN dat een namespace op dat cluster met `rig-prd` begint; `rig-operations-ron` of `rig-ron` kan daar dus niet.

De reden om het niet in `rig-prd-operations` te zetten is niet behoudendheid maar onmogelijkheid, en dat is nu gemeten: **de annotatie `egressGatewayPolicy` neemt één waarde.** Op `rig-prd-vlam-wt8` staat hij op `rig-ron`, en in de laatst toegepaste configuratie van diezelfde namespace staat nog `internet`. Het is dus een of-of. RON aanzetten op `rig-prd-operations` kost daar het internet, en daarmee ArgoCD, de registry en Keycloak. De eis "internet moet blijven werken" en "RON erbij" kunnen in één namespace niet allebei waar zijn.

Gevolg voor het netwerkbeleid, en dat is de prijs: verkeer tussen projectnamespaces en deze namespace moet expliciet worden toegestaan, per project. Dat hoort via het dienstensysteem te gaan en niet met de hand.

## 3. Het netwerkbeleid komt uit de dienst zelf

Het plan zegt wat het beleid moet toestaan (projectnamespaces naar de relay, verder niets) maar niet waar het vandaan komt. Het hoort bij de dienst: die weet wanneer hij aanstaat en voor welk component.

**Eerst uitzoeken, dan bouwen:** kan een dienst vandaag een NetworkPolicy bijdragen, of dragen de manifesthaken alleen containers, secrets en poorten bij? De auth wall voegt een sidecar toe via `contribute_manifest_context`, dus er is een weg voor extra manifestinhoud, maar een NetworkPolicy is een eigen resource en geen stukje van de deployment. Kan het niet, dan is dát het eerste dat gebouwd wordt, want de handmatige variant is precies hoe de storing van 10 juni ontstond (`project_incident_20260610_netpol`: één apply die het allow-all-masker liet vallen).

## 4. Accounts die niet aan een projectbestand hangen

Nieuwe eis, en het is er geen die je later kunt aanbouwen: **ZAD zelf moet kunnen mailen.** Dat is geen bijkomstigheid maar de reden dat twee trajecten stilstaan:

- `features/futures/keycloak-sso-bypass-voorkomen.md` fase 2: wachtwoord instellen en resetten voor lokale accounts, buiten Keycloak om, met een gemailde token. Dat document legt ook uit waarom een mailserver aan Keycloak knopen dit niet oplost.
- `plans/otp-en-verhoogde-rechten.md`: herstel bij verlies van het toestel via een gemailde link. Nu vervangen door handwerk met database- en AGE-toegang.

Het probleem is het datamodel. Elk account in het ontwerp hangt aan een project, en ZAD is geen project: er is geen projectbestand, dus geen plek om de configuratie te zetten en geen levenscyclus die het account opruimt.

**Voorstel: een platformaccount dat in de configuratie van de relay zelf staat**, niet in een projectbestand. Aangemaakt bij het opzetten van de relay, met zijn wachtwoord als SOPS-secret in de namespace van de relay, en gelezen door OPI zoals het zijn andere platformgeheimen leest.

Waarom niet een projectbestand `zad.yaml` verzinnen: dan bestaat er een project dat in de portal verschijnt, dat gebruikers in lijsten zien, waar de reconciliatie iets van vindt en dat iemand kan verwijderen. Een datamodel oprekken om één account kwijt te kunnen, levert een tweede soort project op die overal een uitzondering nodig heeft.

De prijs is dat er twee wegen naar een account zijn. Die prijs is te betalen op één voorwaarde: **één stuk code dat accounts aanmaakt, met twee aanroepers.** De manager kent alleen "maak een account met deze naam, dit afzenderadres en deze limiet"; wie dat vraagt (een projectverwerking of het opzetten van de relay) is zijn zaak niet. Wordt dat twee implementaties, dan lopen ze uit elkaar en is het platformaccount het account waar niemand naar kijkt.

**Bij het ontwerp van dat platformaccount hoort ook een strengere eis dan bij een projectaccount**, want het verstuurt wachtwoordreset-tokens. Wie die mail kan versturen of onderscheppen, kan een account overnemen. Dat vraagt in elk geval een eigen limiet en een eigen afzenderadres, zodat een fout in de projectkant niet aan deze mail komt, en het is de moeite waard te bepalen of dit account een aparte upstream-identiteit hoort te hebben.

## 4b. Het platformaccount volgt het bestaande patroon: beheerdersgeheim uit de infra

Herziening van het voorstel hierboven, en het is eenvoudiger. Ik schreef "een platformaccount dat in de configuratie van de relay zelf staat", maar daarmee bedacht ik een tweede soort account terwijl er al een patroon is voor precies dit.

Zo werken Keycloak, PostgreSQL en MinIO nu, en de relay hoort daarbij aan te sluiten:

1. **De infrastructuur zet de dienst klaar met een beheerdersaccount.** Het wachtwoord is een gegenereerd geheim uit de gedeelde secret-generatie (`@secret-gen:random:XX`), SOPS-versleuteld in de bootstrap van de component. Niemand typt het, niemand kent het, en het staat niet in een projectbestand.
2. **OPI praat met dat beheerdersaccount via een connector.** `opi/connectors/mail.py` doet voor de relay wat `connectors/keycloak.py` voor Keycloak doet: accounts aanmaken, wachtwoorden roteren, limieten zetten, opruimen.
3. **Elk account komt via die ene weg tot stand**, of het nu voor een project is of voor ZAD zelf.

Daarmee vervalt de vraag "waar hangt het ZAD-account aan" grotendeels: het is een gewoon account op de relay, aangemaakt door dezelfde manager, alleen aangevraagd door de opstart van het platform in plaats van door een projectverwerking. Het onderscheid zit in de AANROEPER, niet in het soort account, en dat was ook al de voorwaarde die ik eraan verbond.

Wat blijft staan uit punt 4: het ZAD-account verstuurt wachtwoordreset-tokens en verdient daarom een eigen limiet en een eigen afzenderadres, zodat een fout aan de projectkant er niet aan komt. En het geheim van dat account is platformdata (punt 5), net als het beheerdersgeheim zelf.

Twee dingen om bij het bouwen te controleren, want hier zit de valkuil van dit patroon: het beheerdersgeheim moet te roteren zijn zonder dat elk projectaccount opnieuw moet worden aangemaakt, en OPI moet zich gedragen als de relay er nog niet is bij het opstarten. Dat laatste is bij Keycloak een bekende bron van opstartvolgorde-ellende.

## 4c. Het wachtwoord van het ZAD-account komt ook uit de bootstrap

Verbetering op 4b, en hiermee vervalt het kip-ei-probleem helemaal.

De bootstrap genereert TWEE geheimen: dat van de beheerder en dat van het ZAD-mailaccount, allebei SOPS-versleuteld. OPI leest het tweede, logt met het eerste in op de management-API, en maakt het account aan MET dat vooraf bekende wachtwoord.

Daarmee is de vraag uit punt 4 ("waar schrijft OPI een zelf gegenereerd geheim naartoe") van tafel: het geheim bestond al voordat het account bestond, en het overleeft een herstart omdat het gewoon in de bootstrap staat. Rotatie is dezelfde handeling als aanmaken: wijzig het SOPS-geheim, en OPI zet dat wachtwoord opnieuw op het bestaande account. Dat maakt het meteen idempotent -- bestaat het account al, dan zorg je dat het wachtwoord klopt in plaats van een tweede aan te maken.

**De aanname die eerst geverifieerd moet worden:** de management-API van Stalwart moet toestaan dat je bij het aanmaken een wachtwoord MEEGEEFT in plaats van er een terug te krijgen. Bij een beheerders-API is dat vrijwel altijd zo, maar dit voorstel rust erop, dus het hoort in de eerste bouwstap getoetst te worden en niet halverwege ontdekt.

Voor PROJECTaccounts blijft het wachtwoord wel runtime-gegenereerd en gaat het naar de projectsecrets: die accounts komen en gaan met hun project, en er is geen bootstrap die per project iets kan klaarzetten.

## 4d. Netwerkbeleid: ZAD moet zelf ook bij de relay kunnen

Het plan beschreef alleen het pad van projectnamespaces naar de relay. Er is een tweede, en die wordt gemakkelijk vergeten omdat hij niet uit een projectbestand volgt: **OPI zelf moet bij de relay kunnen**, vanuit `rig-prd-operations` naar `rig-prd-ron`, en wel voor twee dingen:

1. de **management-API**, want daarmee maakt OPI accounts aan, zet hij wachtwoorden en limieten en ruimt hij op;
2. de **submissiepoort**, want ZAD verstuurt zelf mail (uitnodigingen, en straks de wachtwoordreset- en OTP-hersteltokens uit de twee trajecten die hierop wachten).

Beide kanten moeten kloppen: uitgaand vanuit de namespace van OPI, en inkomend toegestaan in de namespace van de relay. Zonder dat werkt de dienst voor projecten wel en voor het platform zelf niet, en dat valt pas op bij de eerste uitnodiging die niet aankomt.

Dit beleid hoort bij de infrastructuur van de relay en niet bij de dienst: het geldt altijd, ongeacht of een project de dienst aanzet. Het beleid dat per project ontstaat (punt 3) komt wel uit de dienst.

## 5. De accountgegevens zijn platformdata

Sinds 15 augustus geldt in de API de regel dat configdata die OPI zelf zet niet via de API te wissen of te wijzigen is; een dienst declareert dat met `platform_managed_fields`. Het SMTP-wachtwoord en de accountnaam vallen daaronder: die worden door de mailmanager geschreven, niet door een gebruiker. Neem die declaratie mee in het configmodel, dan is het meteen goed in plaats van een reparatie achteraf zoals bij het realm-wachtwoord van Keycloak.

## Wat deze aanvulling niet verandert

De identiteitsregels, het afzenderdomein, de limieten, de bouwlijst voor OPI en de gefaseerde uitrol blijven zoals ze hierboven staan. Openstaande beslissing 5 (de servicenaam) blijft open; mijn voorkeur is nog steeds `send-email`.

## 6. Het gebruik van de mailrelay loopt via goedkeuring

Toegevoegd na het shippen, en het is geen detail aan de rand: **een project dat de maildienst aanzet, krijgt pas iets als een beheerder dat heeft goedgekeurd.**

Dat gaat via de generieke goedkeuringsweg die er inmiddels is, dezelfde die publish-on-web voor domeinen en subdomeinen gebruikt: de dienst declareert zijn goedkeuring met een `ApprovalSpec` in `config_approvals(...)`, en beantwoordt `ensure_approval_requests()` zodat het aanzetten van de dienst de aanvraag aanmaakt. Geen tweede mechanisme, geen eigen scherm: de aanvraag verschijnt in de bestaande beheerdersinterface en wordt daar afgehandeld, en de wachtstand komt via dezelfde weg terug in de API als bij een domeinaanvraag.

Het gedrag per status is expres saai:

| Status | Wat er gebeurt |
|---|---|
| geen aanvraag / in behandeling | **niets.** Geen account op de relay, geen netwerkbeleid, geen credentials in de projectsecrets. |
| afgewezen | hetzelfde: niets. |
| goedgekeurd | het account wordt aangemaakt, het netwerkbeleid komt erbij, de variabelen komen in de secrets. |

Er is dus geen half werkende tussentoestand, en dat is bewust. Een account dat wel bestaat maar niet mag mailen, of een netwerkbeleid zonder account, is een toestand die niemand kan uitleggen en die bij het opruimen wordt vergeten. Alles of niets.

Twee dingen om bij het bouwen scherp te houden. Het intrekken van een goedkeuring hoort hetzelfde pad te volgen als een projectverwijdering, anders blijft er een weesaccount op de relay achter. En de wachtstand moet zichtbaar zijn in het projectscherm en in de API, want een dienst die aanstaat en niets doet zonder dat iemand het ziet, is precies de klasse fout die we vandaag bij de domeinaanvraag hebben weggehaald.

## 7. De identiteitsregels zijn gemeten, en drie ervan stonden er niet in

Toegevoegd 15 augustus 2026, na de securityreview op PR #113. Stap 4 van de uitrol
("identiteitsregels aanzetten, verifieerbaar met een testbericht") is uitgevoerd - niet
tegen RON, want stap 2 staat nog open, maar tegen een echte Stalwart v0.11.8 met een
nep-upstream die de afgeleverde post bewaart. Dat had eerder gemoeten: de configuratie die
er lag, **laadde niet eens**.

Wat de proef aan het licht bracht:

| Wat het bestand zei | Wat Stalwart deed |
|---|---|
| `[session.data.remove-headers]` verwijdert Received, X-Mailer, X-Originating-IP | die sleutel bestaat niet; de verklikkers gingen ongemoeid naar buiten |
| regel 2 (From vastzetten) in een comment boven `[session.data.add-headers]` | geen enkele controle op de From:; `From: attacker@vreemd-domein.nl` vertrok, ondertekend met onze DKIM-sleutel |
| `message-id = true` dekt regel 6 af | die voegt er alleen een toe als hij ONTBREEKT; het Message-ID met de podnaam erin bleef staan |
| `[session.throttle]` regelt de limieten | die sleutel bestaat niet in v0.11; er was geen enkele limiet |
| `mechanisms = ["PLAIN","LOGIN"]`, `directory = "internal"`, `next-hop = "upstream"` | drie parse-fouten bij het opstarten: het zijn EXPRESSIES, dus `"[plain, login]"` en `"'internal'"` |
| `sign = [{if = "is_local_domain", ...}]` | parse-fout; er werd niets ondertekend |
| `image: stalwartlabs/mail-server:v0.11.5` | die tag bestaat niet (v0.11.4 -> v0.11.6): ImagePullBackOff |

De les die de tak zelf al opschreef maar niet toepaste: **een sleutel die Stalwart niet
kent wordt stil genegeerd.** Er komt geen fout, de regel doet niets, en het bestand leest
alsof alles geregeld is. Vandaar dat elke regel in de configmap nu de gemeten uitkomst bij
zich draagt.

Wat er nu echt staat, en hoe het is aangetoond:

1. **Envelope** - een applicatie die `MAIL FROM:<noreply.ander@...>` aanbiedt, levert bij
   de upstream `MAIL FROM:<bounce+demo@mail.rijksapp.nl>` af. De MAIL FROM van de
   applicatie wordt weggegooid, niet getoetst; dat is meteen de sterkste vorm.
2. **From** - een sieve-script op de DATA-fase (de enige plek waar v0.11 kopregels kan
   aanraken) eist dat het adres in de From: het adres van dit account is. Vreemd domein:
   `550`. Adres van een ander project: `550`. Eigen adres met eigen weergavenaam: komt
   aan. Daarvoor draagt het afzenderadres nu de accountnaam
   (`noreply.project-<project>@<maildomein>`) - de relay kent zijn accounts, dus de regel moet uit
   de accountnaam af te leiden zijn. Een adres bestaat trouwens maar EEN keer op de hele
   relay, dus een gedeelde `noreply@` was sowieso niet houdbaar.
2a. **Precies EEN From-adres** (toegevoegd na de securityreview van 15 augustus, r8).
   `address :all` is waar zodra EEN adres in de header matcht, dus
   `From: <invoice@evil.example>, noreply.project-demo@<maildomein>` haalde regel 2 en
   vertrok met onze DKIM-handtekening op naam van het slachtoffer. Het script eist nu
   `address :count "eq" ... "1"`. Gemeten: twee mailboxen -> 550, een mailbox -> afgeleverd.
3. **DKIM** - de afgeleverde post draagt `DKIM-Signature: ... s=zad; d=<maildomein>` met
   From in de `h=`-lijst.
5. **Received** - geen keten in de afgeleverde post. RFC 5293 raadt implementaties aan
   `deleteheader "Received"` te weigeren, dus deze regel had stil niets kunnen doen; de
   tegenproef (dezelfde relay zonder de vijf `deleteheader`-regels) levert Received,
   X-Originating-IP, X-Mailer, X-Originating-Client en X-Authenticated-Sender wel bij de
   upstream af. Stalwart v0.11.8 weigert het dus niet.
6. **Message-ID** - `<12345@app-pod-7f9c.rig-prd-demo.svc.cluster.local>` komt aan als
   `<12345@mail.rijksapp.nl>`. Het unieke deel blijft, het interne domein gaat eraf.
   Weggooien alleen kan niet: `add-headers` draait VOOR het script, dus dan vertrekt het
   bericht zonder Message-ID.
7. **Verklikkers** - `X-Mailer` en `X-Originating-IP` zijn weg bij de ontvanger.

**Een limiet per account bestaat niet in v0.11.** De management-API weigert een
`limits`-veld op een principal ("JSON deserialization failed"), dus het per-projectgetal
is de vastgelegde begroting en de relay dwingt een plafond af dat voor elk account gelijk
is (`queue.limiter.inbound.account`, gelijk aan `MAX_MESSAGES_PER_DAY`). Gemeten met
`3/1d`: het vierde bericht krijgt `452 4.4.5 Rate limit exceeded`, een ander account merkt
er niets van. Wil een project echt zijn eigen getal afgedwongen zien, dan is dat een
nieuwe stap (een eigen limiter per account bij het aanmaken wegschrijven, of wachten op
een Stalwart-versie die het op de principal kent) en geen regel die je in een comment zet.

Twee dingen die de API ook anders doet dan de connector aannam, en die de dienst zonder
reparatie onbruikbaar maakten: een onbekend account geeft **200 met
`{"error":"notFound"}`** en geen 404 (dus las de connector "bestaat" en werkte bij in
plaats van aanmaken), en een account zonder **rol** wordt na een geslaagde authenticatie
alsnog geweigerd met `550 5.7.1 Your account is not authorized to use this service`.

### Drie dingen die de proef er nog uit haalde (r8)

- **Een platte accountnaamruimte.** Het platformaccount `zad-platform` was een geldige
  PROJECTnaam, en de accountnaam was de projectnaam kaal. Een project met die naam kon het
  account van ZAD overnemen (bijwerken van een bestaand principal) of het laten verwijderen.
  Projectaccounts heten nu `project-<project>`, en de projectweg weigert de platformnaam
  expliciet.
- **Het image negeert `args`.** Het entrypoint start altijd met
  `/opt/stalwart-mail/etc/config.toml` en genereert dat bestand als het ontbreekt: met
  alleen `args` zou de relay op een standaardconfiguratie draaien. Het deployment zet nu
  `command`.
- **Het maildomein moet als principal bestaan** voordat er een account met een adres erin
  kan worden gemaakt (200 + `{"error":"notFound","item":"<domein>"}`). De connector maakt
  het domein nu aan.

### Wat hiermee NIET is afgedekt

- De weg naar `rmrmail.rijksweb.nl` (stap 2). Ongewijzigd blokkerend.
- De management-API loopt binnen het cluster over **plain HTTP met Basic auth**. Het
  beheerderswachtwoord gaat dus base64 over het podnetwerk. Wat het vandaag inperkt is het
  NetworkPolicy: alleen de OPI-namespace mag poort 8080 aan. Wil je het echt dicht, dan
  hoort daar een certificaat op de listener, en dat is een eigen stap.
- **Submission heeft geen TLS** terwijl er PLAIN/LOGIN overheen gaat. Zelfde inperking,
  zelfde antwoord: hetzelfde certificaat lost beide op.
- Een **eigen afzenderdomein** werkt nog niet: het sieve-script kent alleen het
  platformmaildomein. Bij het bouwen van die flow hoort daar een regel bij.
