# Back-upbeleid ZAD

*Concept, 2 september 2026. Vast te stellen door de proceseigenaar. Dit document beschrijft wat ZAD toezegt over back-ups en wat een projecteigenaar zelf moet regelen. De techniek erachter staat in `docs/backup-architectuur-overzicht.md`; die wordt hier niet herhaald.*

Dit beleid geeft invulling aan BIO2 v1.3, beheersmaatregel 8.13 (back-up van informatie) en de daarbij horende overheidsmaatregelen 8.13.01 tot en met 8.13.04, en raakt 5.30 (ICT-gereedheid voor bedrijfscontinuïteit).

## 1. Wie waarvoor verantwoordelijk is

De BIO belegt 8.13 bij de dienstenleverancier en de proceseigenaar samen. Voor ZAD betekent dat het volgende, en die knip is de kern van dit document.

**ZAD levert het back-upmechanisme**: het maken, versleutelen, bewaren, opruimen en terugzetten van back-ups, en de bescherming van de back-upopslag. ZAD bepaalt niet wat een project waard is.

**De projecteigenaar bepaalt de behoefte**: of er back-ups gemaakt worden, hoe vaak, en of de in dit beleid genoemde terugvalwaarden voor dataverlies en hersteltijd volstaan voor zijn toepassing. Wie zwaardere eisen heeft, meldt dat, want dan is een andere inrichting nodig.

ZAD kent geen classificatie per project. Er is geen veld in het projectbestand waarin staat hoe kritiek of gevoelig de gegevens zijn. De waarden in paragraaf 5 zijn daarom generiek en niet risicogestuurd per project. Het identificeren van kritieke systemen (BIO 5.30.02) ligt bij de proceseigenaar en staat als openstaand punt in paragraaf 8.

## 2. Wat er wel en niet in de back-up zit

