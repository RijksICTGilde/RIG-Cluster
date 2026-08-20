# RON-koppeling op ODCN

Referentiedocument voor de netwerkkoppeling tussen ODCN en het RON. Wat we hebben, wat ingress en egress in deze context betekenen, en hoe je ze aanzet.

We gebruiken deze koppeling voor de VLAM-API van SSC-ICT en voor de mailserver. Het bouwverslag van de VLAM-gateway staat in `vlam.md`, de onderbouwing in `features/futures/vlam-api-vpn-proxy.md`. Dit document beschrijft alleen de koppeling zelf.

## Het netwerkblok

Bron: status-update actie 328, wijziging Quattro.

| Blok | Waarvoor |
|---|---|
| `145.21.227.136/29` | het volledige gekoppelde blok |
| `145.21.227.136/30` | ingress, `.136` hangt aan de ingresscontroller |
| `145.21.227.140/30` | egress |

## Ingress en egress, wie initieert wat

Beide termen zijn vanuit ODCN gezien, niet vanuit de tegenpartij.

**Ingress** is verkeer dat bij ons binnenkomt: de ander opent de verbinding, het bestemmingsadres ligt in `145.21.227.136/30`. Onze ingresscontroller luistert daar.

**Egress** is verkeer dat wij naar buiten initiëren: onze pods openen de verbinding, en het bronadres dat de tegenpartij ziet komt uit `145.21.227.140/30` (SNAT via de egressgateway, het pod-IP is aan de andere kant niet zichtbaar).

Praktische vertaling: **wat je aan een externe partij doorgeeft om te allowlisten hangt af van wie de verbinding opzet.** Verbinden wij naar hen, zoals bij de mailserver, dan is dat voor ons egress en heeft de tegenpartij `145.21.227.140/30` nodig als bron-IP. Het ingressblok is dan niet relevant en noem je beter niet, dat levert alleen verwarring op. Geef het hele /30 door in plaats van één adres, want welk adres uit de pool het SNAT-proces pakt ligt niet bij ons vast.

## Welke adressen uit het blok komen er werkelijk langs

Dit is de vraag die de mailkoppeling op 15 augustus 2026 heeft opgehouden, dus hier staat hij uitgeschreven.

Een `/30` bevat vier adressen, en in een gerouteerd subnet doen er maar twee mee als host. De hostbits kennen twee combinaties met een eigen betekenis: alles op nul is het **netwerkadres**, de naam van het subnet zelf waarmee routers zeggen waar het blok heen gaat, en alles op één is het **broadcastadres**, bedoeld voor alle hosts in dat subnet tegelijk. Bij twee hostbits blijft er dus dit over:

| Adres | Rol in `145.21.227.140/30` |
|---|---|
| `.140` | netwerkadres, geen host |
| `.141` | bruikbaar |
| `.142` | bruikbaar |
| `.143` | broadcast, geen host |

Een netwerkadres als bron is betekenisloos, want er is niets dat kan antwoorden. Een broadcastadres als bron is erger: antwoorden zouden naar het hele segment gaan. Netwerkstacks weigeren die twee daarom als hostadres.

Waarom je dan toch een `/30` krijgt voor twee bruikbare adressen: het masker zegt niet hoeveel adressen je kunt gebruiken, maar hoe de ROUTERING het blok moet zien. Een `/30` is bovendien de klassieke maat voor een punt-tot-puntverbinding, precies wat een koppeling tussen twee organisaties is. (Zuiniger kan met een `/31`, RFC 3021, maar dat wordt lang niet overal gebruikt.)

**De kanttekening, en die is wezenlijk.** Bovenstaande geldt voor een echt gerouteerd subnet. Is het blok in werkelijkheid een TOEWIJZING voor NAT-pools en firewallregels, wat bij dit soort koppelingen vaak zo is, dan is `145.21.227.140/30` eerder een boekhoudkundige aanduiding voor "deze vier zijn van jullie" en kan `.140` wel degelijk als SNAT-bron dienstdoen. Twee lezingen van dezelfde notatie, allebei verdedigbaar. Alleen de logs van de tegenpartij zeggen welke hier waar is.

### Beantwoord op 17 augustus 2026: het is de NAT-lezing

ODCN heeft bevestigd dat **`145.21.227.140` het uitgaande adres is** waarmee het cluster de mailserver benadert, en dat is ook wat er in de toelating aan hun kant staat. Daarmee geldt hier de tweede lezing: het blok is een toewijzing voor NAT en firewallregels, geen gerouteerd punt-tot-puntsubnet, en `.140` doet gewoon dienst als SNAT-bron.

