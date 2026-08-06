# Eén schema per schemaversie

Een projectbestand declareert zijn `schema-version` en wordt gevalideerd tegen het schema
van díe versie. Een migratie levert een nieuwe schemaversie op; de oude blijft bestaan voor
bestanden die nog niet mee zijn. Daardoor kan een migratie afgerond worden: de oude vorm
verhuist naar het schema van de versie die hem droeg en verdwijnt uit het nieuwste schema.

- Nieuwste schema: `operations-manager/python/opi/schemas/project_v2.json`
- Oudere versies: `operations-manager/python/opi/schemas/project_legacy/v<versie>.json`
- Code: `opi/core/project_schema.py`, migratieketen in `opi/services/schema_migration.py`
- Tests: `operations-manager/python/tests/test_project_schema_versions.py`

## Waarom

Het projectschema was één bestand voor de hele 2.x-reeks, en `git_monitor.file_change_handler`
valideert een bestand rechtstreeks uit git **vóór** enige migratie. Dat laatste is een
beveiligingsmaatregel (zie hieronder), maar het had een gevolg dat niemand bedoeld had:
zolang er één bestand op schijf stond dat nog een oude vorm droeg, moest het schema die vorm
blijven accepteren. Een migratie kon dus wel geschreven worden, maar nooit afgesloten.

Gemeten op 6 augustus 2026 over de 47 productiebestanden, vóór deze wijziging:

| | aantal |
|---|---|
| bestanden met de root-`domains:` van vóór v2.5 | 30 |
| bestanden met het `config.keycloak`-restant van vóór v2.3 | 21 |
| bestanden die de rauwe schemavalidatie niet haalden | 22 |
| bestanden die ná migratie de validatie niet haalden | 0 |

Die 22 werden door de poort geweigerd en dus **niet verwerkt**, en dat werd nergens gemeld.
Bestanden herschrijven zichzelf namelijk pas bij verwerking (`project_manager` slaat op zodra
`was_migrated`), dus een project dat een half jaar met rust gelaten wordt blijft een half jaar oud.

Na deze wijziging: alle 47 valideren tegen hun eigen versie, en nog steeds alle 47 tegen het
nieuwste schema ná migratie.

## Hoe het werkt

`project_v2.json` beschrijft alleen de **nieuwste** versie. Het draagt één annotatie die zegt
welke dat is:

```json
"x-zad-schema-version": 2.6
```

Een oudere versie wordt samengesteld uit dat schema plus een keten van patches. Elke patch is
een [RFC 7386 JSON Merge Patch](https://www.rfc-editor.org/rfc/rfc7386) die van het schema van
de eerstvolgende versie dat van die versie maakt:

```
project_v2.json                 (2.6)
  + project_legacy/v2.5.json -> 2.5   (root `invites:` terug)
  + project_legacy/v2.4.json -> 2.4   (root `domains:` terug)
  + project_legacy/v2.3.json -> 2.3   (leeg: die migratie versmalde de vorm niet)
  + project_legacy/v2.2.json -> 2.2   (`config.keycloak` terug)
  + project_legacy/v2.1.json -> 2.1   (`path` als string, `rewrite-path`, `paths`)
  + project_legacy/v2.json   -> 2     (component-`root: true`)
  + project_legacy/v1.json   -> 1     (`uses-services`, `storage`, v0 `publish-on-web`)
```

Een patch is daarmee de schemakant van precies één migratie. `null` als waarde verwijdert een
sleutel, wat nodig is als de nieuwere versie een vorm versmalde in plaats van hem toe te voegen
(zie `v2.1.json`, dat `"type": null` gebruikt om de array-only-eis van 2.2 los te maken voordat
hij hem verbreedt).

## Een migratie toevoegen

1. Voeg de migratiestap toe aan `MIGRATION_STEPS` in `opi/services/schema_migration.py` en de
   nieuwe versie aan `SCHEMA_VERSIONS`.
2. Pas `project_v2.json` aan naar de nieuwe vorm en zet `x-zad-schema-version` op de nieuwe versie.
3. Voeg `opi/schemas/project_legacy/v<vorige versie>.json` toe met een patch die de oude vorm
   terugzet. Is er niets versmald, dan is een patch met alleen een `$comment` het juiste antwoord.

Vergeet je stap 3, dan **stopt het opstarten**: `check_schema_versions(SCHEMA_VERSIONS)` in
`opi/core/startup.py` vergelijkt de keten met de schema's op schijf en weigert te booten als ze
niet overeenkomen — in beide richtingen (een migratie zonder schema, én een schema zonder migratie).
Zonder die controle zou het gevolg zijn dat bestanden die de nieuwe versie declareren stil door
de poort geweigerd worden, maanden later en zonder aanwijsbare oorzaak.

## De poort is een beveiligingsmaatregel

`git_monitor` valideert nog steeds **vóór** de migratie. Dat is bewust: een vijandig bestand dat
toevallig schoon migreert zou anders onder de identiteit van de operations manager teruggeschreven
worden naar `zad-projects` voordat de validatie het afkeurt.

De versiedeclaratie komt uit hetzelfde onvertrouwde bestand als de rest, en wordt dus als invoer
behandeld:

- ontbrekende `schema-version` → weigeren
- niet-numeriek (`"2.6"`, `true`) → weigeren
- onbekende versie (99) → weigeren

Er is geen terugval op "dan maar het nieuwste" (dan zou een oud bestand afgerekend worden op regels
die het nooit had) en geen op "dan maar het soepelste" (dan komt alles binnen door versie 1 te
declareren). Een oude versie is een ánder schema, geen slappere poort: de veiligheidsregels die er
altijd al waren — namespace-patronen, AGE-versleutelde velden, gesloten objecten — staan in elke
versie.

Een afkeur is nu een `ERROR` met de tekst "en NIET verwerkt". Vroeger was het een `WARNING`, met
als redenering dat een afkeur normale invoerafhandeling is. Dat is niet meer waar: elke versie die
een bestand mag declareren heeft een schema, dus een weigering betekent dat er echt iets stuk is.

## Bestanden die nooit verwerkt worden

Bewuste keuze: **er komt geen sweep die alle bestanden migreert.** Oude versies mogen blijven
bestaan, want hun schema bestaat.

- De schade die dit urgent maakte, was niet de oude vorm zelf maar dat 22 bestanden stil door de
  poort geweigerd werden. Dat is nu weg: ze valideren tegen hun eigen versie, en een afkeur is luid.
- Een bestand migreert vanzelf bij zijn eerstvolgende verwerking. Alles in één keer herschrijven
  betekent 30+ commits in `zad-projects` en de bijbehorende ArgoCD-beweging, zonder functionele winst.
- De prijs is één klein, bevroren patchbestand per migratie. Dat is een begrensde prijs, in
  tegenstelling tot de oude situatie waarin het *nieuwste* schema oneindig bleef groeien.

Wil je ooit een versie helemaal laten vallen, dan moet eerst vaststaan dat geen enkel bestand hem
nog declareert. Dat is meetbaar met de replay uit `features/upgrade-safety-test.md`
(`RIG_PROJECTS_DIR=<checkout> uv run pytest tests/test_upgrade_safety_replay.py`).

## Wat er niet verandert

`validate_project_schema(data)` zonder `schema_version` valideert nog steeds tegen het nieuwste
schema. Dat is wat elke schrijfroute wil: die valideert de **eindtoestand**, en die is gemigreerd.
Alleen de poort in `git_monitor` kijkt naar de gedeclareerde versie, want alleen die kijkt naar een
bestand dat nog niet gemigreerd is.