| Wat | Back-up |
|---|---|
| Persistente opslag (PVC's) van componenten | Ja |
| PostgreSQL-databases | Ja |
| MinIO-buckets die bij een deployment horen | Ja |
| Projectdefinities en gegenereerde manifesten | Niet via back-up, deze staan in Git bij GitHub, buiten het cluster |
| De MinIO-installatie zelf (accounts, policies) | Nee, deze wordt opnieuw aangemaakt vanuit het projectbestand |
| De back-upbestemming zelf | Nee, dit is het eindpunt van de keten |

Dat de projectdefinities en manifesten buiten het cluster in Git staan is voor het herstel belangrijker dan het lijkt: de inrichting van het platform is daarmee herbouwbaar zonder back-up. Alleen de gegevens niet.

## 3. Back-ups zijn opt-in

Een deployment krijgt alleen automatische back-ups als de projecteigenaar een schema instelt. Staat er geen schema, dan worden er geen back-ups gemaakt en is er bij verlies van gegevens geen herstelpad.

Dit is een bewuste keuze en geen omissie: ZAD draagt veel omgevingen die per definitie wegwerpbaar zijn (test, demo, tijdelijke doorloop) en waarvoor back-uppen alleen kosten oplevert. De keerzijde is dat de projecteigenaar zelf moet vaststellen dat zijn productieomgeving een schema heeft.

ZAD rapporteert welke deployments geen schema hebben, zodat dit een zichtbare keuze is en geen stilzwijgende. Die rapportage bestaat nog niet en staat in paragraaf 8.

## 4. Bewaartermijnen

Voor een deployment met een dagelijks schema geldt:

| Regel | Aantal |
|---|---|
| Laatste momenten | 30 |
| Dagelijkse momenten | 30 |
| Wekelijkse momenten | 4 |
| Maandelijkse momenten | 12 |

Deze regels gelden naast elkaar; een moment blijft bewaard zolang minstens één regel het aanwijst. In de praktijk komt dat neer op ongeveer een jaar historie, fijnmazig voor de recente periode en grofmazig voor het oudere deel.

Handmatig gemaakte back-ups vervallen niet vanzelf. Die blijven staan tot iemand ze expliciet verwijdert.

Wordt een heel project verwijderd, dan wordt de bijbehorende back-upopslag gemarkeerd voor uitgestelde verwijdering met zeven dagen speling, zodat een vergissing terug te draaien is.

## 5. Maximaal dataverlies en maximale hersteltijd

BIO 8.13.02 vraagt om een expliciete afweging van maximaal toegestaan dataverlies en maximale hersteltijd. Eén getal voldoet daar niet, omdat de hersteltijd volledig afhangt van wat er stuk is.

**Maximaal dataverlies (RPO): 24 uur**, voor deployments met een dagelijks schema. Dat volgt rechtstreeks uit de frequentie: in het slechtste geval gaat het werk van de laatste dag verloren. Voor deployments zonder schema is er geen toezegging.

**Maximale hersteltijd (RTO)**, per scenario:

| Scenario | Streefwaarde | Grond |
|---|---|---|
| Eén database, opslag of bucket terugzetten, platform verder gezond | 4 werkuren | Het pad is beproefd op een testcluster, de tijd is nog niet in productie gemeten |
| Een hele deployment of een heel project terugzetten | 1 werkdag | Schatting, nooit als geheel uitgevoerd |
| ZAD opnieuw inrichten op andere hardware, back-ups intact | 1 werkdag | Schatting, onder de drie voorwaarden hieronder |
| ZAD inrichten op een ander platformtype | Weken | Geen incidentscenario maar een project, zie `docs/een-nieuw-cluster-installeren.md` |
| Verlies of corruptie van de back-upopslag zelf | Geen toezegging | Zie paragraaf 8, dit is een erkend risico |

Bij het derde scenario horen drie voorwaarden die expliciet gemaakt moeten worden, omdat de streefwaarde zonder die voorwaarden niets waard is:

1. De sleutels waarmee de back-ups versleuteld zijn moeten beschikbaar zijn. Zonder de sleutel van een omgeving is de back-up onleesbaar, hoe intact de opslag ook is.
2. Het datavolume moet binnen de dag passen. Terugzetten gebeurt per project en er lopen maximaal twee herstelacties tegelijk; bij tientallen projecten is dat de bepalende factor, niet het opnieuw inrichten.
3. Er moet een controlelijst zijn waarmee per project vastgesteld wordt dat het terugzetten geslaagd is. Bij één database is dat een oordeel ter plekke, bij tientallen projecten op één dag is het de plek waar tijd en vergissingen ontstaan.

Deze streefwaarden zijn schattingen en geen gemeten waarden. Ze worden bijgesteld na de eerste hersteltest uit paragraaf 7.

## 6. Bescherming van de back-up, waaronder tegen ransomware

BIO 8.13.01 vraagt om speciale aandacht voor bescherming tegen ransomware en voor het behoud van de integriteit van de back-up. De maatregelen die ZAD daarvoor treft zijn de volgende.

**Versleuteling per project.** Elke back-up wordt versleuteld met een sleutel die wordt afgeleid uit de sleutel van de omgeving van dat project. Die sleutel staat nergens centraal opgeslagen en er bestaat geen lijst die kan uitlekken. Eén project kan de back-ups van een ander project niet lezen, ook niet met toegang tot de onderliggende opslag.

**Gescheiden opslag per project.** Elk project heeft een eigen back-upbewaarplaats. Er is geen gedeelde ruimte waarin een fout of aanval alles tegelijk raakt op inhoudsniveau.

**Meerdere generaties.** Doordat er dagelijkse, wekelijkse en maandelijkse momenten bewaard blijven, is er ook een schoon terugzetpunt als een besmetting pas na enige tijd wordt opgemerkt. Een aanval die alleen de meest recente gegevens raakt, raakt niet de hele reeks.

**Onveranderlijke opslag.** De back-upbestemming krijgt object lock, zodat een back-up binnen zijn bewaartermijn niet verwijderd kan worden, ook niet door iemand met beheerdersrechten op die opslag. Dit is de maatregel die het verschil maakt tussen versleuteling en werkelijke bescherming, want ransomware hoeft de gegevens niet te kunnen lezen om schade aan te richten, alleen te kunnen wissen.

**Scheiding van schrijven en verwijderen.** Het proces dat een back-up maakt krijgt alleen het recht om te schrijven. Alleen het opruimproces, dat centraal draait en niet in de omgeving van een project, krijgt het recht om te verwijderen. Daarmee levert een compromittering van een applicatie geen mogelijkheid op om back-ups te wissen.

**Signalering.** Uitblijvende of mislukte back-ups worden gesignaleerd, evenals een onverwachte daling van het aantal bewaarde momenten. Dat is in de praktijk vaak het eerste zichtbare spoor van een aanval.

**Integriteitscontrole.** De back-upbewaarplaatsen worden periodiek gecontroleerd op consistentie. De opslagvorm is inhoudsgeadresseerd, waardoor stille wijziging of beschadiging detecteerbaar is.

De laatste vier maatregelen zijn belegd en nog niet geïmplementeerd. Ze staan met eigenaar in paragraaf 8. Dit beleid beschrijft ze hier als vastgestelde eis, niet als bestaande situatie.

## 7. Herstellen en testen

Terugzetten gebeurt vanuit het portaal. Voor persistente opslag wordt geen bestaande gegevensverzameling overschreven: er wordt een nieuwe generatie weggeschreven en de omgeving schakelt daarnaar om via de normale uitrolweg. Een herstelactie is daarmee zelf terugdraaibaar, wat het risico van herstellen onder druk aanzienlijk verlaagt.

BIO 8.13.04 en 5.30.01 vragen om een test, minimaal jaarlijks of na een grote wijziging. ZAD legt dat als volgt vast:

- De herstelprocedure wordt minimaal jaarlijks getest, en daarnaast na elke grote wijziging in de back-up- of herstelketen.
- De test wordt uitgevoerd op een niet-productieomgeving, met een echte terugzetactie en een inhoudelijke controle van het resultaat, niet alleen van de statusmelding.
- De test levert een kort verslag op met de gemeten hersteltijd, zodat de streefwaarden uit paragraaf 5 op meting gaan rusten in plaats van op schatting.

Onderdelen van de keten zijn al beproefd op een testcluster, met inhoudelijke controle op het resultaat. Wat nog niet is gedaan: een test die een volledig project omvat, en een gemeten hersteltijd.

## 8. Openstaande punten

Deze punten zijn erkend, belegd en nog niet afgerond. Ze staan hier omdat een beleid dat ze verzwijgt geen beleid is.

| Punt | BIO | Eigenaar | Status |
|---|---|---|---|
| Back-upopslag in een eigen faaldomein. De bestemming staat nu op dezelfde opslaglaag als de productiegegevens, waardoor verlies van die laag beide raakt | 8.13.03 | Platformteam | Loopt, gesprek over opslag buiten het cluster |
| Onveranderlijke opslag (object lock) op de back-upbestemming | 8.13.01 | Platformteam | Belegd, planning volgt; hangt samen met de herinrichting van de opslag |
| Scheiding van schrijf- en verwijderrechten in de back-upketen | 8.13.01 | Platformteam | Belegd, planning volgt |
| Signalering op uitblijvende en mislukte back-ups | 8.13.01 | Platformteam | Belegd, planning volgt |
| Periodieke integriteitscontrole van de back-upbewaarplaatsen | 8.13.01 | Platformteam | Belegd, planning volgt |
| Bewaarplaats buiten het cluster voor de sleutels waarmee back-ups versleuteld zijn | 8.13.01 | Platformteam | Belegd, planning volgt |
| Rapportage over deployments zonder back-upschema | 8.13.02 | Platformteam | Belegd, planning volgt |
| Eerste volledige hersteltest met gemeten hersteltijd | 8.13.04, 5.30.01 | Proceseigenaar | Belegd, planning volgt |
| Identificatie van kritieke systemen en classificatie per project | 5.30.02, 5.12 | Proceseigenaar | Niet belegd |

## 9. Herziening

Dit beleid wordt jaarlijks herzien, en daarnaast zodra de back-upopslag wordt heringericht of de scenario's uit paragraaf 5 wijzigen. De uitkomst van de hersteltest uit paragraaf 7 is aanleiding om de streefwaarden bij te stellen.