De redenering hierboven blijft staan omdat hij bij een volgend blok weer opgaat, maar **voor dit blok is de conclusie dus omgekeerd**: `.140` doorgeven was niet de fout. Wie hier eerder las dat `.141` en `.142` gevraagd moesten worden, leest nu dit.

Gevolg voor de mailkoppeling: het bronadres verklaart de storing niet meer. Zie het openstaande punt onderaan.

### Als een koppeling het niet doet

Vraag of ze in hun firewalllogs kunnen zien **welk bronadres** ze werkelijk van ons zien, en of de poging wordt gelogd als drop. Een firewall die verkeer laat vallen, logt dat meestal wel, en daarmee is het in één blik beslist. Wij kunnen het zelf niet meten: de namespace `quattro-egress-gateway` is van ODCN en niet leesbaar met onze rechten, en met `rig-ron` is er geen internetbestemming die ons bronadres kan terugvertellen.

## Egress aanzetten: annotatie op de namespace

```yaml
apiVersion: v1
kind: Namespace
metadata:
  annotations:
    egress.projectcalico.org/egressGatewayPolicy: rig-ron
```

Toegestane waarden zijn `internet` of een klantgateway (`rig-*`). Andere sleutels of ongeldige waarden worden door Kyverno geweigerd, en bij een foutieve annotatie wordt de namespace onbruikbaar.

**`rig-ron` vervangt `internet`, het komt er niet bij.** Een namespace die zowel internet als RON nodig heeft, kan dit niet zelf oplossen: de annotatie neemt één waarde. Daarvoor moet je bij Quattro zijn.

OPI zet standaard `internet` (`operations-manager/python/manifests/namespace.yaml.jinja:10`). Wijzigen naar `rig-ron` is nu een handmatige stap per namespace. De wijziging overleeft een refresh, want OPI raakt bestaande annotaties niet aan, maar bij een nieuwe namespace moet je het opnieuw doen. Zie de openstaande punten onderaan `vlam.md`.

## Ingress over RON aanzetten: label op de Route of Ingress

```yaml
kind: Route
metadata:
  labels:
    customer.odc-noord.nl/ingress-controller: rig-ron
```

Let op het verschil met egress: dit is een **label op het Route- of Ingress-object**, niet een annotatie op de namespace.

**Het label moet aanwezig zijn op het moment dat het object wordt aangemaakt.** Achteraf toevoegen werkt niet, dan moet het object opnieuw. Voor ons betekent dat: het hoort in de manifest-generatie van OPI thuis en niet in een handmatige `kubectl label` achteraf.

## Links

- Egress: https://docs.rijksapps.nl/egress-internet-traffic/
- Ingress: https://docs.rijksapps.nl/ingress/?h=ron#option-1-using-the-odc-noord-provided-wildcard-certificate

## Openstaand

- ZAD kan de egress-annotatie nog niet vanuit het projectbestand zetten, dus RON-namespaces vragen een handmatige stap.
- Het ingresscontroller-label wordt nergens door OPI gezet. Zolang dat zo is, kan een RON-ingress niet via ZAD worden opgeleverd, want achteraf labelen werkt niet.
- De mailkoppeling werkt. Er is voor DNS niets meer te doen, en dat is een wijziging ten opzichte van eerder op deze pagina: het afzenderdomein is `rijksoverheid.nl` geworden in plaats van een eigen `mail.rijksapp.nl`. Zie hieronder.

## De mailkoppeling, gemeten op 17 augustus 2026

Werkt. `rmrmail.rijksweb.nl` (`145.21.161.201`) neemt op **poort 25** een bericht aan vanuit een pod met `rig-ron`-egress:

```
220 rmrmail.rijksweb.nl ESMTP
250-8BITMIME
250-SIZE 31457280
250 STARTTLS
250 sender <zad@mail.rijksapp.nl> ok
250 recipient <...@rijksoverheid.nl> ok
250 ok:  Message 56754911 accepted
```

Vier dingen die het ontwerp raken:

1. **Poort 25, niet 587 of 465.** Die laatste twee zijn stil. Onze relay moet dus op 25 uitleveren.
2. **Geen `AUTH` in de EHLO-lijst, dus geen credentials.** Het is een IP-gebaseerde relay: wie vanaf `145.21.227.140` verbindt mag relayen. Dat verklaart ook waarom ze om ons uitgaande IP vroegen en niet om een accountnaam.
3. **`SIZE 31457280`, dus 30 MB.** Onze eigen berichtlimiet moet daaronder liggen.
4. **STARTTLS wordt aangeboden** en moet door de relay gebruikt worden. De testmeting hierboven ging plat, omdat er geen openssl in die pod zit.

