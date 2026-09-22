# Een eigen domein aanvragen loopt vast op het kale domein

Status: plan, 22 september 2026. Af en klaar om te shippen, NA RC-214. Dit plan is het contract: er staan geen open beslissingen in, en elke regelverwijzing staat tegen de tak van RC-214 (`de-gereserveerde-namen-gelden-alleen-op-onze-eigen`), want die wordt main voordat dit begint.

Kaartje: [RijksICTGilde/RIG-Cluster#179](https://github.com/RijksICTGilde/RIG-Cluster/issues/179), op board ZAD (project #5). Sluit die issue als dit uitgevoerd en uitgerold is.

**Waarom na RC-214 en niet ertegelijk.** Beide verbouwen de vololgorde van controles in dezelfde methode, `DomainConfigEnforcer.enforce()`. RC-214 verplaatst daar net het reserveringsblok naar voren; dit plan verbouwt het kaal-domein-blok erboven. Tegelijk werken levert een conflict op in dezelfde hunk en, erger, twee mensen die onafhankelijk aan dezelfde volgorde sleutelen. Bovendien kan dit plan pas naar de definitieve volgorde verwijzen als die vaststaat.

## Wat er gebeurde, gemeten in productie op 22 september 2026

De beheerder van `ubbw-0i1` wilde zijn eigen domein `uitbetrouwbarebron.nl` gebruiken, met het kale domein op component `documentatie`. Drie pogingen, alle drie geweigerd met dezelfde melding:

```
13:36:33 [req-04eda088] Wizard step ubbw-0i1/modal-edit-domain-1/domain-edit-1 did not advance:
  field_errors={'deployments[1]': ["Kaal domein is alleen beschikbaar voor een eigen domein:
  'uitbetrouwbarebron.nl' is niet goedgekeurd voor dit project."]} global_errors=[] warnings={}
```

In zijn woorden: "klikken op 'Volgende' ... werkt gewoon niet". Daarna, met het vinkje uit, ging de stap wél door en sloeg een niet-goedgekeurd domein op; publicatie viel stil terug op het clusteradres.

Dat de aanvraaghaak zelf werkt is bewezen: dezelfde stap zonder kaal domein en met het vinkje aan schreef om 12:35:06 UTC netjes een `requested`-regel weg, die om 12:35:51 UTC is goedgekeurd.

## Drie oorzaken, alle drie in `opi/forms/editables/enforcers.py`

**1. De weigering landt nergens.** `validate_bare_domain_allowed` (aangeroepen op `:262`) heft een gewone `ValueError`. Die komt op het pad van de groep terecht, `deployments[N]`, en daar hoort geen veld bij. Er is dus niets te tonen: het scherm blijft staan en de knop lijkt kapot. Bij #156 is de subdomeinmelding al naar zijn eigen veld verhuisd, deze is blijven staan.

**2. De aanvraag zit achter de weigering.** Het kaal-domein-blok staat op `:256`, de afhandeling van het vinkje `_request-domain` op `:360`, en `DomainRequestHook` draait pas bij PRE_SAVE, dus alleen als de stap doorgaat. Met het vinkje aan komt de aanvraag dus nooit tot stand: de controle die weigert zit vóór het mechanisme dat de weigering zou oplossen. Er is geen uitgang.

**3. Een openstaande aanvraag heeft geen zichtbaar gevolg.** Een niet-goedgekeurd domein blokkeert het opslaan niet (bewust: anders kan een beheerder een goedkeuring niet intrekken) en `apply_domain_approval_fallback` (`opi/utils/naming.py:1903`) zet de deployment bij publicatie terug op het clusterdomein. Dat is de juiste grendel, maar hij meldt niets, aan niemand.

## Wat er moet gebeuren

### 1. Het kaal-domein-blok krijgt dezelfde vorm als de blokken eronder

Het domein- en subdomeinblok hanteren al een vaste afhandeling, en het kaal-domein-blok hoort die te volgen:

- **platformdomein**: altijd `FieldError`. Die helft van de regel verzacht nooit, want de apex van een platformdomein is van iedereen op het cluster;
- **status `denied`**: `FieldError` wanneer `denied_blocks` aanstaat (het formulier), doorlaten in de opslagpoort, precies zoals nu;
- **status `requested`, of `_request-domain` aangevinkt**: doorlaten. Dit is de nieuwe uitgang;
- **geen regel en niets aangevinkt**: `FieldWarning` die zegt wat er moet gebeuren, niet alleen dat het niet mag. Bijvoorbeeld: "Het kale domein van 'X' kan pas gebruikt worden als het domein is goedgekeurd. Vink 'domein aanvragen' aan om dat aan te vragen."

Alles op het pad van het veld zelf, `domain_setting_path(DomainSetting.BARE_DOMAIN_COMPONENT, index)`, zodat de melding bij het vinkje staat waar hij over gaat.

Dit is veilig omdat publicatie de regel zelf opnieuw toetst: `validate_bare_domain_allowed` draait daar ook, en tot de goedkeuring houdt `apply_domain_approval_fallback` de deployment op het clusteradres. Het formulier hoeft de apex dus niet te bewaken; het moet de gebruiker vooruit helpen.

### 2. Een fout zonder veld mag niet verdwijnen

Dit is de structurele helft. Een `ValueError` uit een enforcer die op een pad zonder gerenderd veld landt, hoort in de algemene "Let op"-balk te komen in plaats van nergens. Zolang dat niet zo is, is de volgende onzichtbare melding een kwestie van tijd; deze is de tweede die we op deze manier vinden.

### 3. De terugval wordt zichtbaar

`apply_domain_approval_fallback` logt op WARNING welk adres niet gebruikt is en waarom, en de deploymentpagina toont dat als melding: "dit adres is nog niet in gebruik omdat het domein op goedkeuring wacht". Zonder deze stap verplaatst stap 1 de verrassing alleen maar: mensen komen dan door het formulier en staan zonder uitleg op het clusteradres.

## De toets

De eerste vier gaan over het gedrag dat een gebruiker merkt, en horen in `tests/test_domain_restrictions.py` en `tests/test_bare_domain_platform_gate.py`, die deze grens al toetsen:

- kaal domein op een domein zonder allowlist-regel geeft een melding op het pad van het kaal-domein-veld, en NIET op `deployments[N]`;
- diezelfde stand met `_request-domain` aangevinkt komt door de stap heen;
- na die stap staat er een `allowed-domains`-regel met `status: requested`;
- kaal domein op een PLATFORMdomein blijft hard geweigerd, met of zonder vinkje. Dit is de test die betrapt dat stap 1 te ver is doorgeschoten;
- status `denied` blijft hard geweigerd in het formulier en blijft doorgelaten in de opslagpoort (`denied_blocks=False`), zodat een beheerder zijn intrekking kan opslaan.

En voor stap 2 en 3:

- een enforcerfout op een pad zonder gerenderd veld komt in de algemene foutbalk terecht in plaats van te verdwijnen;
- publicatie van een deployment met een niet-goedgekeurd domein logt op WARNING welk adres vervalt, en die melding is op de deploymentpagina te zien.

Draaien vanuit `operations-manager/python`:

```
uv run pytest tests/test_domain_restrictions.py tests/test_bare_domain_platform_gate.py tests/test_domain_approval.py tests/test_domain_request_flow.py -q
uv run pytest tests/ -q
uv run ruff check . --fix && uv run ruff format . && uv run pyright
```

## Waar op te letten

**Een `FieldWarning` beëindigt de enforcer.** Hij is een exception, dus de eerste die valt sluit `enforce()` af en alles erna vervalt. Zet de waarschuwing uit stap 1 dus niet vóór controles die nog hard moeten kunnen weigeren. Dit is precies de val waar RC-214 in zijn tweede reviewronde op omviel.

**Toets in de ONgoedgekeurde stand.** De helper `_yaml()` in `tests/test_domain_restrictions.py` zet het domein op `status: approved`. Wie zijn gevallen alleen daarmee schrijft, rijdt de tak die niet het probleem is en krijgt groen om de verkeerde reden.

**De regel zelf blijft staan.** Een kaal-domein-ingress claimt de apex van een domein plus het certificaat, vanuit één tenant-namespace. Dat mag alleen op een domein dat het project zelf meebrengt. Dit plan versoepelt die regel niet; het maakt alleen de weg ernaartoe begaanbaar.

## Wat er niet in zit

**Geen eigen aanvraagvinkje voor het kale domein.** Het kale domein is toegestaan zodra het domein zelf goedgekeurd is. Een tweede vinkje zou een tweede waarheid zijn over dezelfde goedkeuring.

**Geen wijziging aan de reserveringslijst.** Dat is RC-214 (#174).

**Geen uitrol.** Deze PR levert code en tests.
