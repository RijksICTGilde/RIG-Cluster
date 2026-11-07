# Coding rules

De regels waar code in deze repo aan voldoet. Ze gelden voor nieuw werk vanaf nu, en ze zijn de maatlat voor de opruimrondes die de bestaande code ernaartoe brengen. Waar een regel een getal heeft, is dat getal ook de drempel die een gate later afdwingt.

Eén principe gaat boven alle andere: **code wordt veel vaker gelezen dan geschreven.** Leesbaarheid gaat voor slimheid, eenvoud voor flexibiliteit, minder voor meer.

## 1. De principes

| Principe | Wat het hier betekent |
|---|---|
| **KISS** | De simpelste oplossing die het probleem oplost. Een lezer moet een functie begrijpen zonder drie andere bestanden te openen. |
| **YAGNI** | Geen parameter, hook, abstractie of configuratie voor een toekomst die nog niet bestaat. |
| **DRY** | Eén kennisfeit staat op één plek. Zoek eerst of iets al bestaat (zie "Zoek eerst, schrijf daarna" in `CLAUDE.md`). Twee stukken code die toevallig op elkaar lijken maar om verschillende redenen veranderen, zijn geen duplicatie: voeg die niet samen. |
| **Single Responsibility** | Een module, class of functie heeft één reden om te veranderen. Kun je niet in één zin zonder "en" zeggen wat hij doet, dan doet hij te veel. |
| **Open/Closed** | Een nieuwe service of een nieuw veldtype voeg je toe door iets nieuws te registreren, niet door een bestaande `if/elif`-keten te verlengen. |
| **Liskov** | Een subclass houdt zich aan het contract van zijn basis. Gooit hij `NotImplementedError` voor de helft van de methodes, dan hoort hij er niet onder. |
| **Interface Segregation** | Geef een functie wat hij nodig heeft, niet het hele object. `deploy(namespace, manifests)` in plaats van `deploy(project_manager)`. |
| **Dependency Inversion** | Hoge lagen hangen niet aan de details van lage lagen. Een connector wordt meegegeven, niet binnen een functie aangemaakt. |
| **Law of Demeter** | Praat met je buren, niet met de buren van je buren. `project.config.services[0].settings.database.name` is een signaal dat een functie op de verkeerde plek staat. |
| **Least surprise** | Een functie doet wat zijn naam zegt, en niets anders. `get_` schrijft niet, `validate_` verandert niets. |

## 2. Naamgeving

Een goede naam maakt commentaar overbodig. Neem de tijd voor een naam: hij wordt duizend keer gelezen.

- **Alles in het Engels**: modules, classes, functies, variabelen, testnamen, logberichten, exceptions. De teksten die een gebruiker in de UI ziet blijven Nederlands en staan in `opi/locale/` of de templates, niet als losse strings in Python.
- **Python-conventies (PEP 8)**: `snake_case` voor modules, functies en variabelen; `PascalCase` voor classes; `UPPER_SNAKE_CASE` voor constanten.
- **Functies zijn werkwoorden**, wat ze teruggeven of doen: `render_manifests`, `create_bucket`, `is_expired`. Classes en variabelen zijn zelfstandige naamwoorden.
- **Booleans lezen als een vraag**: `is_enabled`, `has_backup`, `should_retry`. Nooit `flag`, `check` of `status` voor een bool.
- **Vaste werkwoorden, één betekenis**:

  | Werkwoord | Betekent | Doet nooit |
  |---|---|---|
  | `get_` | Geeft iets terug dat er al is, goedkoop | Netwerk, schrijven |
  | `fetch_` / `load_` | Haalt op van een extern systeem of de schijf | Schrijven |
  | `find_` | Zoekt, mag `None` teruggeven | Exception bij niet gevonden |
  | `build_` / `render_` | Maakt een nieuwe waarde, puur | Bijwerkingen |
  | `create_` / `update_` / `delete_` | Verandert de toestand van een extern systeem | Stil slagen als het mislukt |
  | `ensure_` | Idempotent: maakt aan of laat staan | Overschrijven wat goed staat |
  | `validate_` | Gooit bij een fout, geeft niets terug | Corrigeren |
  | `parse_` / `to_` | Zet het ene formaat om in het andere | Bijwerkingen |

