# Een Keycloak-image met onze eigen providers erin

**Status**: plan, nog niets gebouwd.
**Datum**: 2026-09-22
**Aanleiding**: er is geen "release Keycloak" zoals er wel een "update operations-manager" is, en een vers gebouwde jar uitproberen kan vandaag niet zonder de gewenste toestand van productie te wijzigen. Een eerdere versie van dit plan zette de jars in git en leverde ze via een ConfigMap; dat is na tegenspraak vervangen, zie de afweging hieronder.

## Wat er nu staat

De Keycloak-pod haalt bij elke start drie jars op in een initContainer (`base/deployment.yaml:40-46`): het NL-design-thema van MinBZK (`v1.4.2`, niet van ons), onze saml-nameid-mapper (`v1.1.0`) en onze relay-emailSender (`1.0.0`).

Die derde is de uitzondering. Zijn jar staat in git en gaat als ConfigMap de pod in, met de reden erbij in `base/kustomization.yaml`: aan die jar hangt de startvlag `--spi-email-sender-provider`, Keycloak weigert te starten als de provider ontbreekt (gemeten, RC-158), dus zou hij van GitHub komen dan legt een hapering daar het inloggen van het hele platform plat. De andere twee komen nog wel met `wget` van een GitHub-release, zonder checksum. Er is dus geen ontwerp met twee wegen, er is één uitzondering en een restant.

Daarnaast staat de versie van de mapper hardgecodeerd op vier plekken in drie bestanden (`base/deployment.yaml:43,45`, `overlays/local/kustomization.yaml:77,78`, `overlays/local/patch-custom-mapper.yaml:21,22`), en delen sandbox en productie dezelfde pin: `overlays/sandboxed-local` en `overlays/odcn` nemen allebei kaal `../../base` en raken de providers niet aan. Iets proberen in de sandbox is daarmee hetzelfde als het uitrollen naar ODCN.

## Waarom een eigen image, en niet de twee andere wegen

De jars in git via een ConfigMap werkt voor 9,6 kB en is bewezen voor de relay-jar, maar het is een rare vorm om artefacten aan te bieden, en voor de mapper-jar is de omvang niet eens gemeten. Een plan dat op een ongemeten grens leunt, is geen plan.

Ze als packages op GitHub zetten met de URL in een ConfigMap is het huidige mechanisme netjes gemaakt. Het zet de versie op één plek per omgeving, maar houdt de afhankelijkheid bij het starten in stand: elke podstart, elke scale-up en elke node-verplaatsing haalt opnieuw bij github.com op. Voor een provider die Keycloak bij het starten eist is dat precies het risico dat in `base/kustomization.yaml` al is opgeschreven als reden om het niet te doen.

Een eigen image lost allebei op en past bij wat deze repo al doet. Er staan elf eigen images in `images/`, gebouwd en gepusht door `.github/workflows/docker-images.yml` met een CalVer-tag, een `sha-<commit>`-tag en `latest`, per image te kiezen. De pin per omgeving wordt dan een image-tag in de overlay, wat hier al het patroon is voor een deploybeslissing. Er wordt bij het starten niets meer opgehaald. De prijs is dat een Keycloak-upgrade ook een imagebouw wordt; de basisversie (`quay.io/keycloak/keycloak:25.0.6`) staat nu al in git, dus die upgrade was sowieso een wijziging hier.

## Wat we bouwen

**1. Een eigen image met de providers erin.** `images/keycloak-zad/` (naam is een voorstel), meerfasig: een Maven-fase bouwt onze twee jars uit `keycloak-migration/relay-email-sender` en `keycloak-migration/custom-mapper`, de eindfase gaat uit van `quay.io/keycloak/keycloak:25.0.6` en zet ze in `/opt/keycloak/providers/`. Daarmee is de image het artefact dat uit de bron komt, en kan een jar niet meer uit de pas lopen met zijn broncode. Het buildcontext is de repo-wortel, zoals `cmp-kustomize-sops` dat ook doet.

Het thema van MinBZK blijft een download, maar dan tijdens de bouw en met een checksumcontrole. Hapert GitHub, dan faalt de bouw, en dat is iets heel anders dan een pod die niet opstart.

Ga na of Keycloak zijn `kc.sh build` nodig heeft om providers op te nemen. Zo ja, dan hoort die stap in de Dockerfile, anders doet de pod dat werk bij elke start alsnog.

**2. Een job in `docker-images.yml`**, naast de bestaande, met dezelfde tagging (CalVer, `sha-<commit>`, `latest`) en dezelfde naamvorm `ghcr.io/rijksictgilde/zad/<naam>`. De `sha-`tag is wat je nodig hebt om "de jar die ik net gebouwd heb" in de sandbox te draaien zonder een versienummer te verzinnen.

