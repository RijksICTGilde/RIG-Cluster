# Opslag boven 2Gi op aanvraag

Een volume van `persistent-storage` of `temp-storage` mag tot 50Gi groot zijn. Tot en met
2Gi kies je zelf; daarboven keurt een platformbeheerder een **plafond** voor de dienst goed,
en tot die goedkeuring rendert het volume op wat al mocht. Een deployment die meer nodig
heeft dan de rest krijgt een eigen plafond.

Het besluit gaat over de dienst, niet over een partitie: "persistent-storage mag in dit
project volumes tot 5Gi". Een plafond van 20Gi geldt dus voor elk volume van die dienst, in
elke deployment. Er is geen totaalbudget.

```
component zegt:       data: 5Gi
zonder goedkeuring:   PVC van 2Gi, met een melding op de projectpagina
na goedkeuring:       PVC van 5Gi, en elk ander volume van die dienst mag ook tot 5Gi

deployment "productie" zegt voor data: 20Gi
zonder goedkeuring:   PVC van 5Gi (het plafond van het project)
na goedkeuring:       PVC van 20Gi, alleen voor die deployment
```

## De maten en het bereik

De keuzelijst biedt 100Mi, 250Mi, 500Mi, 1Gi, 2Gi, 5Gi, 10Gi, 20Gi en 50Gi. Die reeks is
een gemak, geen regel: het formulier en de API nemen elke Kubernetes-hoeveelheid van 50Mi
tot en met 50Gi aan, dus `3Gi` en `750Mi` mogen ook. Een opgeslagen waarde buiten de reeks
blijft in de select staan als eigen waarde, want een volume kan niet krimpen en de select
mag je geen voorstel aanbieden dat de opslag weigert.

Een nieuw volume begint op 100Mi. Begin klein: groeien kan, krimpen niet.

| | waarde |
|---|---|
| minimum | `50Mi` |
| maximum | `50Gi` |
| zonder aanvraag tot en met | `2Gi` |
| startmaat van een nieuw volume | `100Mi` |

De getallen staan in `opi/services/catalog/shared/storage.py` (`MIN_STORAGE_SIZE`,
`MAX_STORAGE_SIZE`, `FREE_MAX_STORAGE_SIZE`, `DEFAULT_STORAGE_SIZE`); de reeks in
`STORAGE_SIZES`.

## Wat er geldt zolang er geen besluit is

Het plafond van het project is de grootste van 2Gi en het laatst goedgekeurde plafond. De
effectieve grootte van een volume is de kleinste van de gekozen grootte en dat plafond. Een
open aanvraag verlaagt dus nooit wat al gold, en kleiner kiezen geldt meteen.

De aanvraag is de grootste `size` over alle volumes van die dienst op de componentlaag, als
die boven het plafond ligt. Twee volumes van 5Gi en 10Gi geven dus een aanvraag: 10Gi.

Vergelijken gaat op hoeveelheid, nooit op tekst: `5Gi` en `5120Mi` zijn dezelfde aanvraag en
dezelfde goedkeuring.

## Erven en afwijken per deployment

Het plafond van het project geldt voor elke deployment. Een deployment mag per volume een
eigen `size` zetten; blijft die binnen het plafond van het project, dan vraagt dat niets, ook
niet als hij kleiner is.

Gaat een override erboven, dan vraagt die deployment een eigen plafond aan, en dat besluit
geldt alleen voor die deployment. De aanvraag is de grootste override van die dienst in die
deployment. Het plafond van een deployment is de grootste van het projectplafond en het
laatst goedgekeurde deploymentplafond, dus een goedgekeurd plafond blijft staan als het
projectplafond later omlaag gaat. Een tweede deployment houdt het projectplafond.

Net als op projectniveau geldt dat plafond voor elk volume van die dienst in die deployment,
ook voor een volume dat geen eigen override heeft: de effectieve grootte is de kleinste van
de gekozen grootte en het plafond van die deployment.

Een deployment die niets invult volgt het component.

## In het projectbestand

Op het component, per volume:

```yaml
components:
  - name: loep-service
    services:
      - reference: persistent-storage
        config:
          - name: data
            size: 5Gi
            mount-path: /data
```

Het plafond staat op projectniveau, onder de dienst, als een enkel blok. Dat blok schrijft
alleen de goedkeuringsroute:

```yaml
services:
  - name: persistent-storage
    config:
      size-approval:
        status: approved
        value: 5Gi
        approved-value: 5Gi
        history:
          - date: '2026-11-16T10:00:00+00:00'
            status: approved
            by: beheerder@rijksoverheid.nl
```