- **Doorzoekbaar en uitspreekbaar**: geen afkortingen behalve vaste vaktermen (`k8s`, `db`, `url`, `opi`, `pvc`). Geen `tmp`, `res`, `obj`, `d`, `x` buiten een comprehension van één regel.
- **Geen magische waarden**: een getal of string met betekenis krijgt een naam als constante, dicht bij waar hij gebruikt wordt.
- **Geen type in de naam**: `projects`, niet `project_list` of `projects_dict`. De type hint zegt het type.
- **Geen rommelnamen voor modules**: `utils`, `helpers`, `common`, `misc`, `manager` en `handler` zeggen niet wat erin zit. Een nieuwe module heet naar zijn inhoud: `naming.py`, `retry.py`, `sops.py`. De bestaande `utils/` en `manager/` blijven tot een ronde ze opdeelt.
- **Eén begrip, één woord**: kies `project` of `application`, `delete` of `remove`, `cluster` of `environment`, en gebruik dat overal. De woordenlijst van het domein staat in `CLAUDE.md`; een nieuw begrip komt daar bij.

## 3. Functies

- **Klein.** Richtwaarde 20 regels, signaal boven 40, splitsen boven 60. De body van een functie past op één scherm.
- **Doet één ding, op één abstractieniveau.** Een orchestratiefunctie leest als een inhoudsopgave: `validate`, `provision`, `render`, `commit`, `sync`. Het detailwerk staat in de functies die hij aanroept, niet ertussen.
- **Leest van boven naar beneden.** De publieke functie bovenaan, de functies die hij aanroept eronder, in volgorde van aanroep.
- **Hooguit vier parameters.** Meer is een signaal dat ze bij elkaar horen: maak er een `dataclass` of Pydantic-model van.
- **Geen flag-argumenten.** `render(project, include_secrets=True)` is twee functies: `render_manifests` en `render_secrets`.
- **Diepte hooguit drie niveaus.** Gebruik guard clauses en `return` vroeg in plaats van geneste `if`-blokken.
- **Cyclomatische complexiteit hooguit 10.**
- **Command-query separation.** Een functie verandert iets of geeft iets terug, niet allebei. Uitzondering: een `create_` die het gemaakte object teruggeeft.
- **Geen verborgen bijwerkingen.** Een functie wijzigt zijn argumenten niet, tenzij zijn naam dat zegt.
- **Async blijft async.** Geen blokkerende IO (`subprocess.run`, `requests`, `time.sleep`) in een `async def`: dat legt de event loop stil.

## 4. Classes en modules

- **Een module heeft één onderwerp.** Richtwaarde 300 regels, signaal boven 500, splitsen boven 800.
- **Een class heeft één verantwoordelijkheid.** Richtwaarde 200 regels, splitsen boven 500. Veel instance-attributen of methodes die elk maar een paar attributen gebruiken, betekent dat er meerdere classes in zitten.
- **Geen class waar een functie volstaat.** Een class zonder toestand met één publieke methode is een functie.
- **Data en gedrag bij elkaar.** Een functie die vooral de velden van een ander object leest (feature envy), hoort bij dat object.
- **Compositie boven overerving.** Overerving alleen voor een echt "is een"-verband met een klein contract.
- **Expliciete publieke interface.** Wat een package naar buiten aanbiedt, staat in zijn `__init__.py`. Namen met `_` zijn privé en worden van buiten de module niet geïmporteerd.

## 5. Structuur en koppeling

Het belangrijkste hoofdstuk voor deze repo. Code staat op de plek waar hij hoort, en lagen weten zo min mogelijk van elkaar.

### Lagen

De packages onder `opi/` vormen lagen. Een laag importeert alleen uit de lagen **onder** zich, nooit uit een laag erboven, en lagen op hetzelfde niveau importeren elkaar niet.

