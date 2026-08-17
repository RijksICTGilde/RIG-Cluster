# De platte `env-vars`-sleutel op een deployment-component: opruimen of adopteren

Status: issue, 5 augustus 2026. Niet gebouwd. Wacht op één meting op de echte projectbestanden.

## Wat het is

Een component binnen een deployment kan drie bronnen van omgevingsvariabelen hebben, die
bij het uitrollen over elkaar heen worden gelegd (`opi/manager/project_manager.py`, rond
regel 5262-5300), elk met `dict.update`, dus de laatste wint per sleutel:

1. `components[*]/user-env-vars` — versleuteld
2. `deployments[*]/components[*]/user-env-vars` — versleuteld, overschrijft 1
3. `deployments[*]/components[*]/env-vars` — **platte tekst**, overschrijft alles
4. daarna nog de bijlagen met `provide-as: env-var`, die in dezelfde dict worden geschreven

Nummer 1 en 2 zijn sinds RC-25 een systeemdienst met een configmodel, een schemafragment,
validatie bij het opslaan en een formulierveld per laag. Nummer 3 staat daar helemaal
buiten, en dat is de aanleiding voor deze notitie.

Let op: er is **geen** projectniveau. "Deployment-niveau" bestaat wel, maar het is per
component binnen een deployment, niet deployment-breed.

## Waarom dit een issue is en geen opruimklusje

De sleutel met de **hoogste voorrang** in de samenvoeging is tegelijk de enige zonder
validatie, zonder model, zonder formulier en zonder versleuteling. Wie hem gebruikt,
overschrijft daarmee een versleutelde waarde met een waarde die leesbaar in het
projectbestand staat. Als er ook maar één project dit gebruikt, is dat eerst een
security-vraag ("staat daar een geheim in?") en pas daarna een opruimvraag.

## Wat er gemeten is (5 augustus 2026)

| Signaal | Bevinding |
|---|---|
| Ontstaan | de eerste commit (8 oktober 2025, "Initial commit - RIG Cluster configuration"). Overgeërfd, nooit als functie ontworpen |
| `DeploymentComponentModel` | afwezig — het typed model kent alleen `reference`, `image`, `imagePullPolicy` |
| Formulierlaag | afwezig — geen editable, geen visualizer, geen layout. Alleen via API of het bestand |
| `projects/` in de repo | 0 voorkomens |
| Geschoonde productiefixtures (4 bestanden) | 0 voorkomens |
| Tests | 1, en alleen als willekeurig "ander veld" dat auto-tune niet mag wissen (`test_autotune_field_preservation.py`) — geen test van het gedrag zelf |
| `project_v2.json` | aanwezig op `$defs/deployment-component` |
| Runtime | één regel in `project_manager`, hoogste voorrang in de samenvoeging |

Het leeft dus op precies twee plekken: het JSON-schema en één `dict.update`.

## Wat er nog niet gemeten is

**Of enig echt project de sleutel gebruikt.** De vier fixtures zijn geschoonde kopieën van
productiebestanden, maar het zijn er vier; RC-22 speelde er 47 af. De sandbox-Forgejo kan
het niet beantwoorden: die bevat alleen wegwerpprojecten en is na een testronde leeg (op 5
augustus stond er alleen een `README.md`). Vanaf de ontwikkelserver is geen productie-Git
bereikbaar.

De beslissende meting is één regel op de echte `zad-projects`:

```bash
grep -rn '^\s*env-vars:' <zad-projects>/projects/
```

Let bij het lezen op dat `$defs/deployment-helmfile` ook een `env-vars` heeft; die staat
hier los van en moet niet meegeteld worden.

## De twee uitkomsten

**Nul treffers → weghalen.** Drie plekken: de sleutel uit `$defs/deployment-component` in
`opi/schemas/project_v2.json`, het `component.get("env-vars", {})`-blok in
`project_manager.py`, en een ander veld kiezen als proefkonijn in
`test_autotune_field_preservation.py`. Klein en afgebakend. Het is wel een
gedragswijziging voor een projectbestand dat de sleutel alsnog zou zetten, dus niet stil
doen: melden in de release notes.

**Wel treffers → adopteren, niet weghalen.** Dan is het geen legacy maar ongedocumenteerd,
en hoort het dezelfde behandeling te krijgen als de twee lagen ernaast: opnemen in de
`user-env-vars`-systeemdienst als tweede `owned_property`, zodat het door
`validate_service_configs` loopt, en beslissen of platte tekst op de hoogste voorrang
acceptabel is. Kijk in dat geval eerst wát erin staat.

## Wat bewust niet gedaan is

Niet gemodelleerd en niet verwijderd. Modelleren zou een sleutel legitimeren die er
waarschijnlijk niet hoort te zijn, en verwijderen zonder de meting is gokken op andermans
projectbestand. Het staat er dus nog precies zoals het stond; alleen deze notitie is
nieuw.

## Herkomst

Gevonden tijdens RC-25 (`plans/env-vars-en-aliassen-als-systeemdienst.md`), bij het
modelleren van `user-env-vars` en `aliases` als systeemdienst. Die PR raakt deze sleutel
niet: het samenvoegblok in `project_manager.py` is byte-identiek aan de basiscommit.
