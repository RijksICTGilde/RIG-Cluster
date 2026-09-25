# De gereserveerde namen gelden alleen op onze eigen zones

Status: plan, 22 september 2026. Af en klaar om te shippen. Dit plan is het contract: er staan geen open beslissingen in, elke naam is gekozen, en elk regelnummer en getal is gemeten tegen `forgejo/main` op commit `f1092713d` (22-09-2026). Wat hier niet staat, hoort niet in de PR.

Kaartje: [RijksICTGilde/RIG-Cluster#174](https://github.com/RijksICTGilde/RIG-Cluster/issues/174), op board ZAD (RijksICTGilde project #5). Sluit die issue als dit plan uitgevoerd en uitgerold is.

Verwant maar iets anders: issue #156 gaat over de "subdomein al in gebruik"-check en de granulariteit van `subdomain_registry`. Dit plan raakt die check niet aan.

**Het clusterconfig-blok heet `domains`, niet `nice_url`.** Die hernoeming is al gebeurd op `forgejo/main` (RC-211, `dda3ef951`); `nice_url` komt in `opi/` niet meer voor. De namen om te gebruiken:

| Functie | Plek op `forgejo/main` |
|---|---|
| `get_cluster_domains_config` | `cluster_config.py:1225` |
| `get_supported_domain_names` | `cluster_config.py:1243` |
| `is_domain_supported` | `cluster_config.py:1264` |
| `get_external_dns_target_for_hostname` | `cluster_config.py:1308` |

Let op: `get_domains_config` bestaat ook, maar in `opi/connectors/subdomain.py`, en die leest het goedkeuringsblok uit een PROJECTBESTAND. Een andere functie voor een ander ding, en precies daarom heet de clusterlezer `get_cluster_domains_config`. Verwar die twee niet.

## Wat er is, gemeten

Op 22 september 2026 om 10:30 vroeg `ubbw-0i1` in productie een nieuwe deployment aan op het EIGEN domein `uitbetrouwbarebron.nl`, met subdomein `test`. Twee pogingen, beide geweigerd:

```
10:30:07 opi.web.router_detail_edit WARNING [req-dcbe2b1f] Wizard step ubbw-0i1/modal-add-deployment-1/domain-edit-1 did not advance:
  field_errors={'deployments[1]/services{publish-on-web}/config/subdomain': ["Subdomein 'test' is niet beschikbaar"]} global_errors=[] warnings={}
```

De regel ervoor noemt het domein: `[deferral SUCCESS] copied deployments[1]/base-domain:custom to .../config/base-domain (value=uitbetrouwbarebron.nl)`.

`test` staat in `RESERVED_SUBDOMAINS` (`opi/connectors/subdomain.py:574`), blok "Development/staging", naast `dev`, `staging`, `prod`, `demo`, `sandbox`, `qa` en `uat`. De melding komt uit `validate_subdomain()` (`subdomain.py:825`, de reserveringstak op `:879`, de tekst op `:848`), die voor "gereserveerd" bewust dezelfde generieke tekst gebruikt als voor "al in gebruik", zodat de lijst niet af te tasten is. Het was dus niet de registry (die melding noemt domein en eigenaar) en niet de allowlist van het project (die geeft een `FieldWarning`, en `warnings={}` is leeg).

Waarom dit er zo staat, en niet omdat iemand het zo wilde:

- de lijst en `validate_subdomain()` komen uit `a5c1d6735` (28-01-2026), de eerste nice-URL-commit, toen er alleen platformdomeinen bestonden;
- eigen domeinen met goedkeuring kwamen op 01-04-2026 met `b2678d98c`, die een domein-BEWUSTE laag toevoegde (`restricted_subdomains` per domein, `allowed-subdomains` per project) en de oudere domein-BLINDE controle ervoor liet staan;
- `validate_subdomain(subdomain, language)` krijgt het basisdomein niet mee en kan het onderscheid structureel niet maken. De rest van wat die functie doet (lengte, tekenset, koppeltekens) is wél domeinonafhankelijk, en de reserveringscheck is daar meegelift;
- als veldvalidator loopt hij vóór de section-enforcer die het domein wél kent, dus de blinde controle wint altijd.

`features/domain-restrictions.md` zegt "Reserved subdomains (www, api, admin, etc.) are still rejected regardless of allow-list". Dat staat onder het kopje over platformdomeinen, waar het klopt. Voor eigen domeinen documenteert hetzelfde bestand `restricted-subdomains: false` als standaard: jouw zone, jouw namen. De reserveringslijst bijt daar alleen omdat hij elders zit.

Aan de platformkant rechtvaardigt niets het blokkeren op een eigen domein. `get_external_dns_target_for_hostname()` (`cluster_config.py:1308`) loopt alleen door `supported_domains` en geeft voor een eigen domein `None`: wij zetten geen enkele hostnaam in de zone van een tenant die beschermd moet worden. `router.<zone>` bestaat alleen in onze eigen zones.

De controle zit in TWEE lagen, en dat bepaalt de omvang van deze taak:

- het formulier: `SubdomainValidator` (`opi/forms/editables/validators.py:484`, aanroep op `:492`), gehangen aan `DOMAIN_SUBDOMAIN_EDITABLE` (`opi/services/catalog/publish_on_web/editables.py:165`, validator op `:171`);
- het publicatiepad: `SubdomainConnector.register()` en `register_or_update_for_deployment()` (`opi/services/persistence/subdomain_registry.py:141` en `:505`), aangeroepen vanuit `project_manager.py`.

Repareer je alleen het formulier, dan komt `test` door de wizard en klapt het uitrollen. Daarnaast staan er twee check-endpoints op dezelfde functie (`opi/web/router_self_service.py:89`, `opi/api/v2/router.py:645`); lopen die niet mee, dan zegt de live-check in het portaal iets anders dan de wizard.

## Wat "eigen domein" betekent, en wie dat bepaalt

De cluster config is de autoriteit. Vier dingen daarbij, en ze zijn alle vier fout te doen:

**Niet de allowlist van het project.** `ubbw-0i1` heeft `rijks.app` met `status: approved` in zijn eigen `allowed-domains` staan. Zou "staat in mijn allowlist" het criterium zijn, dan ontgrendelt elk project de reserveringslijst op de gedeelde zone door het platformdomein aan te vragen. De allowlist zegt "dit project mag dit domein gebruiken", niet "dit domein is van dit project".

**Per cluster, niet de unie over alle clusters.** `get_supported_base_domains()` zonder argument telt de domeinen van ALLE clusters bij elkaar op (`opi/connectors/subdomain.py:153`). Dat is hier het verkeerde antwoord: de sandbox heeft `sandbox.rijksapp.dev` en `robbertuittenbroek.nl`, `local` heeft `kind` en `local`. Geef altijd een cluster mee. Voor de registry is dat het cluster van de registratie zelf (het argument dat `register()` al krijgt), voor formulier en endpoints `settings.CLUSTER_MANAGER`, zoals `DomainConfigEnforcer` nu al doet (`enforcers.py:232`).

**Suffix, niet gelijkheid.** Een project kan `team.rijks.app` als eigen basisdomein opgeven. Dat staat niet in `supported_domains`, dus een test op lidmaatschap noemt het een eigen domein en laat `admin.team.rijks.app` toe: een `admin`-label op ONZE registreerbare zone, wat precies is wat de lijst moet tegenhouden. Match daarom zoals `get_external_dns_target_for_hostname()` het al doet (`cluster_config.py:1334-1338`): langste domein eerst, dan `hostname == domain or hostname.endswith("." + domain)`.

**De lijst die we hebben is een aanbodlijst, geen beheerlijst.** `domains.supported_domains` zegt welke domeinen het cluster AANBIEDT, met de eigenschappen die bij dat aanbod horen (`supports_dots`, `issuer`, `restricted_subdomains`, `external_dns_target`). Dat is bijna dezelfde verzameling als "de zones die ZAD beheert", maar niet helemaal, en het verschil is precies het gat: de cluster-default zone staat in een ander veld (`ingress_postfix`, `.rig.prd1.gn2.quattro.rijksapps.nl` op odcn-production, `cluster_config.py:182`) en dus in geen van beide lijsten als beheerde zone.

Gemeten over die zone:

- **Kiesbaar is hij niet.** `ClusterBaseDomainOptionsProvider` (`opi/forms/visualizers/providers.py`) biedt `""` aan met het label "Cluster standaard (rig.prd1.gn2.quattro.rijksapps.nl)" plus de `supported_domains`. De zone zelf staat er niet als waarde tussen.
- **Intypbaar is hij wel.** Het veld is geen gesloten verzameling, en dat is bewust: de eigen-domein-route bestaat eruit dat je je domeinnaam in dit veld schrijft. De provider zegt dat zelf in zijn `options_source`-beschrijving.
- **In projectbestanden staat hij niet.** Over alle projectbestanden in de projects-repo komen als `base-domain` alleen voor: `rijks.app`, `rijksapp.nl`, `rijksapp.dev`, `rijksapps.nl`, `kind`, `rijksorganisatieodi.nl`, `overheid.nl` en `inspectie-oe.nl`. De postfix-zone nul keer.

Dus vandaag zit hij achter twee horden (niet kiesbaar, en als eigen domein ingetypt eerst langs de domeingoedkeuring) en is er geen incident. Dat is geen reden om hem impliciet te laten: hij hoort expliciet in de cluster config op een beheerlijst te staan, ook al is hij niet selecteerbaar.

**En die lijst noemt de zone, nooit zijn ouder.** De postfix-zone hangt onder `rijksapps.nl`, en dat is niet van ons (meervoud is ODC-Noord; `rijksapp.nl` is de onze). `rijksapps.nl` is bovendien in ECHT gebruik als eigen basisdomein: `ug-zxt` publiceert `ux-onderzoeken.rijksapps.nl` (goedgekeurd op 12-05-2026), en `jongo-lh2` heeft er nog een oude vermelding. Zou de beheerlijst de ouder `rijksapps.nl` noemen, dan slikt de suffix-regel `ux-onderzoeken.rijksapps.nl` in en gaat de reserveringslijst daar gelden. Noem dus `rig.prd1.gn2.quattro.rijksapps.nl` en niets ruimers.

## Bestanden die je raakt

Code:

- `opi/core/cluster_config.py` (drie `managed_zones`-lijsten, `get_managed_zones`, `is_platform_domain`, de gedeelde suffix-walk waar `get_external_dns_target_for_hostname` op meegaat)
- `opi/connectors/subdomain.py` (`validate_subdomain` uitgekleed, `validate_subdomain_for_domain` erbij)
- `opi/forms/editables/enforcers.py` (`DomainConfigEnforcer`)
- `opi/forms/editables/validators.py` (`SubdomainValidator` houdt alleen de vormregels)
- `opi/services/persistence/subdomain_registry.py` (twee aanroepen: `:141`, `:505`)
- `opi/web/router_self_service.py:89` en `opi/api/v2/router.py:645` (één aanroep elk)

Tests, in bestaande bestanden en niet in nieuwe:

- `tests/test_cluster_config_extended.py`: `is_platform_domain`, `get_managed_zones`, en de drift-test over alle clusters
- `tests/test_subdomain_connector.py`: de vormregels (ongewijzigd), de reservering per domein, en het rechtzetten van het bestaande `www`-geval
- `tests/test_domain_restrictions.py`: het formuliergedrag, eigen domein versus platformdomein
- `tests/test_domain_field_error_renders.py`: dat de reserveringsmelding bij het subdomein-veld hangt

`tests/test_bare_domain_platform_gate.py` is het patroon om na te volgen: dat toetst al een platform-versus-eigen-domein-grens, en deze toetsen horen er hetzelfde uit te zien.

Documentatie: `features/domain-restrictions.md`. Dit is een wijziging aan een bestaande feature, dus GEEN nieuw bestand in `features/`.

## Wat er moet gebeuren

### 1. Een expliciete beheerlijst in de cluster config, en de functies die hem lezen

Per cluster komt er een lijst van de zones die ZAD zelf bedient: `managed_zones`, BINNEN het `domains`-blok, direct na `supported_domains`. Niet als tweede sleutel op clusterniveau: het blok gaat al over "welke domeinen horen bij dit cluster", en dit is een tweede antwoord op diezelfde vraag. De naam is gekozen, niet ter discussie.

`CLUSTER_CONFIG` heeft drie clusters (`cluster_config.py:15`, `:93` en `:181`, met hun `domains`-blok op `:83`, `:166` en `:282`), en dit is de volledige inhoud die erin komt. Neem hem letterlijk over:

```python
"local":           "managed_zones": ["kind", "local"]
"sandboxed-local": "managed_zones": ["sandbox.rijksapp.dev", "robbertuittenbroek.nl"]
"odcn-production": "managed_zones": ["rijks.app", "rijksapp.nl", "rijksapp.dev", "rig.prd1.gn2.quattro.rijksapps.nl"]
```

Alleen odcn-production krijgt er dus een zone bij die nergens anders stond. Bij de andere twee valt de `ingress_postfix`-zone al samen met een `supported_domains`-domein (`.kind` hoort bij `kind`, `.sandbox.rijksapp.dev` bij `sandbox.rijksapp.dev`).

Waarom een eigen lijst en niet afleiden uit wat er al staat: `supported_domains` is een aanbodlijst en `ingress_postfix` is één string voor een heel ander doel. "Welke zones zijn van ons" is een eigen feit met een eigen gevolg (hier de reserveringslijst, later mogelijk meer), en een zone kan van ons zijn zonder aangeboden te worden. Dat is precies het geval dat nu ontbreekt.

Waarom hij toch niet kan wegdrijven van de rest: er komt een test bij die eist dat de `ingress_postfix`-zone van elk cluster door zijn eigen `managed_zones` gedekt wordt, en dat elk domein uit `supported_domains` dat ook is. Vergeet iemand een nieuwe zone, dan valt die test om in plaats van dat er stil een gat ontstaat. Dat is het punt waarop een tweede lijst wél mag bestaan: hij is expliciet EN afgedekt.

Daarbovenop twee functies in `opi/core/cluster_config.py`, bij de andere domein-opzoekers, beide bovenop `get_cluster_domains_config()` en niet rechtstreeks op `CLUSTER_CONFIG`:

- `get_managed_zones(cluster_name: str) -> list[str]`, die de lijst leest en een lege lijst teruggeeft als het blok of de sleutel ontbreekt, in dezelfde vorm als `get_supported_domain_names()` ernaast;
- `is_platform_domain(cluster_name: str, domain: str) -> bool`, die de suffix-walk over die lijst doet.

**Waarom `is_platform_domain` geen dubbeling is van `is_domain_supported` (`cluster_config.py:1264`).** Die laatste is een lidmaatschapstest op `supported_domains` en zegt volgens zijn eigen docstring "check if a cluster OFFERS a specific base domain". Dat is de aanbodvraag. Deze nieuwe functie stelt de beheervraag: valt deze naam binnen een zone die wij bedienen. Voor `team.rijks.app` verschillen die antwoorden, en voor `rig.prd1.gn2.quattro.rijksapps.nl` ook. Gebruik `is_domain_supported` dus niet voor de reserveringscheck, en breid hem ook niet op: aan het aanbod hangen `issuer` en `supports_dots`, en die moeten aan exact lidmaatschap blijven hangen.

**De suffix-walk komt één keer te bestaan.** `get_external_dns_target_for_hostname()` (`:1308`) doet hem al, maar filtert eerst op entries die een `external_dns_target` HEBBEN; op `kind`, `local` en `sandbox.rijksapp.dev` ontbreekt die sleutel, dus hergebruiken zoals hij is zou die zones tot eigen domein verklaren. Haal daarom de kern eruit als hulpfunctie met de vorm `_longest_matching_zone(hostname: str, zones: list[str]) -> str | None` (langste eerst, dan `hostname == zone or hostname.endswith("." + zone)`). `is_platform_domain` geeft hem `get_managed_zones()`; external-dns geeft hem de domeinnamen van zijn gefilterde entries en zoekt met de teruggegeven zone zijn eigen entry op. Zo houdt external-dns zijn filter en zijn eigen lijst, en bestaat de matchregel op één plek. Twee losse walks naast elkaar is hoe de ene over een jaar wel een suffix matcht en de andere niet.

### 2. De reserveringscheck wordt domein-bewust

In `opi/connectors/subdomain.py`:

- `validate_subdomain()` houdt alleen de vormregels: leeg, lengte, tekenset, koppelteken aan begin of eind. De `reserved`-tak (`:879`) gaat eruit. Die functie blijft daarmee wat hij is, een DNS-vormcontrole, en dat mag in zijn docstring staan;
- erbij komt `validate_subdomain_for_domain(subdomain: str, base_domain: str, cluster: str, language: str = "nl") -> tuple[bool, str | None]`, die eerst `validate_subdomain()` doet en daarna, en alleen als `is_platform_domain(cluster, base_domain)`, de reserveringslijst toepast. Zelfde retourvorm, zodat elke aanroeper alleen zijn functienaam en argumenten hoeft te wijzigen;
- `RESERVED_SUBDOMAINS` blijft staan waar hij staat, met een comment dat het een regel over onze eigen zones is en waarom.

De generieke melding blijft precies zoals hij is. Op een platformdomein is "gereserveerd" daarmee nog steeds niet te onderscheiden van "in gebruik", en dat is de hele reden dat die tekst zo luidt.

### 3. Het formulier

De reserveringscheck verhuist van de veldvalidator naar `DomainConfigEnforcer` (`opi/forms/editables/enforcers.py`), want daar zijn `actual_domain` (custom of platform) en `cluster` al berekend. Hij komt te staan vóór de allowlist- en beschikbaarheidschecks, achter dezelfde wacht als die twee (`if subdomain and actual_domain and "{subdomain}" in template`), en heft een `FieldError` op het subdomein-pad, zodat de melding bij het veld blijft renderen zoals `tests/test_domain_field_error_renders.py` dat voor de beschikbaarheidsmelding vastlegt.

`SubdomainValidator` blijft bestaan en blijft de vormregels doen.

**Waarom niet via de validator-context.** Er is een `ContextAwareEditableValidator` (`opi/forms/editables/editable.py:55`), dus je kunt een validator context meegeven. Maar die context wordt per route met de hand samengesteld en bevat vandaag alleen dingen als `project_name` en `existing_component_names` (`opi/web/router_detail_edit.py:489` en `:989`). Het basisdomein van deze deployment zit er niet in, en elke route die een subdomein kan opslaan zou dat veld moeten leren doorgeven. De enforcer heeft de waarde al. Comply or explain: dit is de afwijking van "check hoort bij het veld", en dit is de reden.

### 4. Het publicatiepad

`register()` (`:141`) en `register_or_update_for_deployment()` (`:505`) roepen de domein-bewuste variant aan. Beide krijgen `cluster` al als argument, dus er verandert niets aan hun signatuur. `_atomic_subdomain_change()` loopt via dezelfde ingang; controleer dat er geen derde plek is die `validate_subdomain()` direct aanroept.

### 5. De twee check-endpoints

`opi/web/router_self_service.py:89` en `opi/api/v2/router.py:645` gaan ook over de domein-bewuste variant, met `settings.CLUSTER_MANAGER` als cluster. Beide hebben `base_domain` al in handen.

**Het veld `cluster_domain` in het v2-antwoord blijft zoals het is** (`api/v2/router.py:643`), en dat is bewust. Dat veld staat nu op lidmaatschap van `supported_domains` en antwoordt op de AANBOD-vraag: biedt dit cluster dit basisdomein zelf aan. De reserveringscheck stelt de BEHEER-vraag: valt deze naam binnen een zone die wij bedienen. Voor `team.rijks.app` verschillen die twee antwoorden (geen aangeboden domein, wel onze zone) en dat is correct, want issuer en `supports_dots` hangen ook aan het aanbod. Maak er geen één ding van en verander de API-semantiek niet in deze PR.

### 6. De documentatie

`features/domain-restrictions.md` scherper: de reserveringslijst geldt op platformdomeinen, niet op een eigen domein, met één regel waarom. Dat is de plek waar de volgende lezer het zoekt.

## Poort: wat niet mag regresseren

Deze wijziging maakt een controle LOSSER. Dat is precies het soort wijziging dat te ver kan doorschieten, dus dit is de ondergrens. Elk van deze vier gevallen hoort een test te hebben, en de PR is niet klaar zolang er één ontbreekt:

1. `admin`, `www`, `api` en `test` blijven geweigerd op `rijks.app`, `rijksapp.nl` en `rijksapp.dev`;
2. `admin` op basisdomein `team.rijks.app` blijft geweigerd. Dit valt om bij een implementatie die op lidmaatschap test in plaats van op suffix;
3. `admin` op de `ingress_postfix`-zone wordt geweigerd, en dan in de spelling waarin die zone in de praktijk bereikt wordt: **een LEEG basisdomein** (de keuze "Cluster standaard") met `domain-format: subdomain`. Dat geeft `admin.rig.prd1.gn2.quattro.rijksapps.nl`. De zone als expliciet ingetypt basisdomein is de tweede spelling en dekt deze niet af, want die komt in nul projectbestanden voor.

   **Dit is de val, en hij is gemeten in RC-214.** In de enforcer geldt `actual_domain = custom_domain if base_domain == "__custom__" else base_domain or None`, dus bij de clusterstandaard is `actual_domain` None en slaat elke check achter `and actual_domain` over. Het publicatiepad vangt het evenmin: `project_manager.py` registreert alleen `if ... and subdomain and base_domain`. Vóór deze wijziging dekte de domein-BLINDE veldvalidator dat pad wel. Wie de check alleen aan `actual_domain` hangt, maakt op precies die zone een gat dat er eerst niet was.

   Los het basisdomein in dat blok daarom op met `resolve_domain_tail(actual_domain, get_ingress_postfix(cluster))` (`opi/utils/naming.py:64`), en laat `actual_domain` zelf ongemoeid: dat is wat de goedkeurings- en beschikbaarheidschecks eronder nodig hebben. `apply_domain_approval_fallback` (`opi/connectors/subdomain.py`) heeft een comment over precies deze val;
4. `ux-onderzoeken.rijksapps.nl` van `ug-zxt` blijft een EIGEN domein. Dit valt om bij een beheerlijst die de ouder `rijksapps.nl` noemt in plaats van de postfix-zone.

**Waar het blok staat, bepaalt of de poort iets betekent.** Het goedkeuringsblok erboven heft een `FieldWarning` zodra een domein geen `allowed-domains`-regel heeft en het aanvraagvinkje uit staat, en een `FieldWarning` is een exception: de enforcer stopt daar en alles erna komt niet aan de beurt. De reserveringscheck hoort dus VOOR dat blok, direct na de punten-check. Staat hij erachter, dan gelden poort 2 en poort 3 alleen voor een al goedgekeurd domein, en dat is precies de stand waarin een eigen domein nooit begint.

**En de toetsen moeten de ONgoedgekeurde stand rijden.** De helper `_yaml()` in `tests/test_domain_restrictions.py` zet het domein op `status: approved`. Wie zijn poortgevallen alleen daarmee schrijft, toetst de tak die niet het probleem is, en krijgt groen om de verkeerde reden. Elk van de vier gevallen hierboven hoort er in beide standen te staan.

En één eigenschap die geen test met een getal heeft maar wel de reden is dat de tekst zo luidt: op een platformdomein blijft de melding generiek (`Subdomein 'x' is niet beschikbaar`), zonder het woord "gereserveerd" en zonder onderscheid met "al in gebruik". Wie die tekst specifieker maakt, maakt de lijst aftastbaar.

## De toets

- `test` op `uitbetrouwbarebron.nl` komt door de wizard EN door `register_or_update_for_deployment()`. Alleen het eerste testen is precies de halve reparatie waar dit plan voor waarschuwt;
- `test`, `admin`, `www` en `api` blijven geweigerd op de drie platformdomeinen, met de ongewijzigde generieke tekst;
- `admin` op `team.rijks.app` blijft geweigerd;
- `admin` op de `ingress_postfix`-zone wordt geweigerd via een LEEG basisdomein plus `domain-format: subdomain`, niet alleen via de ingetypte zone. Geen enkele toets in de suite reed die weg vóór RC-214;
- `ux-onderzoeken.rijksapps.nl` blijft een eigen domein;
- elke `ingress_postfix`-zone en elk `supported_domains`-domein van elk cluster in `CLUSTER_CONFIG` wordt gedekt door de `managed_zones` van datzelfde cluster;
- de vormregels zijn onveranderd: leeg, 64 tekens, `-x`, `x-`, `MY-APP` wordt `my-app` en is geldig;
- de reserveringsmelding rendert bij het subdomein-veld, niet op het onzichtbare deployment-groeppad;
- `tests/test_subdomain_connector.py` registreert vandaag in `test_register_rejects_reserved_subdomain` (`:139`) `www` op `base_domain="rijks.app"` met `cluster="local"`, en op `local` is `rijks.app` geen platformdomein. Dat paar is nu al inconsistent en moet een echt paar worden (`rijks.app` met `odcn-production`, of `local` met `local`), anders legt de test straks juist het verkeerde gedrag vast.

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

**De beschikbaarheidscheck is een andere check.** Die blijft doen wat hij doet, ook op een eigen domein: één eigenaar per `(subdomain, base_domain)`, met de melding die het bezittende project noemt (`enforcers.py:444`). Issue #156 gaat daarover. Niet in deze taak meenemen.

**De clusterstandaard is een leeg basisdomein, geen domein.** `actual_domain` is None zodra de gebruiker "Cluster standaard" kiest, en dat is precies de `ingress_postfix`-zone. Elke regel die je aan `actual_domain` hangt, geldt daar dus niet. Zie poort 3.

**Eén waarschuwing wint.** `DomainConfigEnforcer` levert waarschuwingen af door een `FieldWarning` te heffen, dus de eerste die afgaat verdringt de rest, en de certificaatnotitie staat bewust achteraan. Voeg hier geen vierde waarschuwing tussen zonder te bepalen waar hij in die rij hoort.

**Dit beschermt geen losse platform-hostnames in zones van derden.** `rcr.rijksapps.nl` is onze registry in een zone die niet de onze is. Die naam staat niet op de reserveringslijst en hoort daar ook niet: wat hem beschermt is dat `rijksapps.nl` als eigen domein eerst door een platformbeheerder goedgekeurd moet worden, en dat de DNS van die zone niet bij ons ligt. Vergroot dit plan niet tot een tweede mechanisme daarvoor.

**Sandbox gedraagt zich anders, en dat is correct.** Op `sandboxed-local` staat `robbertuittenbroek.nl` in `supported_domains`, dus daar is dat domein van ons en blijft de lijst gelden. Dat is de cluster config die zijn werk doet, geen bug.

## Wat er niet in zit

Vier dingen die eruit blijven, elk met de reden erbij, zodat ze niet onderweg alsnog aangroeien:

**Geen uitzondering voor de auto-discovery-namen op een eigen domein.** Overwogen: `autodiscover`, `autoconfig`, `wpad`, `isatap` en de RFC 2142-mailboxnamen ook op een eigen domein tegenhouden of er een waarschuwing bij geven, omdat een app op `autodiscover.<eigen domein>` de Outlook-autodiscovery van die organisatie kan breken. Besloten van niet: het is hun domein en hun keuze, ze moeten die naam expliciet invullen, en een extra waarschuwing zou concurreren met de waarschuwingen die al op dat veld kunnen vallen. Willen we het later toch, dan als waarschuwing en nooit als blokkade.

**Geen hernoeming van het domeinenblok.** Die is al gedaan (RC-211). Zie de functietabel bovenaan.

**Geen wijziging aan de beschikbaarheidscheck.** Dat is issue #156.

**Geen uitrol.** Deze PR levert code en tests. Uitrollen naar de sandbox of naar productie gebeurt niet in deze taak en niet door de taak zelf.