**Beveiligingsgevolg, en dit is de belangrijkste uitkomst.** Omdat de upstream niet authenticeert, is ons eigen netwerkbeleid het enige dat de relay verplicht maakt. Elke pod die `145.21.161.201:25` kan bereiken, mailt buiten de relay om: zonder limiet, zonder From-policy, zonder DKIM en zonder log, met onze organisatie als afzender.

Het goede nieuws is dat die grendel er structureel al zit. De enige egressregel richting buiten staat hardgecodeerd in `manifests/tenant-baseline-network-policy.yaml.jinja` en laat alleen 443 en 80 door. Het veld `ports.outbound` in het projectbestand suggereert anders, maar wordt nergens in de manifestgeneratie naar een egressregel vertaald; het leeft alleen in de formulieren en in cross-domain-access, dat over verkeer binnen het cluster gaat. Een project kan poort 25 dus niet zelf openzetten.

Wat er dan wél moet gebeuren, is die eigenschap vastpinnen in plaats van hem te vertrouwen: **een regressietest die vastlegt dat de tenant-baseline nooit iets anders dan 443 en 80 naar `0.0.0.0/0` toestaat.** Zonder die test is dit een eigenschap die iemand er over een jaar in één regel uit haalt zonder te weten dat er een mailrelay op leunt.

## STARTTLS naar de upstream is een garantie, geen voorkeur

Open vraag bij het ontwerp: `[remote.upstream.tls]` staat op `implicit = false` met
`allow-invalid-certs = false`, maar de documentatie zegt nergens wat er gebeurt als
STARTTLS mislukt of het certificaat wordt afgekeurd. Valt Stalwart terug op platte tekst,
of mislukt de bezorging? Dat verschil bepaalt of dit een garantie is of alleen een voorkeur.

Gemeten op 19 augustus 2026 op de sandbox, tegen Stalwart v0.11.8, met de SMTP-sink als
upstream. **Stalwart valt niet terug op platte tekst.** In geen van beide gevallen vertrekt
er iets onversleuteld:

| Wat de upstream aanbiedt | Wat de relay doet |
|---|---|
| geen STARTTLS in de EHLO-lijst | `STARTTLS was not advertised by host`, **permanente** fout, bericht gebounced |
| STARTTLS met een certificaat dat niet valideert | `invalid peer certificate`, **tijdelijke** fout, bericht blijft in de wachtrij |

Het verschil tussen die twee is de moeite van het onthouden waard. Zou de upstream ooit
zijn STARTTLS verliezen, dan bouncet de post meteen en is dat luid zichtbaar. Verloopt zijn
certificaat, dan stapelt de wachtrij zich stil op tot hij is vernieuwd.

**En een tweede uitkomst, die productie raakt: Stalwart leest de trust store van het
besturingssysteem niet.** Een eigen CA in `/etc/ssl/certs/ca-certificates.crt` hangen
verandert niets aan de uitslag; de fout blijft `UnknownIssuer`. Hij gaat af op de
webpki-roots die in de binary zitten. Presenteert `rmrmail.rijksweb.nl` ooit een
certificaat van een interne CA in plaats van een publiek vertrouwde, dan is er geen knop
om die CA te vertrouwen en is de enige uitweg `allow-invalid-certs` - wat de controle in
zijn geheel uitzet. Dat is het scenario om in de gaten te houden bij een certificaatwissel
aan hun kant.

Daarom komt `allow-invalid-certs` sinds RC-140 uit de omgeving
(`MAIL_UPSTREAM_ALLOW_INVALID_CERTS`), met `"false"` in de basis van het Deployment. Omgezet
wordt hij door de component `mail/sink/as-upstream`, en dat is geen toeval: die component
levert de sink en de schakelaar samen, want de sink draagt een zelfondertekend certificaat
en kan per definitie niet door webpki-validatie komen. Wie de sink inlaadt, kan de
schakelaar dus niet vergeten. In de praktijk betekent dat: `local` en `sandboxed-local` -
de twee ontwikkelclusters, allebei met een sink - zetten hem om, en `odcn`, met de
mailserver van de Rijksoverheid als upstream en zonder sink, staat strikt.

## De afzender is `noreply-rijksapp+<project>@rijksoverheid.nl`

Vastgesteld op 18 augustus 2026, aangescherpt op 20 augustus (RC-145). Er komt geen eigen maildomein: we versturen via de mailserver van de Rijksoverheid, dus onze post draagt hun identiteit. Wat een project daarbinnen krijgt is een eigen plusdeel en een eigen weergavenaam:

```
From:         <from-name uit de projectconfiguratie> <noreply-rijksapp+<project>@rijksoverheid.nl>
Return-Path:  noreply-rijksapp+<project>@rijksoverheid.nl
```

