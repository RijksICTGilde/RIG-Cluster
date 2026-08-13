# Het tabblad Toegang

De blokken die de diensten zelf op de projectpagina leveren staan op een eigen tabblad:
**Toegang**, tussen Services en Deployments. Adres: `/projects/<naam>/toegang`.

## Wat er op staat

Alles wat een dienst je aanreikt om hem te GEBRUIKEN. Op dit moment drie diensten:

| Dienst | Wat het blok toont |
|---|---|
| Keycloak | realm, adres van de admin-console met een knop erheen, admin-gebruikersnaam, admin-wachtwoord (afgeschermd) en de gedeelde OTP-code |
| Uitnodigingen | per sleutel de volledige uitnodigingslink, de toegekende rollen en het contactadres |
| Bijlagen | de geuploade bestanden (certificaten en dergelijke) die je per component koppelt, met toevoegen en verwijderen |

Ze stonden onderaan Overzicht, tussen de rest van de detailpagina. Daar vielen ze weg,
terwijl je er juist naartoe gaat als je iets nodig hebt: een adres om naartoe te gaan, een
wachtwoord om mee in te loggen, een code om in te vullen.

## Waarom "Toegang" en niet iets anders

Het tabblad **Services** ernaast gaat over BEHEER: welke diensten staan aan, wat doen ze,
hoe zijn ze gebonden, waar zet je ze aan of uit. Toegang gaat over GEBRUIK. Dat zijn twee
verschillende vragen, en daarom twee tabbladen.

"Services gebruik" was de eerste ingeving en is afgevallen: gebruik leest als verbruik of
kosten, en dat is het niet. De twee andere kandidaten, met de reden dat ze het niet zijn
geworden:

* **Servicegegevens** - het meest letterlijk juist, en precies daarom onbruikbaar: hij
  staat in dezelfde tabbalk naast "Services" en die twee zijn dan niet uit elkaar te
  houden. Een tabbalk moet je in een oogopslag kunnen lezen.
* **Aansluiten** - een werkwoord tussen zeven zelfstandige naamwoorden, en het klopt niet
  voor alle drie: een uitnodigingslink en een Keycloak-wachtwoord sluit je op aan, een
  geupload certificaat niet.

"Toegang" is gekozen omdat het alle drie de blokken dekt zonder een van de drie te
verdraaien: het Keycloak-blok is toegang tot je realm, de uitnodiging is toegang voor
iemand anders, en een bijlage is het materiaal waarmee je component ergens bij komt. Hij
blijft ook staan als de vierde dienst zich meldt - een MinIO-endpoint met sleutel, een
databaseverbinding - want dat is hetzelfde soort gegeven.

De naam staat bij de tabbladen zelf, in `opi/web/lotc_switch.py` (`PROJECT_TABS`).

## Geen leeg tabblad

Levert geen enkele dienst van het project een blok, dan is er geen tabblad Toegang: hij
valt uit de tabbalk. Een tab die een lege pagina opent is een belofte die niet
waargemaakt wordt.

Wie er via een gedeelde link toch op uitkomt, wordt naar Overzicht verwezen (302) in
plaats van op een lege pagina te belanden. De regel staat op een plek
(`TABS_MET_VOORWAARDE` in `opi/web/lotc_switch.py`), zodat de tabbalk en de route hetzelfde
zeggen.

## Generiek, geen dienstnamen in het sjabloon

Het tabblad noemt geen enkele dienst met naam. Elke dienst levert zijn blok via
`@on(UIEvent.PROJECT_SECTIONS)` (zie `features/service-owned-detail-page-blocks.md`), de
route verzamelt ze met `collect_detail_page_sections()` en het sjabloon rendert de lijst.
De vierde dienst die iets te tonen heeft hoeft dit sjabloon dus niet aan te raken; met drie
diensten is dat de moeite waard. Een tabblad PER dienst - de afweging uit RC-100 - is pas
nodig als een dienst een eigen indeling wil in plaats van een blok.

## Adressen

* `/projects/<naam>/toegang` - het tabblad.
* `/projects/toegang/<naam>` - de oude vorm (tabblad voor de projectnaam), verwijst door
  naar het adres hierboven, net als bij de andere tabbladen. Beide vormen staan letterlijk
  als route geregistreerd; een van de twee als wildcard registreren zou de andere opvangen
  met de projectnaam op de verkeerde plek.

## Tests

* `tests/test_lotc_toegang_tabblad.py` - het pad, de route, en de tabbalk die het lege
  tabblad weglaat.
* `tests/e2e/test_lotc_toegang_tabblad.py` - op de draaiende pagina: het blok staat op
  Toegang, staat NIET meer op Overzicht, en een project zonder blokken heeft geen tab en
  wordt van dat adres doorverwezen.
