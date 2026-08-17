# Het tabblad Services info

De blokken die de diensten zelf op de projectpagina leveren staan op een eigen tabblad:
**Services info**, tussen Services en Deployments. Adres: `/projects/<naam>/services-info`.

## Waarom het zo heet

Het heette eerst "Toegang". Dat beschreef wat de eerste drie diensten toevallig tonen (een adres, een wachtwoord, een code) in plaats van wat het tabblad is, en het loopt scheef zodra een dienst hier iets anders neerzet. Wat het tabblad draagt is de haak `detail_page_sections`: elke dienst mag er zelf een blok op zetten. De naam noemt daarom de diensten en niet het onderwerp van de eerste drie.

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

## De velden zien eruit als "Configuratie & Secrets"

Het Keycloak-blok zette elke waarde in een eigen omkaderd subblok, met een codeblok en een
losse tekstknop "Kopieer" ernaast. Drie kaders in elkaar (paneel > realm > veld), en elk
veld anders dan het veld ernaast. Het volgt nu de vorm van het blok Configuratie & Secrets
op Overzicht:

* label boven, waarde in een `<c-secret-field>`, het klembord IN het veld;
* een onthul-oogje alleen bij wat echt geheim is: `revealed show-copy` voor het
  console-adres, het realm en de gebruikersnaam, en `show-copy` zonder `revealed` voor het
  wachtwoord en de OTP;
* geen subkaders en geen losse kopieerknoppen.

Uitzondering: **Open Admin Console** blijft een knop, want dat is een actie en geen waarde.

### De gedeelde OTP is ook een veld

De OTP was een kop met twee regels uitleg en een knop "Toon code", die de code met htmx
ophaalde bij `/projects/<p>/keycloak/<realm>/otp-code`. Hij is nu een veld zoals het
wachtwoord ernaast; het endpoint en zijn fragment hadden daarmee geen aanroeper meer en
zijn verwijderd.

De code wordt bij het RENDEREN afgeleid (`totp_now`, in dezelfde lus die het wachtwoord
ontsleutelt) en ververst niet vanzelf. De hulptekst onder het veld zegt dat: hij verloopt
binnen 30 seconden en de pagina opnieuw laden geeft een verse code. Een code die stilletjes
verlopen is, is erger dan een code die zegt dat hij oud is.

Wat NIET verandert is de eigenschap waarvoor het endpoint ooit gemaakt is: de **seed**
bereikt de pagina nooit. De seed geeft voor altijd codes, deze code vergaat binnen een
periode. Wie hem kan zien mag ook het admin-wachtwoord zien - hetzelfde blok, dezelfde
rolpoort (alleen admin/owner) - en dat wachtwoord is van de twee het langstlevende.

## Waarom "Services info" en niet iets anders

Het tabblad **Services** ernaast gaat over BEHEER: welke diensten staan aan, wat doen ze,
hoe zijn ze gebonden, waar zet je ze aan of uit. Services info gaat over GEBRUIK. Dat zijn twee
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

"Services info" is gekozen omdat het alle drie de blokken dekt zonder een van de drie te
verdraaien: het Keycloak-blok is toegang tot je realm, de uitnodiging is toegang voor
iemand anders, en een bijlage is het materiaal waarmee je component ergens bij komt. Hij
blijft ook staan als de vierde dienst zich meldt - een MinIO-endpoint met sleutel, een
databaseverbinding - want dat is hetzelfde soort gegeven.

De naam staat bij de tabbladen zelf, in `opi/web/lotc_switch.py` (`PROJECT_TABS`).

## Geen leeg tabblad

Levert geen enkele dienst van het project een blok, dan is er geen tabblad Services info: hij
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

* `/projects/<naam>/services-info` - het tabblad.
* `/projects/services-info/<naam>` - de oude vorm (tabblad voor de projectnaam), verwijst door
  naar het adres hierboven, net als bij de andere tabbladen. Beide vormen staan letterlijk
  als route geregistreerd; een van de twee als wildcard registreren zou de andere opvangen
  met de projectnaam op de verkeerde plek.

## Tests

* `tests/test_lotc_toegang_tabblad.py` - het pad, de route, en de tabbalk die het lege
  tabblad weglaat.
* `tests/e2e/test_lotc_toegang_tabblad.py` - op de draaiende pagina: het blok staat op
  Services info, staat NIET meer op Overzicht, en een project zonder blokken heeft geen tab en
  wordt van dat adres doorverwezen.