De override van een deployment staat in het record per volume, naast de PVC-generatie, en het
plafond van die deployment staat in zijn eigen dienstblok:

```yaml
deployments:
  - name: productie
    services:
      - name: persistent-storage
        config:
          size-approval:
            status: requested
            value: 20Gi
    components:
      - reference: loep-service
        services:
          persistent-storage:
            - reference: data
              config:
                size: 20Gi
                generation: 2
```

Het volume heet hier naar het mount-pad (`/data` wordt `data`), dezelfde naam waarmee het
renderpad de PVC noemt. Backup en restore gebruiken het `name`-veld van het component; die
twee sleutels naast elkaar is bekend en staat op een eigen kaartje.

## Via de API

De grootte op het component gaat via de bestaande componentroute:

```
PUT /api/v2/projects/{project}/services/persistent-storage/config/component/{component}
[{"name": "data", "size": "5Gi", "mount-path": "/data"}]
```

De override van een deployment gaat via de deployment-componentroute, nieuw voor deze
regel. De body is de grootte per volume, en niets anders:

```
PUT /api/v2/projects/{project}/services/persistent-storage/config/deployment/{deployment}/component/{component}
[{"reference": "data", "config": {"size": "20Gi"}}]
```

De server voegt dat samen met de records die er al staan: een volume dat je niet noemt houdt
wat het had, en `generation` en `revisions` blijven staan. Een body die een van die twee
meestuurt is een 422, en een `reference` die geen volume van dat component is ook. Laat je
`size` weg, dan vervalt de override en volgt de deployment het component weer.

Wil je één volume zetten zonder de andere mee te sturen, dan kan dat met een PATCH op
hetzelfde pad:

```
PATCH /api/v2/projects/{project}/services/persistent-storage/config/deployment/{deployment}/component/{component}
{"add": [{"reference": "data", "config": {"size": "20Gi"}}], "remove": []}
```

Dezelfde regels gelden: hetzelfde bereik, dezelfde 422 op een onbekende `reference`, en de
platformvelden van het record blijven staan. `remove` laat de override van een volume
vervallen: `generation` en `revisions` blijven staan, en een record dat daarna niets anders
meer bevat verdwijnt.

Een schrijfactie legt de ontbrekende aanvraag meteen vast, in dezelfde commit. Een
goedkeuring geven kan alleen via de aanvragenpagina, nooit via deze route.

## Wat de gebruiker ziet

Op de aanvragenpagina staat per dienst het gevraagde plafond en wat er nu geldt: "Opslag tot
20Gi per volume voor persistent-storage (nu 2Gi)", en voor een deployment "... in deployment
productie (nu 5Gi)". Op de projectpagina staat bij die aanvraag welke volumes voorlopig
kleiner renderen: "Voorlopig geldt: data op 2Gi".

## Krimpen

Kubernetes weigert een kleinere PVC, en dan faalt de sync van de hele deployment. De
opslagpoort weigert daarom een verlaging van de effectieve grootte van een persistent volume
bij het opslaan, met een melding die het volume noemt. `temp-storage` is een `emptyDir` en
mag wel kleiner. Een nieuwe generatie van een volume (een recreate via de opslagacties) is
een nieuwe PVC en dus geen krimp.

## Bestaande projecten

Schemaversie 2.19 zet per dienst het projectplafond op de grootste grootte die al boven 2Gi
stond, als goedgekeurd met `by: schema-migration` in de history, en geeft een deployment met
een override daarboven een eigen goedgekeurd plafond. Niemands volume gaat dus omlaag door
deze regel. Een vergroting daarna loopt langs de gewone aanvraag.

## Waar het staat

| Wat | Waar |
|---|---|
| maten, bereik en de modellen | `opi/services/catalog/shared/storage.py` |
| de regels: kiezen, toegestaan, aanvragen, effectief, krimpen | `opi/services/catalog/shared/storage_size.py` |
| de generieke approval-helpers (`config_value_approval`, `deployment_value_approval`) | `opi/services/catalog/approval.py` |
| de resolver die het renderpad leest | `storage_size.resolve_component_storage()` |
| de migratie | `opi/services/schema_migration.py`, stap 2.19 |

## Afhankelijkheden

Geen nieuwe. De regel leunt op het bestaande approval-systeem
(`features/aanvragen-beheerpagina.md`) en op de twee opslagdiensten, waarvan de hulptekst in
hun eigen pakket staat (`opi/services/catalog/{persistent,temp}_storage/help.md`).