| Laag | Packages | Rol |
|---|---|---|
| 1. Ingang | `api`, `web`, `jobs`, `middleware`, `server.py`, `worker_main.py` | HTTP, UI en geplande taken: vertaalt een verzoek naar een aanroep, en het resultaat terug. Geen bedrijfslogica (zie "Eén pad per handeling"). |
| 2. Orchestratie | `manager` | Zet de stappen van een use case achter elkaar: project aanmaken, verwijderen, herstellen. |
| 3. Domein | `services`, `forms`, `generation`, `schemas`, `handlers` | De regels van ZAD: wat een service is, hoe een projectbestand eruitziet, welke manifests eruit komen. Weet niets van HTTP. |
| 4. Integratie | `connectors` | Praat met één extern systeem per module. Weet niets van ZAD-begrippen zoals een project of een service. |
| 5. Fundament | `core`, `utils` | Configuratie, logging, generieke hulpmiddelen. Importeert niets uit `opi/` buiten zichzelf. |

Dit is een **voorstel**: de inventarisatie toetst het aan de code en stelt de indeling bij voordat een gate hem afdwingt. Vandaag importeert onder meer `core` uit `manager` en `api`, `services` uit `web`, en `connectors` uit `services`. Precies die pijlen omhoog maken de repo moeilijk te veranderen.

### Eén pad per handeling

Een handeling die op meerdere plekken begint, zoals een project aanmaken via de API, de UI, een job of de wizard, heeft per ingang een eigen dunne laag en daaronder precies één pad.

- **De ingang vertaalt, meer niet.** Een route of job leest de invoer, controleert of de aanroeper het mag, roept de ene use case aan en zet het resultaat om naar JSON, HTML of een taakstatus. Validatie, beslissingen, volgorde van stappen en foutafhandeling staan niet in de ingang.
- **Elke handeling heeft één use case-functie** in de orchestratielaag, en alle ingangen roepen die aan. Een tweede route naar dezelfde handeling roept dezelfde functie aan; hij bouwt hem niet na.
- **Dezelfde invoer, hetzelfde resultaat.** Of een verzoek nu uit de API of de UI komt: het loopt door dezelfde validatie, dezelfde stappen en dezelfde fouten. Een verschil in uitkomst tussen twee ingangen is een bug.
- **Wat een ingang extra wil, gaat als parameter mee, niet als kopie.** Heeft de UI een dry-run nodig of de API een andere foutvorm, dan krijgt de use case daar een expliciete optie voor, of de ingang zet de uitkomst anders om. Er komt geen tweede versie van de flow.
- **Convergeren boven naast elkaar bestaan.** Vind je twee paden voor dezelfde handeling, dan is dat een bevinding: één wordt het pad, de andere ingang gaat erdoorheen, en het overbodige pad verdwijnt.

### Regels

- **Afhankelijkheden wijzen naar beneden.** Heeft een lage laag iets van een hoge nodig, dan geeft de hoge laag het mee (een parameter, een callback, een protocol), of het hoort zelf in de lage laag.
- **Geen import-cycli.** Een import binnen een functie om een cyclus te ontwijken is verboden: los de cyclus op.
- **Een package heeft één onderwerp.** `connectors/keycloak.py` praat met Keycloak; wat ZAD met een realm wil, staat in de domein- of orchestratielaag.
- **Hoge cohesie, lage koppeling.** Wat samen verandert, staat samen. Wat los verandert, staat los. Een feature die tien packages raakt, is een signaal dat de grenzen verkeerd liggen.
- **Feature folders binnen een laag.** Groeit een package boven de tien modules, deel het dan op naar onderwerp (`manager/project/`, `manager/backup/`), niet naar soort (`manager/models/`, `manager/helpers/`).
- **Geen globale toestand.** Configuratie komt uit `core.config`, één keer gelezen. Geen module-level mutable state buiten caches met een duidelijke eigenaar.
- **Eén manier om iets te doen.** Eén HTTP-client, één manier van loggen, één manier om een secret te ontsleutelen, één manier om een naam samen te stellen. Vind je een tweede, dan is dat een bevinding.

