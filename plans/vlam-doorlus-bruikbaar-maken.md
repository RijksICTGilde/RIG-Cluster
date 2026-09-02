# De vlam-service maakt het doorlus-pad bruikbaar

## Waarom

Poort 8443 op `vlam-proxy-intern` lust door zonder te termineren. De afnemer zet zelf de TLS-sessie met VLAM op en valideert zelf het certificaat, waardoor ZAD het verkeer niet meer ziet. De poort ligt er, maar hij is nog niet bruikbaar zonder handwerk, en dat handwerk is precies het soort dat afnemers verkeerd doen.

Er zijn drie dingen nodig, en ze zijn alle drie waardeloos zonder de andere twee. Daarom staan ze in één plan.

| wat | waarom | zonder dit |
|---|---|---|
| het **adres** van de doorlus-poort | de afnemer moet weten waar hij heen moet | hij kent 8443 niet |
| de **naam** die naar onze proxy wijst | TLS vergelijkt de hostnaam uit de URL met het certificaat | certificaatfout op elke verbinding |
| het **CA-certificaat** in de pod | hij verifieert nu zelf, en kent de uitgever niet | onbekende uitgever, verbinding faalt |

Alle drie worden door dezelfde dienst op dezelfde Deployment gezet, op hetzelfde moment. De dienst koppelt zich al aan een component en injecteert daar de omgevingsvariabele en de NetworkPolicy-peer; dit zijn drie dingen uit dezelfde hand.

## Ontwerpbeslissingen, met de reden erbij

**Het certificaat is van het platform, niet van het project.** Het is voor elke afnemer identiek en verandert alleen als VLAM van uitgever wisselt. Daarom levert de dienst het en wordt het geen bijlage. Als bijlage zou hetzelfde publieke bestand in elk projectbestand terechtkomen, AGE-versleuteld, en zou een rotatie betekenen dat iedereen opnieuw moet uploaden. Levert de dienst het, dan is rotatie een wijziging op één plek.

**Wel de vocabulaire van attachments overnemen, niet de catalogus.** Attachments kennen al `provide-as: file`, `path` en `env-name`, en `attachment_secret_mounts` is de bestaande weg van Secret naar volume naar `volumeMount` in `manifests/deployment.yaml.jinja`. Die machinerie hergebruiken we, zodat er geen tweede manier ontstaat om een bestand in een pod te krijgen. De catalogus niet, want daar hoort een door de gebruiker geüpload bestand in.

**Niet het clusterbrede `ca_certificate`-mechanisme gebruiken.** Dat bestaat in `cluster_config.py` met `node_path`, `container_path` en `env_vars`, en zet die op elke Deployment op het cluster. Het staat alleen aan op het lokale Kind-cluster en mount via een `hostPath` van de node. Ongeschikt: te breed, en niet werkend op ODCN.

**De taalstandaard-variabelen NIET overschrijven.** Dit is de valkuil van dit plan. Het is verleidelijk om ook `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE` of `NODE_EXTRA_CA_CERTS` te zetten zodat het vanzelf werkt. Maar die **vervangen** de vertrouwensketen, ze vullen hem niet aan. Een pod met `SSL_CERT_FILE` op alleen de Rijksdienst Root CA vertrouwt verder niets meer: geen publieke API, geen Keycloak, geen pakketspiegel. Die storing ziet er totaal anders uit dan de oorzaak. De dienst kan geen betrouwbare samenvoeging maken omdat hij het basis-image van de afnemer niet kent. Dus: pad aanbieden, applicatie laat kiezen, en per taal documenteren hoe je een CA er **naast** zet.

**`hostAliases` neemt een IP-adres, geen naam.** Het mechanisme zelf is klein: een blok in de podspec dat een regel in `/etc/hosts` zet.

```yaml
hostAliases:
  - ip: 172.30.254.144
    hostnames: [vlam-api.overheid-i.nl]
```

Daar staat een adres en geen servicenaam, en de Service-template van ZAD zet geen `clusterIP`, dus dat adres is dynamisch toegewezen. **Besloten op 2026-09-01: we accepteren dat en gaan door.** Het adres komt als geconfigureerde waarde in `cluster_config.py` te staan, naast de andere `vlam`-instellingen, zodat er één plek is om het te wijzigen.

Waarom dat verantwoord is: een ClusterIP is onveranderlijk zolang de Service bestaat. Hij verschuift alleen als die Service wordt verwijderd en opnieuw aangemaakt, en dat gebeurt zelden. Gebeurt het toch, dan faalt het veilig: TLS valideert de hostnaam, dus verkeer dat bij een andere dienst uitkomt strandt op een certificaatfout in plaats van ergens verkeerd bezorgd te worden.

Wat we daarvoor terugkrijgen is dat er niets vastgezet hoeft te worden en het plan meteen kan beginnen. De prijs is dat het stilzwijgend kan verlopen, dus dat willen we kunnen zien. Twee latere mogelijkheden, geen van beide nu nodig: een controle die het geconfigureerde adres vergelijkt met de draaiende Service en klaagt bij verschil, of alsnog een vaste `clusterIP`, wat een kleine uitbreiding van ZAD vraagt.

