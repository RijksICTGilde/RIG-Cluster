# De restore hernoemt het verkeerde schema, en gooit soms het goede weg

Een backup terugzetten meldt succes, en daarna staat de applicatie voor een schema met de
verkeerde tabellen erin. De data is er nog, onder een naam waar niemand naar kijkt. Bij een
project met een extra schema is dat de normale afloop van de tweede generatie-restore, niet
een randgeval.

De metingen hieronder zijn van één sessie, op een wegwerp-PostgreSQL 16 in Docker, met de
shell-logica letterlijk overgenomen uit het sjabloon. **Meet het opnieuw voordat je bouwt**,
en meet daarna op het cluster: dit gaat over data, en een redenering is hier niet genoeg.

## Wat er nu gebeurt

De backup is in orde en hoeft niet te veranderen. `manifests/backup-database-pod.yaml.jinja:83`
draait `pg_dump --format=custom` zonder `-n`, dus de héle database gaat mee met alle schema's.
Dat is ook wat `features/postgresql-scope-and-schemas.md` belooft.

Het gaat mis in de restore. `manifests/restore-database-pod.yaml.jinja:222-226` moet het
bronschema hernoemen naar de doelnaam, want de generatie zit in de databasenaam én in het
standaardschema (`opi/manager/database_manager.py:208`, `db_schema = db_database`). Dat gaat zo:

```sh
SOURCE_SCHEMA=$(pg_restore --list "$DUMP_PATH" | grep " SCHEMA - " | head -1 | awk '{print $6}')
...
DROP SCHEMA IF EXISTS ${TARGET_DB_NAME} CASCADE;
ALTER SCHEMA ${SOURCE_SCHEMA} RENAME TO ${TARGET_DB_NAME};
```

`head -1` gaat ervan uit dat er precies één schema is. Sinds RC-17 is dat niet zo: extra
schema's heten `{project}_{deployment}_{postfix}` en leven in dezelfde database
(`opi/utils/naming.py:549`, en de featuredoc zegt het met zoveel woorden). Bovendien sorteert
`pg_restore --list` de schema's **alfabetisch**, niet op aanmaakvolgorde — gemeten door het
extra schema als eerste aan te maken (lager OID) en te zien dat het alsnog achteraan stond.

Er wordt dus één schema hernoemd op grond van een alfabetische toevalligheid.

### Het scenario dat productie raakt

Een project met een extra schema, dat voor de **tweede** keer naar een nieuwe generatie wordt
hersteld. De dump van generatie 2 bevat `amt_prod_v2` (standaard, met generatie) en
`amt_prod_rapportage` (extra, nooit geversioneerd). `r` sorteert vóór `v`:

```
TOC:            amt_prod_rapportage        <- head -1 pakt deze
                amt_prod_v2
SOURCE_SCHEMA = amt_prod_rapportage        TARGET = amt_prod_v3
DROP SCHEMA amt_prod_v3 CASCADE
ALTER SCHEMA amt_prod_rapportage RENAME TO amt_prod_v3
```

Gemeten eindtoestand:

| | verwacht | werkelijk |
|---|---|---|
| `DATABASE_SCHEMA` (`amt_prod_v3`) | `klanten` | `cijfers`, de rapportagetabel |
| applicatiedata | in `amt_prod_v3` | staat nog in `amt_prod_v2`, ongebruikt |
| `DATABASE_SCHEMA_RAPPORTAGE` | `amt_prod_rapportage` | bestaat niet meer, weggehernoemd |

De restore meldt succes. Twee soorten data tegelijk onzichtbaar.

De **eerste** generatie-restore gaat wél goed: `amt_prod` is een prefix van
`amt_prod_rapportage` en sorteert daarom vooraan. Het breekt pas bij de tweede. Postfixes die
met `w`, `x`, `y` of `z` beginnen zijn toevallig veilig, alles vóór `v` niet — en dat zijn
zowat alle echte namen (`rapportage`, `analyse`, `archief`, `logging`, `staging`). Dat het één
keer goed gaat en daarna niet meer is precies waarom niemand dit heeft gezien.

### Twee bijvangsten, ook nagemeten