## 6. Commentaar en docstrings

Goede code heeft geen commentaar nodig. Commentaar is een eerlijke bekentenis dat de code het niet zelf kon zeggen.

- **Eerst de code verbeteren.** Wil je uitleggen wat een blok doet, haal het dan uit in een functie met een naam die dat zegt.
- **Commentaar legt het waarom uit, nooit het wat.** Wel: een afwijking van de afspraak, een bewuste keuze, een valkuil die de vorige versie in liep, een verwijzing naar een externe bug of een eis. Niet: wat de volgende regel doet.
- **Docstrings alleen waar naam en type hints tekortschieten**: een publieke functie met een niet-voor-de-hand-liggend contract, een exception die hij gooit, een voorwaarde die de aanroeper moet kennen. Een docstring die de functienaam herhaalt, verdwijnt.
- **Geen uitgecommentarieerde code.** Git onthoudt hem.
- **Geen `TODO` in code.** Open werk is een kaartje op het ZAD-bord.
- **Geen sectiekoppen** (`# ---- Helpers ----`) in een bestand. Heeft een bestand ze nodig, dan zijn het meerdere bestanden.
- **Geen em dashes**, ook niet in commentaar.

## 7. Fouten

- **Specifieke exceptions.** Geen kale `except Exception` of `except:`, behalve op de buitenste grens (een request-handler, een job-runner), en daar altijd gelogd met traceback.
- **Nooit stil slikken.** Een `except` die niets doet of alleen `pass` bevat, legt in een commentaar uit waarom dat veilig is.
- **`raise ... from err`** bij het vertalen van een exception, zodat de oorzaak niet verloren gaat.
- **Fail fast aan de rand.** Valideer invoer bij binnenkomst (Pydantic, de form-validatie), en vertrouw daarna op de types. Geen defensieve `if x is None` diep in de domeinlaag voor iets dat niet `None` kan zijn.
- **Eigen exceptions per laag waar de aanroeper er iets mee doet.** Een connector vertaalt de fout van zijn library naar een eigen exception, zodat hogere lagen niet aan `httpx` of `asyncpg` vastzitten.
- **Geen `None` of `False` als foutcode.** Een mislukte `create_` gooit.

## 8. Types

- **Volledige annotaties** op elke functie: parameters en returntype. Moderne syntax: `list[str]`, `str | None`.
- **`Any` alleen met reden**, en die reden staat erbij.
- **Gestructureerde data is getypeerd**: een `dataclass`, `TypedDict` of Pydantic-model, geen `dict[str, Any]` die door vijf functies reist.
- **Enums of `Literal` voor vaste waardesets**, geen losse strings.
- **pyright** gaat ronde voor ronde van `basic` naar `standard`.

## 9. Tests

- **Testen gedrag, niet implementatie.** Een refactor die het gedrag gelijk laat, breekt geen test. Doet hij dat wel, dan test de test de verkeerde dingen.
- **De teststructuur spiegelt de code**: `opi/manager/backup/restore.py` heeft `tests/manager/backup/test_restore.py`.
- **De naam zegt het gedrag**: `test_restore_fails_when_snapshot_is_missing`, niet `test_restore_2`.
- **Arrange, act, assert**, één gedrag per test.
- **Snel en onafhankelijk.** Geen volgorde-afhankelijkheid, geen gedeelde toestand tussen tests. De lagen en de live groepen staan in [toetsen.md](toetsen.md).
- **Eerst een vangnet, dan verbouwen.** Voor code zonder tests schrijf je karakteriseringstests die het huidige gedrag vastleggen, voordat je hem opsplitst.

## 10. Stijl buiten Python

