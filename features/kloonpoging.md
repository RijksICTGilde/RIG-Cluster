# Een afgebroken kloon afmaken (`clone-from.status.in-progress`)

Een deployment met `clone-from` kloont zijn database vooraan in de run, en pas aan het eind
van die run wordt `status.completed` vastgelegd. Breekt de run daartussen af (Keycloak weg,
ArgoCD weigert, een manifest faalt), dan maakt de volgende run de kloon af in plaats van er
een `_v1` naast te zetten.

## De vlag

```yaml
clone-from:
  type: deployment
  reference: production
  mode: once
  status:
    in-progress: true
```

- **Aan**: binnen `process_project` (niet bij een kloon via de API), vlak voordat de
  databasekloon begint, bij `mode: once` waarvan `completed` nog niet true is. Hij gaat met een
  eigen commit naar git (`Clone attempt started for ...`), zodat hij een fout later in de run
  overleeft. Staat hij er al, dan wordt er niets opnieuw geschreven. Een run die eerder
  stukgaat laat dus geen vlag achter.
- **Niet boven een doelschema dat er al stond.** Precies de schema's die een volgende run
  terugleest als "kloon afgerond" moeten nu nog ontbreken, anders zou die run een schema van
  voor de `clone-from` voor de kloon aanzien. De kloon weigert zo'n schema toch
  (`postgres.py:1840`), dus er gaat geen geslaagde kloon verloren.
- **Niet bij een nieuwe generatie** (failover of `force-clone`): de volgende run zoekt de
  database onder de vastgelegde generatie, en die gaat pas bij de save aan het eind naar schijf.
  Met de vlag zou hij de oude database voor de kloon aanzien. Zo'n run gedraagt zich als voorheen.
- **Uit**: zodra de kloon als afgerond wordt vastgelegd (`set_clone_status`).
- `mode: always` krijgt de vlag nooit: die kloont elke run in een nieuwe generatie.
- Een bestand zonder de vlag gedraagt zich als voorheen. Er is geen migratie.

## Wat een tweede poging doet

Alleen een vlag die al op schijf stond **voordat** deze run begon telt als "er liep een
poging". Dan geldt voor de database, zowel bij `type: deployment` als bij `remote-source`:

| Doeldatabase | Doelschema's | Wat er gebeurt |
|---|---|---|
| bestaat | alle aanwezig | niet opnieuw klonen; de kloon wordt gemeld zoals een geslaagde, dus de save aan het eind zet `completed` |
| bestaat | niet (allemaal) aanwezig | klonen in diezelfde database, geen `_v1` |
| bestaat niet | - | gewoon klonen |

`force-clone` (of `mode: always`) krijgt nog steeds een nieuwe generatie. De vlag onderdrukt
alleen de failover die afging op "database bestaat, maar er is geen generatie vastgelegd".
De grens van vijf generaties blijft staan voor het geval het schrijven van de vlag zelf faalt.

Verschil tussen de twee: `type: deployment` kijkt naar het doelschema plus de extra schema's en
roept `record_clone` en `report_clone_performed` aan; `remote-source` kijkt alleen naar het
doelschema (de extra schema's maakt het daarna leeg aan) en meldt alleen `report_clone_performed`.

## Een half schema blijft niet liggen

Faalt een kloon in een database die er al stond, dan worden de schema's die deze poging
aanmaakte weer weggegooid (ook het tussenschema onder de bronnaam). Alleen de namen van deze
kloon komen in aanmerking: dit is een levende tenantdatabase, en de schemalijst bevat ook wat
een andere sessie ondertussen aanmaakte. Faalt een drop, dan gaan de overige schema's alsnog
weg. Schema's die er voor de poging al waren blijven staan. Een database die de poging zelf
aanmaakte wordt bij `type: deployment`, zoals voorheen, in zijn geheel verwijderd; bij `remote-source` blijft die
staan en gaan alleen de nieuwe schema's weg. Zo betekent een overlevend doelschema altijd een
afgeronde kloon.

Een proces dat halverwege hard stopt (OOM, pod weg) ruimt niets op. Heet het bronschema anders
dan het doelschema, dan krijgt het doelschema zijn naam pas bij de laatste stap van de kloon en
wordt een half gekopieerd schema ook dan niet voor afgerond aangezien. Heten ze hetzelfde (een
`remote-source` met een bronschema met de doelnaam), dan is dat na een harde stop niet uit te sluiten.

## Wat niet verandert

- MinIO- en PVC-klonen op een tweede poging.
- Projecten die al zombie-databases (`_v1` ... `_v5`) hebben: die ruim je los op.

## Code en tests

- `ProjectFileHandler.mark_clone_in_progress` / `is_clone_in_progress`
- `ProjectManager.mark_clone_started` zet de vlag
- `ProjectManager.process_project` geeft `clone_interrupted` door via `ProvisionContext`
- `DatabaseManager._ensure_database_state` en `_execute_external_clone`
- `tests/test_clone_attempt_flag.py`, `tests/test_schrijvers_tegen_het_schema.py::test_de_kloonpoging_blijft_geldig_na_migratie`
