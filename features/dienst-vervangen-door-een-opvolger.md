# Een dienst vervangen door een opvolger

Een dienst in de catalogus kan verklaren dat hij opgevolgd is door een andere. Op een
cluster waar die opvolger er echt is verdwijnt de oude dienst voor wie hem nog niet heeft,
terwijl een project dat hem al draait hem houdt, ook op een nieuwe deployment. Zo kan een
dienst uitfaseren zonder dat er iets omvalt dat er al staat.

Eerste gebruik: `minio-storage`, opgevolgd door `seaweedfs-storage` (RC-382). MinIO
distribueert zijn images niet meer (`docs/KNOWN-ISSUES.md`) en de proef uit RC-331 kan wat
wij van MinIO gebruiken (`features/seaweedfs-objectopslag.md`).

## Hoe je het markeert

Twee regels in het dienstpakket:

```python
class MinioStorageService(BackupsPageMixin, Service):
    service_type = ServiceType.MINIO_STORAGE
    replaced_by = ServiceType.SEAWEEDFS_STORAGE
```

`Service.replaced_by` is de declaratie; `Service.replaced_on_cluster(cluster)` is de vraag
die de rest van de code stelt. Die is alleen waar als de opvolger op dat cluster
`available_on_cluster` is. Zolang de opvolger op een cluster ontbreekt, is er niets om naar
over te stappen en verandert daar dus niets: vandaag is dat productie, waar SeaweedFS niet
staat.

Niet te verwarren met `superseded_by_component_details`: dat is een feit op de
deploymentkaart over een component, en zegt niets over de catalogus.

## Wat er dan gebeurt

Twee lezers, dezelfde twee als bij `available_on_cluster`:

| Waar | Gedrag |
|---|---|
| De dienstenkiezer (`ServiceOptionsProvider`) | De kaart staat er niet meer voor een project dat de dienst niet heeft. Een project dat hem WEL heeft ziet de kaart nog, aangevinkt en uit te zetten, met twee korte pillen: "Vervangen door `<opvolger>`" en "Uitzetten kan niet terug". Kort, want een `<c-tag>` breekt niet af en een kaart van een derde kolom houdt ongeveer veertig tekens; het hele verhaal staat achter het vraagteken. |
| `validate_service_availability` (`opi/manager/project_validation.py`) | Weigert een deployment die de vervangen dienst erbij krijgt, met een melding die de opvolger noemt. Een deployment die hem al had, slaagt. |

De weigering is de regel die telt: een weggelaten kaart dekt alleen de wizard, en de API en
een met de hand geschreven projectbestand komen daar nooit langs. Beide paden lopen door
dezelfde validatie, dus een API-aanroep krijgt dezelfde weigering als het formulier.

### Wat "had hem al" betekent

Per PROJECT: gebruikte een deployment in de vorige versie van het projectbestand
(`previous`, die `validate_project_structure` al meekrijgt) de dienst, dan mag elke
deployment van dat project hem gebruiken. Gelezen met
`ProjectFileHandler.deployment_uses_service`, zodat helm-charts en helmfiles meetellen.
Per project en niet per deployment, omdat een PR-preview een verse kloon is van een
deployment die de dienst al draaide: per deployment weigerde dat elke nieuwe preview
(wies, `pr-803`). Daaruit volgt:

- een nieuw project heeft geen vorige versie, dus elke keuze is nieuw: geweigerd;
- een bestaand project waarin geen deployment de dienst gebruikt, en dat hem erbij krijgt:
  geweigerd;
- een nieuwe deployment in een project dat hem al gebruikt, zoals een PR-preview:
  toegestaan;
- de deployment die hem al gebruikte: toegestaan, ook bij elke volgende save, reprocess en
  replay;
- de dienst staat in de projectlijst maar geen enkele deployment gebruikt hem: er is niets
  dat hem erbij krijgt, dus geen weigering. Een project dat zo opgeslagen staat blijft op
  te slaan.

### Uitzetten is definitief

De kaart van een vervangen dienst is uit te vinken, en de dienstenlijst leest dan precies
wat de kiezer aanbood (`offered_selection_values` krijgt dezelfde `project_services`-context
als de renderer). Gebruikt daarna geen enkele deployment van het project de dienst nog,
dan is hij voor het project nieuw, en dus geweigerd. Zolang een andere deployment hem nog
draait, is terugzetten wel mogelijk. De melding op de kaart zegt dat vooraf.

## Wat blijft werken

Alles van een deployment die de dienst al heeft: de bucket, de back-up, de restore, de
configuratiestap en de knoppen op de deploymentkaart. De vervanging raakt alleen wie de
dienst erbij zou krijgen.

## Gevolg voor de sandbox-e2e

SeaweedFS staat op de sandbox, dus daar kan een nieuw project geen `minio-storage` meer
kiezen. De suites die objectopslag meten draaien daarom op `seaweedfs-storage`
(`tests/e2e/helpers/lifecycle.py`, `test_sandbox_all_services.py`,
`test_sandbox_service_config_ui.py`). `tests/e2e/test_dienst_met_configstap_uitvinken.py`
draait tegen een testserver met `CLUSTER_MANAGER=local`, waar MinIO niet vervangen is, en
blijft de meting dat een MinIO-project doorwerkt.

De buckethelft van `test_sandbox_restore_van_buiten.py` is weg: SeaweedFS heeft nog geen
back-up, en een nieuw MinIO-project kan de suite niet meer aanmaken. Die meting hoort bij de
migratie van de bestaande buckets.

## Afhankelijkheden

Geen. De declaratie staat in het dienstpakket, de twee lezers bestonden al voor
`available_on_cluster`.