- **Jinja2-templates**: één component of pagina per bestand, logica in Python en niet in de template, NLDD-componenten (zie `features/nldd-app-shell.md`).
- **YAML en Kustomize**: `base/` plus `overlays/`, één resource per bestand, bestandsnaam naar `kind` en naam.
- **Taskfile** voor elke handeling, geen losse shellscripts.
- **Markdown**: Nederlandse prose met Engelse vaktermen, geen harde regelafbrekingen, geen em dashes.

## 11. Hoe we ombouwen

De repo in één keer omzetten is gevaarlijk. Een ronde volgt deze regels:

1. **Inventariseren voor plannen.** Eerst meten wat er is (lengte, complexiteit, import-pijlen, naamgeving, taal), dan pas kiezen wat eerst gaat.
2. **Gedrag blijft gelijk.** Een refactor-PR verandert geen gedrag, en een feature-PR refactort niet. Nooit allebei in één PR.
3. **Kleine stappen, elk groen.** Eén verplaatsing of één splitsing per PR. Na elke stap draaien de tests, ruff en pyright groen.
4. **Eerst verplaatsen, dan veranderen.** Een functie die naar een andere module gaat, verhuist eerst ongewijzigd. Verbeteren gebeurt in de volgende PR, zodat de diff leesbaar blijft.
5. **Strangler fig voor de grote bestanden.** `project_manager.py` en zijn soortgenoten worden niet herschreven. Er komt een nieuwe module naast, één verantwoordelijkheid verhuist, de oude plek roept de nieuwe aan, de aanroepers verhuizen mee, en de oude code verdwijnt als niemand hem meer gebruikt.
6. **Ratchet.** Is een regel na een ronde in een deel van de code gehaald, dan gaat de gate voor dat deel aan (een ruff-regel, een import-linter-contract, een lengtecheck). Vanaf dan kan het niet meer terugzakken.
7. **Buiten een ronde geldt "Surgical changes"** uit `CLAUDE.md`: raak alleen aan wat je taak vraagt. Opruimen gebeurt in een ronde, niet terloops.

### Gates die de regels afdwingen

Elke gate gaat aan per package, zodra de ronde voor dat package klaar is.

| Regel | Gate |
|---|---|
| Complexiteit ≤ 10 | ruff `C901` met `max-complexity = 10` (nu 30 en uitgezet voor `opi/`) |
| Functielengte, parameters, takken | ruff `PLR0915`, `PLR0913`, `PLR0912` |
| Naamgeving | ruff `N` (pep8-naming) |
| Geen uitgecommentarieerde code | ruff `ERA001` (nu uitgezet) |
| `raise ... from` | ruff `B904` (nu uitgezet) |
| Annotaties | ruff `ANN` (nu uitgezet) |
| Lagen en geen cycli | `import-linter` met een `layers`-contract en `independence`-contracten |
| Modulelengte | een kleine check in de Taskfile op het aantal regels per bestand |
| Taal | een check op Nederlandse woorden in identifiers en commentaar, met een allowlist voor domeintermen |

## Bronnen

- Robert C. Martin, *Clean Code* (2008), de hoofdstukken over namen, functies, commentaar en classes; samengevat in [deze gist](https://gist.github.com/wojteklu/73c6914cc446146b8b533c0988cf8d29) en [deze samenvatting](https://medium.com/thedevproject/summary-of-clean-code-by-robert-c-martin-7117b3947cb8).
- Robert C. Martin, de SOLID-principes en de package-principes (cohesie en koppeling).
- Martin Fowler, *Refactoring* (2e druk, 2018): de code smells (long method, large class, feature envy, divergent change, shotgun surgery) en de refactorings die ze oplossen.
- Martin Fowler, [Strangler Fig](https://en.wikipedia.org/wiki/Strangler_fig_pattern) en Branch by Abstraction, voor stapsgewijs vervangen.
- [PEP 8](https://peps.python.org/pep-0008/) en [PEP 20](https://peps.python.org/pep-0020/) (*The Zen of Python*).
- [import-linter](https://import-linter.readthedocs.io/en/stable/contract_types/layers/) voor het afdwingen van lagen.