Het alternatief, het adres opzoeken tijdens het genereren van de manifesten, is geen optie: gegenereerde manifesten staan in git en zouden dan stilzwijgend verouderen zonder dat iemand het ziet.

Overwogen en afgevallen: `vlam-api.overheid-i.nl` clusterbreed naar onze proxy laten wijzen. Dan hoeft er bij afnemers niets, maar dan kan onze eigen proxy zichzelf niet meer opzoeken en moet het echte VLAM-adres in onze backend hardgecodeerd worden. Dat is precies wat `resolvers dns` oplost, want VLAM is eerder van IP gewisseld en dat leverde 500'en op. Netto verplaats je het instabiele adres naar de plek waar het het meeste pijn doet.

## Naamgeving

Voorstellen, nog niet vastgesteld.

`VLAM_CA_BUNDLE_PATH` in plaats van `VLAM_CERTIFICATE_PATH`, want het is geen certificaat van VLAM of van ons maar de **uitgever** waartegen je VLAM's certificaat verifieert. Dat verschil is precies wat afnemers verkeerd begrijpen.

`VLAM_API_URL_DIRECT` voor de doorlus, naast het bestaande `VLAM_API_URL`.

Mountpad `/etc/ssl/vlam/rijksdienst-ca.pem`, bewust niet in `/etc/ssl/certs/`, want daar kijken sommige runtimes vanzelf en dan krijg je gedrag dat afhangt van het image.

## Stappen

1. **Het certificaat als platformgegeven vastleggen.** Eén bestand in de repo, plus een verwijzing onder de bestaande `vlam`-sleutel in `cluster_config.py`, zodat een cluster zonder VLAM ook geen CA aanbiedt. → verifieer: `get_vlam_config()` levert het pad, een cluster zonder `vlam` levert `None`
2. **De ClusterIP opschrijven.** Het huidige adres van de Service als geconfigureerde waarde onder de `vlam`-sleutel in `cluster_config.py`, één plek om te wijzigen. Niet vastzetten, dat is een bewust uitgesteld risico, zie hierboven. → verifieer: het geconfigureerde adres is gelijk aan het draaiende adres van `productie-vlam-proxy-intern`
3. **`vlam_endpoint()` levert drie dingen in plaats van één.** Het bestaande adres, het doorlus-adres en het IP voor de alias, alle drie afgeleid uit één configuratie-ingang zodat ze niet uit elkaar kunnen lopen. Dat is het bestaande argument van die module en het blijft gelden. → verifieer: bestaande test uitbreiden; een cluster zonder `vlam` levert nog steeds `None`
4. **De alias injecteren.** `hostAliases` op de Deployment van elk component dat `vlam` afneemt, die de VLAM-naam naar het geconfigureerde ClusterIP-adres wijst. → verifieer: gegenereerde manifesten bevatten het blok; een component zonder de dienst niet
5. **Het certificaat meemounten.** Een Secret per deployment langs de weg van `attachment_secret_mounts`, plus de `volumeMount`. → verifieer: manifest bevat volume, mount en Secret; inhoud is bytegelijk aan het bronbestand
6. **De twee variabelen erbij.** `VLAM_API_URL_DIRECT` en `VLAM_CA_BUNDLE_PATH` als extra `VariableDefinition` in `variables.py`. → verifieer: test op de variabelen van de dienst uitbreiden
7. **Downloadknop op de detailpagina.** Een endpoint bij het dienstblok, volgens "Endpoints belong with the block" in `instructions/services.md`. Voor wie lokaal wil testen of wil zien wat hij vertrouwt. → verifieer: levert een PEM met de juiste `Content-Type`, bytegelijk aan wat gemount wordt
8. **`help.md` herschrijven.** Wanneer kies je welke URL, wat is het verschil tussen termineren en doorlussen, en per taal hoe je een CA er naast zet. → verifieer: iemand die de dienst niet kent kan het doorlus-pad opzetten zonder te vragen

## Volgorde en wat je los kunt opleveren

Stap 2 levert het adres dat stap 4 nodig heeft, en stap 4 maakt 5 en 6 zinvol. Verder is er geen blokkade meer: het plan kan in volgorde worden uitgevoerd.

## Open vragen

- Wanneer wordt het geconfigureerde ClusterIP-adres een probleem, en willen we daar een controle op? Bewust uitgesteld, niet vergeten.
- Wisselt `vlam-api.overheid-i.nl` van uitgever? Op 2026-09-01 niet vastgesteld, want geen enkele pod in `rig-prd-vlam-wt8` heeft `openssl` of `curl`. Blijkt het een andere CA, dan wordt stap 1 een lijst in plaats van één bestand.
- Welke naam zet de alias, nu beide namen leven? Zolang `vlam-api.rijksweb.nl` en `vlam-api.overheid-i.nl` allebei werken is dat een expliciete keuze, geen bijproduct van volgorde.
- Moet de download ook zonder inloggen kunnen? Het is een publiek CA-certificaat, dus er valt niets te lekken, maar de detailpagina zit achter authenticatie.
