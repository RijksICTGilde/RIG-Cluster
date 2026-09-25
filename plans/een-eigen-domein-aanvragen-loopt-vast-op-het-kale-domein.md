# Een eigen domein aanvragen loopt vast op het kale domein

Status: plan, 22 september 2026. Af en klaar om te shippen. Dit plan is het contract: er staan geen open beslissingen in. RC-214 is inmiddels gemerged (`48b226b2b`), en elke regelverwijzing hieronder is daarna hertoetst tegen main.

Kaartje: [RijksICTGilde/RIG-Cluster#179](https://github.com/RijksICTGilde/RIG-Cluster/issues/179), op board ZAD (project #5). Sluit die issue als dit uitgevoerd en uitgerold is.

**De volgorde in `DomainConfigEnforcer.enforce()` staat nu vast**, en daar bouwt dit plan op voort. Gemeten op main: het kaal-domein-blok op `:258` met zijn aanroep op `:263`, de reserveringscheck van RC-214 op `:352`, en de afhandeling van `_request-domain` op `:377`. Het kaal-domein-blok is dus het eerste van de drie, en dat is precies waarom een aanvraag er vandaag niet langs komt.

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

## De scheidslijn waar dit plan op rust

Opslaan en toepassen zijn twee verschillende dingen, en de goedkeuring hoort alleen over het tweede te gaan.

**Opslaan mag.** Een eigen domein, een subdomein daarop en een kaal domein mogen alle drie in het projectbestand staan voordat de goedkeuring rond is. Het projectbestand legt vast wat het project WIL; een `requested`-regel hoort daarbij. Het formulier laat de gebruiker in een keer door.

**Toepassen mag niet.** Zolang het domein niet is goedgekeurd, draait de deployment op het clusteradres en wordt het kale domein NIET toegepast. Niet op het eigen domein, want dat is nog niet bevestigd, en zeker niet op het clusteradres waarop teruggevallen wordt: dat zou de apex van onze eigen zone claimen.

## Wat er moet gebeuren

### 1. Het kaal-domein-blok krijgt dezelfde vorm als de blokken eronder

Het domein- en subdomeinblok hanteren al een vaste afhandeling, en het kaal-domein-blok hoort die te volgen:

- **platformdomein**: altijd `FieldError`. Die helft van de regel verzacht nooit, want de apex van een platformdomein is van iedereen op het cluster;
- **status `denied`**: `FieldError` wanneer `denied_blocks` aanstaat (het formulier), doorlaten in de opslagpoort, precies zoals nu;
- **status `requested`, of `_request-domain` aangevinkt**: doorlaten. Dit is de nieuwe uitgang;
- **geen regel en niets aangevinkt**: `FieldWarning` die zegt wat er moet gebeuren, niet alleen dat het niet mag. Bijvoorbeeld: "Het kale domein van 'X' kan pas gebruikt worden als het domein is goedgekeurd. Vink 'domein aanvragen' aan om dat aan te vragen."

Alles op het pad van het veld zelf, `domain_setting_path(DomainSetting.BARE_DOMAIN_COMPONENT, index)`, zodat de melding bij het vinkje staat waar hij over gaat.

Dit is veilig omdat publicatie de regel zelf opnieuw toetst: `validate_bare_domain_allowed` draait daar ook, en tot de goedkeuring houdt `apply_domain_approval_fallback` de deployment op het clusteradres. Het formulier hoeft de apex dus niet te bewaken; het moet de gebruiker vooruit helpen.

### 2. Het kale domein hangt aan de goedkeuring, net als het rootadres

Dit is een bevinding op zichzelf, en hij staat er nu naast. In `opi/utils/naming.py` staan twee blokken onder elkaar:

```python
if domain_approved and domain_format in ROOT_COMPONENT_FORMAT_IDS and root_component and subdomain and base_domain:
    root_hostname = generate_root_hostname(subdomain, base_domain)      # gegrendeld op goedkeuring
...
if expose_on_bare_domain and base_domain:                               # naming.py:2119, nergens op gegrendeld
    bare_hostname = generate_bare_domain_hostname(base_domain)
```

Het rootadres wacht op `domain_approved`, het kale domein niet. Bij een niet-goedgekeurd eigen domein valt de rest dus terug op het clusteradres terwijl de apex gewoon in de hostnamenlijst blijft staan, inclusief certificaataanvraag daarop, vanuit een tenant-namespace.

Vandaag is dat onschadelijk doordat het formulier je er niet doorheen laat en `expose-component-on-bare-domain` dus zelden zonder goedkeuring in een bestand komt. Stap 1 haalt die blokkade weg, en dan wordt dit acuut. Deze grendel hoort er dus VOOR stap 1 in, of in hetzelfde werk.

De reparatie is dezelfde conditie die het rootadres al gebruikt: `domain_approved`. Niet een eigen variant ernaast.

### 3. Een fout zonder veld mag niet verdwijnen

Dit is de structurele helft. Een `ValueError` uit een enforcer die op een pad zonder gerenderd veld landt, hoort in de algemene "Let op"-balk te komen in plaats van nergens. Zolang dat niet zo is, is de volgende onzichtbare melding een kwestie van tijd; deze is de tweede die we op deze manier vinden.

### 4. De terugval wordt zichtbaar

`apply_domain_approval_fallback` logt op WARNING welk adres niet gebruikt is en waarom, en de deploymentpagina toont dat als melding: "dit adres is nog niet in gebruik omdat het domein op goedkeuring wacht". Zonder deze stap verplaatst stap 1 de verrassing alleen maar: mensen komen dan door het formulier en staan zonder uitleg op het clusteradres.

## De toets

De eerste vier gaan over het gedrag dat een gebruiker merkt, en horen in `tests/test_domain_restrictions.py` en `tests/test_bare_domain_platform_gate.py`, die deze grens al toetsen:

- kaal domein op een domein zonder allowlist-regel geeft een melding op het pad van het kaal-domein-veld, en NIET op `deployments[N]`;
- diezelfde stand met `_request-domain` aangevinkt komt door de stap heen;
- na die stap staat er een `allowed-domains`-regel met `status: requested`;
- kaal domein op een PLATFORMdomein blijft hard geweigerd, met of zonder vinkje. Dit is de test die betrapt dat stap 1 te ver is doorgeschoten;
- een deployment met kaal domein op een NIET-goedgekeurd eigen domein levert geen apex-hostnaam op, niet van het eigen domein en niet van de clusterzone. Dit is de toets bij stap 2, en hij valt vandaag om;
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
