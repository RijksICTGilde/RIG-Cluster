# De gereserveerde namen gelden alleen op onze eigen zones

Status: plan, 22 september 2026. Af en klaar om te shippen. Dit plan is het contract: er staan geen open beslissingen in, elke naam is gekozen, en elk getal is gemeten tegen main op 22-09-2026. Wat hier niet staat, hoort niet in de PR.

Kaartje: [RijksICTGilde/RIG-Cluster#174](https://github.com/RijksICTGilde/RIG-Cluster/issues/174), op board ZAD (RijksICTGilde project #5, kolom Backlog). Sluit die issue als dit plan uitgevoerd en uitgerold is.

Verwant maar iets anders: issue #156 gaat over de "subdomein al in gebruik"-check en de granulariteit van `subdomain_registry`. Dit plan raakt die check niet aan.

**Volgorde: dit eerst, de hernoeming daarna.** Dit is een bug op domeinen en gaat voor. Het blok dat hieronder `nice_url` heet staat op de rol om `domains` te gaan heten (issue #175, `plans/nice-url-heet-niet-meer-naar-een-verdwenen-mode.md`, gedragsneutraal), maar dat is een hernoeming van 25 bestanden en die wacht. Schrijf hier dus gewoon in `nice_url` zoals het blok vandaag heet; #175 neemt de nieuwe sleutel en functie mee als het zover is. Verzin er geen tussenvorm voor.

Wat verdwenen is, is de PROJECTBESTAND-mode `domain-mode: nice-url` (`5361dd4c`, v2.8-migratie). Het clusterconfig-blok is springlevend: 96 verwijzingen in `opi/` en `tests/`, waarvan 27 in `cluster_config.py`, en een blok in elk van de drie clusters.

## Wat er is, gemeten

Op 22 september 2026 om 10:30 vroeg `ubbw-0i1` in productie een nieuwe deployment aan op het EIGEN domein `uitbetrouwbarebron.nl`, met subdomein `test`. Twee pogingen, beide geweigerd:

```
10:30:07 opi.web.router_detail_edit WARNING [req-dcbe2b1f] Wizard step ubbw-0i1/modal-add-deployment-1/domain-edit-1 did not advance:
  field_errors={'deployments[1]/services{publish-on-web}/config/subdomain': ["Subdomein 'test' is niet beschikbaar"]} global_errors=[] warnings={}
```

De regel ervoor noemt het domein: `[deferral SUCCESS] copied deployments[1]/base-domain:custom to .../config/base-domain (value=uitbetrouwbarebron.nl)`.

`test` staat in `RESERVED_SUBDOMAINS` (`opi/connectors/subdomain.py:574`), blok "Development/staging", naast `dev`, `staging`, `prod`, `demo`, `sandbox`, `qa` en `uat`. De melding komt uit `validate_subdomain()` (`subdomain.py:848`), die voor "gereserveerd" bewust dezelfde generieke tekst gebruikt als voor "al in gebruik", zodat de lijst niet af te tasten is. Het was dus niet de registry (die melding noemt domein en eigenaar) en niet de allowlist van het project (die geeft een `FieldWarning`, en `warnings={}` is leeg).

Waarom dit er zo staat, en niet omdat iemand het zo wilde:

- de lijst en `validate_subdomain()` komen uit `a5c1d6735` (28-01-2026), de eerste nice-URL-commit, toen er alleen platformdomeinen bestonden;
- eigen domeinen met goedkeuring kwamen op 01-04-2026 met `b2678d98c`, die een domein-BEWUSTE laag toevoegde (`restricted_subdomains` per domein, `allowed-subdomains` per project) en de oudere domein-BLINDE controle ervoor liet staan;
- `validate_subdomain(subdomain, language)` krijgt het basisdomein niet mee en kan het onderscheid structureel niet maken. De rest van wat die functie doet (lengte, tekenset, koppeltekens) is wél domeinonafhankelijk, en de reserveringscheck is daar meegelift;
- als veldvalidator loopt hij vóór de section-enforcer die het domein wél kent, dus de blinde controle wint altijd.

`features/domain-restrictions.md:54` zegt "Reserved subdomains (www, api, admin, etc.) are still rejected regardless of allow-list". Dat staat onder het kopje over platformdomeinen, waar het klopt. Voor eigen domeinen documenteert hetzelfde bestand `restricted-subdomains: false` als standaard: jouw zone, jouw namen. De reserveringslijst bijt daar alleen omdat hij elders zit.

Aan de platformkant rechtvaardigt niets het blokkeren op een eigen domein. `get_external_dns_target_for_hostname()` (`opi/core/cluster_config.py:1324`) loopt alleen door `supported_domains` en geeft voor een eigen domein `None`: wij zetten geen enkele hostnaam in de zone van een tenant die beschermd moet worden. `router.<zone>` bestaat alleen in onze eigen zones.

De controle zit in TWEE lagen, en dat bepaalt de omvang van deze taak:

- het formulier: `SubdomainValidator` (`opi/forms/editables/validators.py:490`), gehangen aan `DOMAIN_SUBDOMAIN_EDITABLE` (`opi/services/catalog/publish_on_web/editables.py:171`);
- het publicatiepad: `SubdomainConnector.register()` en `register_or_update_for_deployment()` (`opi/services/persistence/subdomain_registry.py:141` en `:505`), aangeroepen vanuit `project_manager.py:5621`.

Repareer je alleen het formulier, dan komt `test` door de wizard en klapt het uitrollen. Daarnaast staan er twee check-endpoints op dezelfde functie (`opi/web/router_self_service.py:89`, `opi/api/v2/router.py:645`); lopen die niet mee, dan zegt de live-check in het portaal iets anders dan de wizard.

## Wat "eigen domein" betekent, en wie dat bepaalt

De cluster config is de autoriteit: `nice_url.supported_domains` van het cluster (`opi/core/cluster_config.py:294` voor odcn-production). Staat een domein daarin, of valt het eronder, dan is het van ons. Anders is het een eigen domein. Vier dingen daarbij, en ze zijn alle vier fout te doen:

**Niet de allowlist van het project.** `ubbw-0i1` heeft `rijks.app` met `status: approved` in zijn eigen `allowed-domains` staan. Zou "staat in mijn allowlist" het criterium zijn, dan ontgrendelt elk project de reserveringslijst op de gedeelde zone door het platformdomein aan te vragen. De allowlist zegt "dit project mag dit domein gebruiken", niet "dit domein is van dit project".

**Per cluster, niet de unie over alle clusters.** `get_supported_base_domains()` zonder argument telt de domeinen van ALLE clusters bij elkaar op (`subdomain.py:171`). Dat is hier het verkeerde antwoord: de sandbox heeft `sandbox.rijksapp.dev` en `robbertuittenbroek.nl`, `local` heeft `kind` en `local`. Geef altijd een cluster mee. Voor de registry is dat het cluster van de registratie zelf (het argument dat `register()` al krijgt), voor formulier en endpoints `settings.CLUSTER_MANAGER`, zoals `DomainConfigEnforcer` nu al doet.

**Suffix, niet gelijkheid.** Een project kan `team.rijks.app` als eigen basisdomein opgeven. Dat staat niet in `supported_domains`, dus een test op lidmaatschap noemt het een eigen domein en laat `admin.team.rijks.app` toe: een `admin`-label op ONZE registreerbare zone, wat precies is wat de lijst moet tegenhouden. Match daarom zoals `get_external_dns_target_for_hostname()` het al doet: `hostname == domain or hostname.endswith("." + domain)`, langste domein eerst.

**De lijst die we hebben is een aanbodlijst, geen beheerlijst.** `nice_url.supported_domains` zegt welke domeinen het cluster AANBIEDT, met de eigenschappen die bij dat aanbod horen (`supports_dots`, `issuer`, `restricted_subdomains`, `external_dns_target`). Dat is bijna dezelfde verzameling als "de zones die ZAD beheert", maar niet helemaal, en het verschil is precies het gat: de cluster-default zone staat in een ander veld (`ingress_postfix`, `.rig.prd1.gn2.quattro.rijksapps.nl` op odcn-production, `cluster_config.py:182`) en dus in geen van beide lijsten als beheerde zone.

Gemeten over die zone:

- **Kiesbaar is hij niet.** `ClusterBaseDomainOptionsProvider` (`opi/forms/visualizers/providers.py:533`) biedt `""` aan met het label "Cluster standaard (rig.prd1.gn2.quattro.rijksapps.nl)" plus de `supported_domains`. De zone zelf staat er niet als waarde tussen.
- **Intypbaar is hij wel.** Het veld is geen gesloten verzameling, en dat is bewust: de eigen-domein-route bestaat eruit dat je je domeinnaam in dit veld schrijft. De provider zegt dat zelf in zijn `options_source`-beschrijving.
- **In projectbestanden staat hij niet.** Over alle projectbestanden in de projects-repo komen als `base-domain` alleen voor: `rijks.app`, `rijksapp.nl`, `rijksapp.dev`, `rijksapps.nl`, `kind`, `rijksorganisatieodi.nl`, `overheid.nl` en `inspectie-oe.nl`. De postfix-zone nul keer.

Dus vandaag zit hij achter twee horden (niet kiesbaar, en als eigen domein ingetypt eerst langs de domeingoedkeuring) en is er geen incident. Dat is geen reden om hem impliciet te laten: hij hoort expliciet in de cluster config op een beheerlijst te staan, ook al is hij niet selecteerbaar. Zie stap 1.

**En die lijst noemt de zone, nooit zijn ouder.** De postfix-zone hangt onder `rijksapps.nl`, en dat is niet van ons (meervoud is ODC-Noord; `rijksapp.nl` is de onze). `rijksapps.nl` is bovendien in ECHT gebruik als eigen basisdomein: `ug-zxt` publiceert `ux-onderzoeken.rijksapps.nl` (goedgekeurd op 12-05-2026), en `jongo-lh2` heeft er nog een oude vermelding. Zou de beheerlijst de ouder `rijksapps.nl` noemen, dan slikt de suffix-regel `ux-onderzoeken.rijksapps.nl` in en gaat de reserveringslijst daar gelden. Noem dus `rig.prd1.gn2.quattro.rijksapps.nl` en niets ruimers.

## Bestanden die je raakt

Code:

- `opi/core/cluster_config.py` (drie `managed_zones`-lijsten, `get_managed_zones`, `is_platform_domain`, de gedeelde suffix-walk waar `get_external_dns_target_for_hostname` op meegaat)
- `opi/connectors/subdomain.py` (`validate_subdomain` uitgekleed, `validate_subdomain_for_domain` erbij)
- `opi/forms/editables/enforcers.py` (`DomainConfigEnforcer`)
- `opi/forms/editables/validators.py` (`SubdomainValidator` houdt alleen de vormregels)
- `opi/services/persistence/subdomain_registry.py` (twee aanroepen)
- `opi/web/router_self_service.py`, `opi/api/v2/router.py` (één aanroep elk)

Tests, in bestaande bestanden en niet in nieuwe:

- `tests/test_cluster_config_extended.py`: `is_platform_domain`, `get_managed_zones`, en de drift-test over alle clusters
- `tests/test_subdomain_connector.py`: de vormregels (ongewijzigd), de reservering per domein, en het rechtzetten van het bestaande `www`-geval
- `tests/test_domain_restrictions.py`: het formuliergedrag, eigen domein versus platformdomein
- `tests/test_domain_field_error_renders.py`: dat de reserveringsmelding bij het subdomein-veld hangt

`tests/test_bare_domain_platform_gate.py` is het patroon om na te volgen: dat toetst al een platform-versus-eigen-domein-grens, en deze toetsen horen er hetzelfde uit te zien.

Documentatie: `features/domain-restrictions.md`. Dit is een wijziging aan een bestaande feature, dus GEEN nieuw bestand in `features/`.

## Wat er moet gebeuren

### 1. Een expliciete beheerlijst in de cluster config, en één functie die hem leest

Per cluster komt er een lijst van de zones die ZAD zelf bedient: `managed_zones`, BINNEN het `nice_url`-blok, naast `supported_domains`. Niet als derde sleutel op clusterniveau: het blok gaat al over "welke domeinen horen bij dit cluster", en dit is een tweede antwoord op diezelfde vraag. De naam is gekozen, niet ter discussie.

`CLUSTER_CONFIG` heeft drie clusters, en dit is de volledige inhoud die erin komt. Neem hem letterlijk over:

```python
"local":           "managed_zones": ["kind", "local"]
"sandboxed-local": "managed_zones": ["sandbox.rijksapp.dev", "robbertuittenbroek.nl"]
"odcn-production": "managed_zones": ["rijks.app", "rijksapp.nl", "rijksapp.dev", "rig.prd1.gn2.quattro.rijksapps.nl"]
```

Alleen odcn-production krijgt er dus een zone bij die nergens anders stond. Bij de andere twee valt de `ingress_postfix`-zone al samen met een `supported_domains`-domein (`.kind` hoort bij `kind`, `.sandbox.rijksapp.dev` bij `sandbox.rijksapp.dev`).

Waarom een eigen lijst en niet afleiden uit wat er al staat: `supported_domains` is een aanbodlijst en `ingress_postfix` is één string voor een heel ander doel. "Welke zones zijn van ons" is een eigen feit met een eigen gevolg (hier de reserveringslijst, later mogelijk meer), en een zone kan van ons zijn zonder aangeboden te worden. Dat is precies het geval dat nu ontbreekt.

Waarom hij toch niet kan wegdrijven van de rest: er komt een test bij die eist dat de `ingress_postfix`-zone van elk cluster door zijn eigen `managed_zones` gedekt wordt, en dat elk domein uit `supported_domains` dat ook is. Vergeet iemand een nieuwe zone, dan valt die test om in plaats van dat er stil een gat ontstaat. Dat is het punt waarop een tweede lijst wél mag bestaan: hij is expliciet EN afgedekt.

Daarbovenop twee functies in `opi/core/cluster_config.py`, bij de andere domein-opzoekers:

- `get_managed_zones(cluster_name: str) -> list[str]`, die de lijst leest en een lege lijst teruggeeft als het blok ontbreekt;
- `is_platform_domain(cluster_name: str, domain: str) -> bool`, die de suffix-walk over die lijst doet.

`get_external_dns_target_for_hostname()` doet zo'n walk al, maar filtert eerst op entries die een `external_dns_target` HEBBEN. Op `kind`, `local` en `sandbox.rijksapp.dev` ontbreekt die sleutel, dus hergebruiken zoals hij is zou die zones tot eigen domein verklaren. Haal de walk daarom naar een eigen hulpfunctie (langste eerst) en laat beide hem gebruiken, waarbij external-dns zijn filter houdt en zijn eigen lijst blijft lezen. Twee losse walks naast elkaar is hoe de ene over een jaar wel een suffix matcht en de andere niet.

### 2. De reserveringscheck wordt domein-bewust

In `opi/connectors/subdomain.py`:

- `validate_subdomain()` houdt alleen de vormregels: leeg, lengte, tekenset, koppelteken aan begin of eind. De `reserved`-tak gaat eruit. Die functie blijft daarmee wat hij is, een DNS-vormcontrole, en dat mag in zijn docstring staan;
- erbij komt `validate_subdomain_for_domain(subdomain: str, base_domain: str, cluster: str, language: str = "nl") -> tuple[bool, str | None]`, die eerst `validate_subdomain()` doet en daarna, en alleen als `is_platform_domain(cluster, base_domain)`, de reserveringslijst toepast. Zelfde retourvorm als `validate_subdomain()`, zodat elke aanroeper alleen zijn functienaam en argumenten hoeft te wijzigen;
- `RESERVED_SUBDOMAINS` blijft staan waar hij staat, met een comment dat het een regel over onze eigen zones is en waarom.

De generieke melding blijft precies zoals hij is. Op een platformdomein is "gereserveerd" daarmee nog steeds niet te onderscheiden van "in gebruik", en dat is de hele reden dat die tekst zo luidt.

### 3. Het formulier

De reserveringscheck verhuist van de veldvalidator naar `DomainConfigEnforcer` (`opi/forms/editables/enforcers.py`), want daar zijn `actual_domain` (custom of platform) en `cluster` al berekend. Hij komt te staan vóór de allowlist- en beschikbaarheidschecks en heft een `FieldError` op het subdomein-pad, zodat de melding bij het veld blijft renderen, zoals `tests/test_domain_field_error_renders.py` voor de beschikbaarheidsmelding vastlegt.

`SubdomainValidator` blijft bestaan en blijft de vormregels doen.

**Waarom niet via de validator-context.** Er is een `ContextAwareEditableValidator` (`opi/forms/editables/editable.py:56`), dus je kunt een validator context meegeven. Maar die context wordt per route met de hand samengesteld en bevat vandaag alleen dingen als `project_name` en `existing_component_names` (`opi/web/router_detail_edit.py:445`, `:489`, `:989`). Het basisdomein van deze deployment zit er niet in, en elke route die een subdomein kan opslaan zou dat veld moeten leren doorgeven. De enforcer heeft de waarde al. Comply or explain: dit is de afwijking van "check hoort bij het veld", en dit is de reden.

### 4. Het publicatiepad

`register()` en `register_or_update_for_deployment()` roepen de domein-bewuste variant aan. Beide krijgen `cluster` al als argument, dus er verandert niets aan hun signatuur. `_atomic_subdomain_change()` loopt via dezelfde ingang; controleer dat er geen derde plek is die `validate_subdomain()` direct aanroept.

### 5. De twee check-endpoints

`opi/web/router_self_service.py:89` en `opi/api/v2/router.py:645` gaan ook over de domein-bewuste variant, met `settings.CLUSTER_MANAGER` als cluster. Beide hebben `base_domain` al in handen.

**Het veld `cluster_domain` in het v2-antwoord blijft zoals het is**, en dat is bewust. Dat veld staat nu op lidmaatschap van `supported_domains` en antwoordt op de AANBOD-vraag: biedt dit cluster dit basisdomein zelf aan. De reserveringscheck stelt de BEHEER-vraag: valt deze naam binnen een zone die wij bedienen. Voor `team.rijks.app` verschillen die twee antwoorden (geen aangeboden domein, wel onze zone) en dat is correct, want issuer en `supports_dots` hangen ook aan het aanbod. Maak er geen één ding van en verander de API-semantiek niet in deze PR.

### 6. De documentatie

`features/domain-restrictions.md:54` scherper: de reserveringslijst geldt op platformdomeinen, niet op een eigen domein, met één regel waarom. Dat is de plek waar de volgende lezer het zoekt.

## Poort: wat niet mag regresseren

Deze wijziging maakt een controle LOSSER. Dat is precies het soort wijziging dat te ver kan doorschieten, dus dit is de ondergrens. Elk van deze vier gevallen hoort een test te hebben, en de PR is niet klaar zolang er één ontbreekt:

1. `admin`, `www`, `api` en `test` blijven geweigerd op `rijks.app`, `rijksapp.nl` en `rijksapp.dev`;
2. `admin` op basisdomein `team.rijks.app` blijft geweigerd. Dit valt om bij een implementatie die op lidmaatschap test in plaats van op suffix;
3. `admin` op de `ingress_postfix`-zone van het cluster wordt geweigerd. Dit valt om als de beheerlijst die zone niet noemt;
4. `ux-onderzoeken.rijksapps.nl` van `ug-zxt` blijft een EIGEN domein. Dit valt om bij een beheerlijst die de ouder `rijksapps.nl` noemt in plaats van de postfix-zone.

En één eigenschap die geen test met een getal heeft maar wel de reden is dat de tekst zo luidt: op een platformdomein blijft de melding generiek ("`Subdomein 'x' is niet beschikbaar`"), zonder het woord "gereserveerd" en zonder onderscheid met "al in gebruik". Wie die tekst specifieker maakt, maakt de lijst aftastbaar.

## De toets

- `test` op `uitbetrouwbarebron.nl` komt door de wizard EN door `register_or_update_for_deployment()`. Alleen het eerste testen is precies de halve reparatie waar dit plan voor waarschuwt;
- `test`, `admin`, `www` en `api` blijven geweigerd op `rijks.app`, `rijksapp.nl` en `rijksapp.dev`, met de ongewijzigde generieke tekst en zonder het woord "gereserveerd";
- `admin` op basisdomein `team.rijks.app` blijft geweigerd. Dit is de test die een implementatie op alleen lidmaatschap laat vallen;
- `admin` op de `ingress_postfix`-zone van het cluster wordt geweigerd. Vandaag komt niemand daar, dus dit is de test die vastlegt dat de zone op de beheerlijst staat;
- `ux-onderzoeken.rijksapps.nl` van `ug-zxt` blijft een EIGEN domein: de beheerlijst noemt de postfix-zone en niet de ouder `rijksapps.nl`. Dit is de test die een te ruime lijst betrapt;
- elke `ingress_postfix`-zone en elk `supported_domains`-domein van elk cluster in `CLUSTER_CONFIG` wordt gedekt door de `managed_zones` van datzelfde cluster. Dit is de test die voorkomt dat de lijst wegdrijft;
- de vormregels zijn onveranderd: leeg, 64 tekens, `-x`, `x-`, `MY-APP` wordt `my-app` en is geldig;
- de reserveringsmelding rendert bij het subdomein-veld, niet op het onzichtbare deployment-groeppad;
- `tests/test_subdomain_connector.py` (rond regel 145) registreert vandaag `www` op `base_domain="rijks.app"` met `cluster="local"`, en op `local` is `rijks.app` geen platformdomein. Dat paar is nu al inconsistent en moet een echt paar worden (`rijks.app` met `odcn-production`, of `local` met `local`), anders legt de test straks juist het verkeerde gedrag vast;
- `uv run ruff check . --fix`, `uv run ruff format .`, `uv run pyright`.

## Klaar als

Draai vanuit `operations-manager/python`:

```
uv run pytest tests/test_cluster_config_extended.py tests/test_subdomain_connector.py tests/test_domain_restrictions.py tests/test_domain_field_error_renders.py tests/test_bare_domain_platform_gate.py -q
uv run pytest tests/ -q
uv run ruff check . --fix && uv run ruff format . && uv run pyright
```

En deze greps:

```
grep -n "RESERVED_SUBDOMAINS" opi/connectors/subdomain.py     # alleen de lijst zelf en de domein-bewuste functie
grep -rn "validate_subdomain(" --include="*.py" opi/          # geen aanroeper meer die het basisdomein wel kent
```

De volledige suite moet hetzelfde aantal gefaalde tests geven als vóór de wijziging, en dat is nul: meet dat ijkpunt op de basistak voordat je begint. De tweede grep is de kern van de oplevering: elke plek die een basisdomein in handen heeft, hoort de domein-bewuste variant te gebruiken. Blijft daar een aanroeper staan, dan is de reparatie half en komt `test` door de wizard om later bij het uitrollen te klappen.

## Waar op te letten

**De beschikbaarheidscheck is een andere check.** Die blijft doen wat hij doet, ook op een eigen domein: één eigenaar per `(subdomain, base_domain)`, met de melding die het bezittende project noemt. Issue #156 gaat daarover. Niet in deze taak meenemen.

**Eén waarschuwing wint.** `DomainConfigEnforcer` levert waarschuwingen af door een `FieldWarning` te heffen, dus de eerste die afgaat verdringt de rest, en de certificaatnotitie staat bewust achteraan. Voeg hier geen vierde waarschuwing tussen zonder te bepalen waar hij in die rij hoort.

**Dit beschermt geen losse platform-hostnames in zones van derden.** `rcr.rijksapps.nl` is onze registry in een zone die niet de onze is. Die naam staat niet op de reserveringslijst en hoort daar ook niet: wat hem beschermt is dat `rijksapps.nl` als eigen domein eerst door een platformbeheerder goedgekeurd moet worden, en dat de DNS van die zone niet bij ons ligt. Vergroot dit plan niet tot een tweede mechanisme daarvoor.

**Sandbox gedraagt zich anders, en dat is correct.** Op `sandboxed-local` staat `robbertuittenbroek.nl` in `supported_domains`, dus daar is dat domein van ons en blijft de lijst gelden. Dat is de cluster config die zijn werk doet, geen bug.

## Wat er niet in zit

Vier dingen die eruit blijven, elk met de reden erbij, zodat ze niet onderweg alsnog aangroeien:

**Geen uitzondering voor de auto-discovery-namen op een eigen domein.** Overwogen: `autodiscover`, `autoconfig`, `wpad`, `isatap` en de RFC 2142-mailboxnamen ook op een eigen domein nog tegenhouden of er een waarschuwing bij geven, omdat een app op `autodiscover.<eigen domein>` de Outlook-autodiscovery van die organisatie kan breken. Besloten van niet: het is hun domein en hun keuze, ze moeten die naam expliciet invullen, en een extra waarschuwing zou concurreren met de waarschuwingen die al op dat veld kunnen vallen. Willen we het later toch, dan als waarschuwing en nooit als blokkade.

**Geen hernoeming van `nice_url`.** Dat is issue #175 en die komt hierna. Schrijf in `nice_url` zoals het blok vandaag heet.

**Geen wijziging aan de beschikbaarheidscheck.** Dat is issue #156.

**Geen uitrol.** Deze PR levert code en tests. Uitrollen naar de sandbox of naar productie gebeurt niet in deze taak en niet door de taak zelf.