**3. Een taak die dat lokaal doet voor de sandbox**: bouwen, in de sandboxcluster zetten, en de tag pinnen in de sandbox-overlay. De taak roept een script aan in plaats van het werk zelf te doen, zodat CI later hetzelfde script gebruikt.

**4. De pin staat per omgeving.** Base draagt de tag die productie draait. De sandbox-overlay pint zijn eigen tag. De odcn-overlay schrijft het pad in rcr-vorm, zoals de postgres-overlay dat doet met de uitleg erbij; zonder die vorm in git blijft ArgoCD eeuwig OutOfSync. Een proef in de sandbox raakt base en odcn dus niet.

**5. De initContainer verdwijnt**, samen met de `wget`-regels, het kopieerwerk, de `configMapGenerator` met de ingecheckte relay-jar, en de twee kopieën van diezelfde initContainer in de local-overlay.

**6. CI draait de tests van allebei de Java-pakketten.** De custom-mapper heeft er een (`UnrestrictedXPathAttributeMapperTest.java`) die vandaag nergens draait. De byte-voor-byte-controle op de ingecheckte jar vervalt met die jar; wat die controle bewaakte, namelijk dat bron en artefact niet uit elkaar lopen, wordt overgenomen doordat de image de jars zelf bouwt.

## Wat we niet doen

Keycloak zelf upgraden. De basisversie blijft `25.0.6`; dit plan gaat over hoe onze providers erin komen.

Het thema van MinBZK overnemen of in git zetten. Dat blijft een externe download, alleen verplaatst naar de bouw.

Automatisch promoveren naar productie. Een nieuwe tag in de odcn-overlay blijft een bewuste wijziging in git.

## Waar dit mis kan gaan

De relay-provider is verplicht bij het starten. Dat blijft zo, alleen verschuift de faalvorm van "GitHub is weg" naar "verkeerde image-tag", en dat laatste staat in git en is terug te draaien.

De andere images bouwen voor amd64 en arm64. De Maven-fase levert dezelfde jars voor allebei; let erop dat die fase niet per architectuur opnieuw draait zonder reden.

ODCN haalt images via de rcr-proxy. Staat het pad niet in die vorm in git, dan blijft de applicatie OutOfSync, en dat is eerder gebeurd.

## Regels voor uitbrengen

**De jars blijven eigen artefacten.** De image bundelt ze, hij vervangt ze niet. Elk van de twee modules houdt zijn eigen pom, zijn eigen versienummer en zijn eigen `mvn test`, en blijft los te bouwen met zijn bestaande taak. Wie aan de mapper werkt, bouwt en toetst de mapper, niet een Keycloak.

**Geen Keycloak-release zonder aanleiding.** Een nieuwe image-tag hoort bij een gewijzigde jar of een gewijzigde Keycloak-basisversie, en anders niet. De imagebouw blijft daarom een bewuste handeling per image, zoals de bestaande workflow dat al doet, en de taak zegt het als er niets te bouwen valt in plaats van er stilzwijgend een tag bij te maken.

**Productie volgt op de sandbox, met dezelfde tag.** Promoveren is het verplaatsen van precies de tag die op de sandbox heeft gedraaid, niet opnieuw bouwen; een herbouw levert een ander artefact op en dan is het bewijs van de sandbox niets waard. De odcn-overlay mag dus alleen een tag dragen die aantoonbaar op de sandbox heeft gedraaid.

**De GitHub-release van de mapper stopt.** Er worden geen nieuwe assets meer gepubliceerd zodra de image de bron is. De bestaande `v1.1.0` blijft staan, zodat een oud manifest of een terugval er niet op stukloopt; hij wordt alleen niet meer bijgewerkt.

## Klaar als

- In de sandbox draait een vers gebouwde image terwijl `kustomize build` van de odcn-overlay nog steeds de oude tag oplevert. Dit is de eigenlijke acceptatie; toets het met beide builds naast elkaar.
- De pod start zonder iets op te halen: `grep -rn wget` in de keycloak-map levert niets meer op, en er is geen provider-ConfigMap meer.
- Keycloak start met `--spi-email-sender-provider`. Dat is meteen de scherpste toets dat de providers echt in de image zitten, want hij weigert te starten als de provider ontbreekt.
- De jars in de image komen uit de bron in deze repo; er staat geen gebouwde jar meer in git.
- CI draait `mvn -B test` voor allebei de Java-pakketten, en kan de image bouwen en pushen.
- Een taak doet hetzelfde voor de sandbox, en productie blijft daarbij ongemoeid.
- Allebei de jars zijn nog los te bouwen en te toetsen met hun eigen taak, zonder dat daar een image aan te pas komt.
- Promoveren naar productie zet de tag die op de sandbox draaide, en bouwt niet opnieuw. Toets dat de odcn-overlay na een promotie exact de sandboxtag draagt.
- `features/` beschrijft de weg: bouwen, in de sandbox draaien, toetsen, promoveren naar productie.