De relay schrijft die hele `From:` zelf en gooit weg wat de applicatie meestuurde, naam en adres allebei; een applicatie kan er niet omheen en hoeft er niets voor te doen. De `Reply-To:` blijft wel van de applicatie. Houdt de relay voor een account geen afzender, dan is de terugval het kale `noreply-rijksapp@rijksoverheid.nl` zonder naam — dat is ook wat het platformaccount van ZAD zelf gebruikt, dat geen project is.

Waarom het niet anders kan, en dit is het stuk dat je moet onthouden:

```
_dmarc.rijksoverheid.nl   v=DMARC1; p=reject; adkim=r; aspf=r
rijksoverheid.nl          v=spf1 redirect=spf-a.ssonet.nl
```

`p=reject` betekent dat een bericht met `From: @rijksoverheid.nl` dat DMARC niet haalt, door de ontvanger wordt geweigerd. DMARC slaagt als het `From:`-domein uitlijnt met de envelope (SPF) of met de handtekening (DKIM). Wij kunnen niet ondertekenen, want daarvoor moet onze publieke sleutel in hún zone staan. **SPF-uitlijning is dus het enige been om op te staan**, en daarom is de envelope `noreply-rijksapp+<project>@rijksoverheid.nl`: sinds RC-145 exact hetzelfde adres als de `From:`, met het project in het plusdeel zodat een bounce herleidbaar blijft. Dat de twee eerder een voorvoegsel verschilden hielp de uitlijning niet — die kijkt naar het domein — en kostte de ontvanger het zicht op welk project schreef.

Wat dat oplevert: de hele DNS-post valt weg. Hun SPF autoriseert de uitgaande IP's van de upstream al, want de upstream is hun eigen infrastructuur.

Wat dat kost: er is geen tweede been. Gaat de envelope-herschrijving stuk, dan haalt geen enkel bericht nog DMARC en weigert elke ontvanger buiten de Rijksoverheid alles. Dat is een enkele faalpunt en het staat als zodanig in de relayconfiguratie beschreven.

En let op de valstrik die dit met zich meebracht: het envelope-adres op het relay-account zetten zou betekenen dat de relay `rijksoverheid.nl` als LOKAAL domein kent, en dan bezorgt hij mail áán collega's daar bij zichzelf in plaats van hem door te sturen. Daarom draagt een account geen adressen meer en staat `must-match-sender` uit.

## De mailmeting van 15 augustus was ongeldig

Op 15 augustus 2026 werd vanuit `rig-prd-vlam-wt8` gemeten dat poort 25, 587 en 465 op `rmrmail.rijksweb.nl` (`145.21.161.201`) alle drie in een timeout liepen, zonder banner en zonder weigering. Daaruit is geconcludeerd dat de upstream onbereikbaar was, met het bronadres als verdachte. Beide conclusies houden geen stand.

Op 17 augustus opnieuw gemeten, met hetzelfde beeld, plus twee ijkpunten die het beslissen: `chat.rijksweb.nl:443` en de VLAM-API antwoorden meteen, maar `chat.rijksweb.nl:9999` is óók stil. Een niet-toegestane poort valt dus stil weg naar een host die verder gewoon werkt.

De oorzaak zit in ons eigen netwerkbeleid. `productie-tenant-baseline-network-policy` in die namespace heeft `policyTypes: [Ingress, Egress]` en staat egress naar `0.0.0.0/0` alleen toe op poort 443 en 80. Bij Kubernetes is de rest daarmee verboden, want zodra er egressregels zijn geldt deny-by-default. **De SMTP-pakketten hebben de pod nooit verlaten.**

Wat dat betekent:

- Het bronadres `145.21.227.140` treft geen blaam, en de firewallregel aan hun kant evenmin. Die discussie was op een verkeerde meting gebouwd.
- Over de werkelijke bereikbaarheid van de mailserver weten we nog **niets**. Eén meetpunt kwam wel langs het beleid, namelijk poort 443 naar dat adres, en bleef stil, maar een mailserver hoeft daar niets te hebben staan.

Een geldige meting vraagt een pod die op 587 naar buiten mag. Alle drie de draaiende pods dragen het label `deployment=productie` en vallen dus onder dat beleid; de tweede policy in die namespace (`acme-http-productie-network-policy`) is `Ingress`-only en beperkt egress niet. Een pod **zonder** dat label heeft in die namespace dus vrije egress en gebruikt nog steeds de `rig-ron`-gateway, want die is een namespace-annotatie. Dat is de kortste weg naar een echt antwoord, en het is een productiewijziging, dus die vraag ligt bij de gebruiker.
- De eerdere bevinding dat ODCN geen route had naar `145.21.0.0/16` (VLAM en SMTP gaven "Network is unreachable") gaat over precies deze route. Na oplevering van het blok opnieuw testen vanuit een pod met `rig-ron`.
