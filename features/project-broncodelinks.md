# Broncodelinks bij de projectgegevens

Een project kan vastleggen waar zijn code staat: een lijst links naar git-repo's op
GitHub, GitLab, code.overheid.nl of Forgejo. Ze staan bij de projectgegevens, naast de
weergavenaam en de omschrijving, en meer is het niet.

**Puur informatief.** ZAD doet niets met deze links: geen clone, geen build, geen
toegangscontrole, geen deploy, en geen controle of de repository bestaat. Ze zijn voor de
mensen die de projectpagina lezen.

## Niet hetzelfde als `repositories`

| | `source-code` | `repositories` |
|---|---|---|
| waarvoor | laten zien waar de code staat | de code ophalen om te deployen |
| draagt | `url`, optioneel `name` | naam, url, gebruikersnaam, wachtwoord, branch, pad |
| wie leest het | de projectpagina en de v2-API | `deployments[].repository`, de backup- en clone-wegen |
| protocollen | alleen `https://` | ook `http://`, `ssh://` en `git://` |

Een `tests/test_source_code_links.py`-grendel bewaakt dat het zo blijft: alleen de vorm,
het formulier, de weergave en de API noemen `source-code`, en niets in `manager/`,
`generation/` of `connectors/`.

## Het veld

```yaml
source-code:
  - url: https://github.com/MinBZK/mijn-app
    name: Applicatie
  - url: https://code.overheid.nl/minbzk/mijn-app-infra
```

| regel | waarde |
|---|---|
| `url` | verplicht, alleen `https://`, geen witruimte, maximaal 500 tekens |
| `name` | optioneel, 1 tot 100 tekens; zonder naam toont het portaal de url zelf |
| aantal | maximaal 20 links, geen dubbele url's |
| overig | geen andere velden; het object is gesloten |

Alleen `https` omdat de waarde een aanklikbare link in de browser wordt. `http://` is daar
onveilig, en `ssh://` of `git@host:pad` levert geen link op die een browser kan openen.

Een project zonder links draagt het veld niet. Leegmaken in het formulier haalt
`source-code` uit het bestand weg in plaats van een lege lijst te schrijven.

De regel staat op één plek: `SourceCodeLinkModel` in `opi/forms/models/project_file.py`.
Het JSON-schema draagt hetzelfde patroon, en de editables lenen de modelregel via
`ModelFieldValidator`, zodat schema, model en formulier niet uit elkaar kunnen lopen.

Behalve de regel over dubbele url's. Die gaat over twee items naast elkaar, en daar reikt
een veldvalidatie niet: `uniqueItems` in het schema vergelijkt hele objecten, dus dezelfde
url met een andere naam komt er langs. Het portaal handhaaft hem met
`UniqueSourceCodeUrlEnforcer` op de identiteitssectie, die op elke schrijfweg van het
formulier draait (de wizard en `modal-edit-identity`) en de fout op de tweede link legt.
`ProjectFileModel.validate_source_code_urls_are_unique` draagt dezelfde regel, maar dat
model staat op geen inleesweg: een bestand dat rechtstreeks in git wordt gezet komt alleen
langs de schemapoort, en die laat dezelfde url met een andere naam door.

## Wijzigen

- In de wizard, in de stap **Projectgegevens**.
- Op de projectpagina via **Bewerken** (de dialoog `modal-edit-identity`).

Er is geen nieuwe rol en geen nieuwe action: wie de omschrijving mag wijzigen
(`project.edit`), mag ook de links wijzigen.

Rechtstreeks in het projectbestand in git kan ook. De schemapoort in `git_monitor`
valideert dat bestand vóór elke verwerking, dus een link die de vormregels niet haalt komt
er niet in. Eén uitzondering: twee keer dezelfde url met een andere naam, zie hierboven.

## Weergave

Op de projectpagina staat onder de omschrijving een opsomming met de links. De tekst van
een link is zijn `name`, of anders de url. Elke link opent in een nieuw tabblad, met
`rel="noopener noreferrer"`.

De v2-API geeft de lijst terug op `GET /api/v2/projects/{project}` als `source_code`
(`url` en `name` per item). Alleen lezen: schrijven gaat via het portaal of via het
projectbestand.

## Schemaversie

`source-code` is toegevoegd in schemaversie **2.17**. De root van het projectschema is
gesloten, dus een 2.16-bestand dat het veld draagt wordt geweigerd; de migratiestap
`source_code_links` is een no-op die alleen de nieuwe versie in de keten zet. Zie
`features/project-schema-versions.md`.

## Afhankelijkheden

Geen. Het veld raakt geen dienst, geen connector en geen manifest.