1. **Een zelfgemaakt schema kan het overnemen.** Een project is eigenaar van zijn database en
   mag schema's maken. Heet er één alfabetisch eerder (`aaa_werkmap`), dan wordt díe hernoemd
   naar de doelnaam. Gemeten: het doelschema bevatte `tmp` in plaats van `klanten`.
2. **`DROP SCHEMA ... CASCADE` kan net teruggezette data vernietigen.** Bevat de dump een
   schema dat exact zo heet als de doeldatabase, en sorteert een ander schema ervóór, dan
   gooit de DROP weg wat `pg_restore` er zojuist in zette. Gemeten, met
   `NOTICE: drop cascades to table amt_prod.klanten` als bewijs.

### Wat er verder van belang is

- Alleen `restore-database-pod.yaml.jinja` heeft deze logica; de andere restore-sjablonen niet.
- Er is **geen test** op de hernoemstap. `tests/test_restore_target_fault.py` dekt alleen de
  onbereikbare doeldatabase.
- Restore naar dezelfde naam slaat de hernoemstap over en gaat goed. Het probleem zit
  uitsluitend op het pad waar de naam verandert: een nieuwe generatie
  (`opi/api/restore_router.py:2427`) of een expliciete `target_database_name`.
- De identifiers gaan ongequote de SQL in. Onze eigen namen zijn `[a-z0-9_]`, maar
  `SOURCE_SCHEMA` komt uit de dump en is dus niet van ons.

## Wat er moet komen

1. **Kies het bronschema op wat het IS, niet op alfabet.** Het standaardschema is te herkennen
   aan de naamgeving die OPI zelf hanteert: het is de databasenaam van de bron. Zet die naam
   in het manifest mee vanuit Python (waar `generate_database_name` al bekend is) in plaats van
   hem uit de dump te raden. Kan de bronnaam niet worden vastgesteld, stop dan met een
   leesbare fout — een restore die gokt is erger dan een restore die weigert.
2. **Laat de extra schema's met rust.** Zij dragen geen generatie, dus hun naam is in de
   doeldatabase al goed. Alleen het standaardschema wordt hernoemd.
3. **Maak de `DROP` veilig.** Alleen het lege doelschema mag weg dat bij het opzetten is
   gemaakt. Staat er iets in, dan is dat teruggezette data en moet de restore stoppen in
   plaats van hem weg te gooien.
4. **Quote de identifiers.** `ALTER SCHEMA "x" RENAME TO "y"`, en weiger een naam die niet
   door de eigen naamgevingsregel komt.
5. **Zeg wat er gebeurd is.** De restore logt nu alleen "completed successfully". Noem welk
   schema is hernoemd en welke schema's ongemoeid zijn gelaten, zodat een mislukking in het
   log zichtbaar is en niet pas in de applicatie.

## Wat er buiten valt

De backup zelf. Die dumpt de hele database en dat is goed. Raak `backup-database-pod.yaml.jinja`
niet aan, behalve als de meting laat zien dat er iets ontbreekt wat de restore nodig heeft.

De kloonweg (`clone_schema`) kopieert elk schema via een eigen pijplijn en heeft dit probleem
niet; kijk er wel even naar, maar los het niet mee op als het goed is.

## Verifieerbaar

- Een test die de dump-TOC-volgorde vastlegt: extra schema eerst aangemaakt, standaardschema
  daarna, en de keuze moet nog steeds het standaardschema zijn. Rood zonder de fix.
- Een test voor de tweede generatie-restore: dump met `{db}_v2` + `{db}_{postfix}`, doel
  `{db}_v3`. Na afloop staat de applicatiedata in `{db}_v3` en heet het extra schema nog
  steeds `{db}_{postfix}`. Rood zonder de fix.
- Een test dat een gevuld doelschema de restore laat stoppen in plaats van het weg te gooien.
- Op het cluster gemeten: een project met een extra schema, twee keer achter elkaar
  teruggezet naar een nieuwe generatie, en daarna in de pod controleren dat de applicatie zijn
  eigen tabellen ziet en dat `DATABASE_SCHEMA_{POSTFIX}` naar een bestaand schema wijst.
  De applicatie-images zijn distroless, dus lezen via een ephemeral debug-container op
  `/proc/1` (zie `tests/e2e/helpers/cluster.py`, RC-119).
- `uv run pytest tests/ -q` groen, plus `ruff check`, `ruff format`, `pyright`.
