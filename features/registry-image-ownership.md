# Eigendom in de gedeelde container-registry

Wat het is: elke image die een project via de push-API uploadt, landt op een tag die het
bezittende project voorop draagt. Twee projecten kunnen daardoor niet meer op dezelfde
plek in de registry schrijven, en een deployment mag niet naar de image van een ander
project wijzen.

## Waarom dit zo is

Het platform pusht alles naar één registry-repository: de repo van het robot-account
(`{REGISTRY_URL}/{REGISTRY_ORG}`). Dat is geen slordigheid maar een gegeven van Quay, dat
geen geneste repositories onder één robot-account-scope kent. De tagruimte is dus plat, en
de tag is de enige plek waar eigendom kan wonen.

Tot deze wijziging stond daar niets: de bestemming werd gebouwd uit de `image_name` en
`tag` die de aanroeper zelf opgaf. Een project met een eigen geldige API-sleutel kon
daarmee naar de tag van een ander project schrijven. Omdat de gegenereerde manifesten
`imagePullPolicy: Always` gebruiken en niet op digest pinnen, haalde Kubernetes die image
bij de eerstvolgende herstart binnen en voerde hem uit in de namespace van het andere
project. Dat is bevinding A ("Kritiek") uit
`plans/technische-review-bio-en-nora-bevindingen.md`.

## Hoe de tag eruitziet

```
{REGISTRY_URL}/{REGISTRY_ORG}:{project}_{image_name}-{tag}
```

Bijvoorbeeld: `docker save mink-app:v1.0` gepusht door project `mink` als
`image_name=app&tag=v1.0` landt op `rcr.rijksapps.nl/rig/zad:mink_app-v1.0`.

Het scheidingsteken tussen de eigenaar en de rest is de **underscore**. Een projectnaam
matcht `^[a-z][a-z0-9-]*$` en kan dus nooit een underscore bevatten, terwijl een
image-naam dat wel mag. Het deel vóór de eerste underscore is daarmee ondubbelzinnig de
eigenaar, ook als de image-naam er zelf een bevat. Was het koppelteken de scheiding
geweest, dan zouden project `foo` met image `bar-backend` en project `foo-bar` met image
`backend` op dezelfde tag uitkomen; met de underscore kan dat niet.

De naamgeving staat op één plek: `build_registry_tag()` en `registry_tag_owner()` in
`opi/utils/naming.py`. Zowel de push als de leescontrole gebruiken die.

## Gebruik

```bash
docker save my-app:v1.0 -o /tmp/my-app.tar

curl -X POST "https://<ops-manager-url>/api/v1/projects/<project>/images/push?image_name=my-app&tag=v1.0" \
  -H "X-API-Key: <project-api-key>" \
  -F "file=@/tmp/my-app.tar"
```

Het antwoord bevat de volledige referentie onder `image`. **Gebruik die referentie**, bouw
hem niet zelf op: de tag bevat de projectnaam en die zetten wij erin, niet jij.

Geef je ook `deployment` en `component` mee, dan schrijft de push de nieuwe referentie
meteen in het projectbestand en rolt de deployment uit. Dat is de eenvoudigste weg, want
dan hoef je de referentie nergens over te typen.

De eigenaar komt van de **API-sleutel**, niet van het pad: `validate_api_token` overschrijft
`project_name` met de naam van het project dat bij de sleutel hoort. Een ander pad
opgeven verandert de eigenaar niet, het levert een 401.

## Wat er geweigerd wordt

| Situatie | Antwoord |
|---|---|
| Sleutel hoort niet bij `project_name` | 401 |
| `image_name` of `tag` voldoet niet aan de tekenset | 400 |
| De samengestelde tag wordt langer dan 128 tekens | 400, met de tag en de lengte in de melding |
| Deployment verwijst naar een tag van een ander project in de platformregistry | Opslaan wordt geweigerd (`ProjectIntegrityError`) |

## De leeskant

Pinnen aan de schrijfkant dekt maar de helft: een project kon ook de image van een ander
gewoon als eigen deployment-image opgeven en zo binnenhalen. Daarom controleert
`validate_platform_registry_image_ownership()` (aangeroepen vanuit
`validate_project_structure`, het enige punt waar elke schrijfweg langskomt) de
image-referenties van alle deployment-componenten.

De controle is bewust smal:

- ze kijkt **alleen** naar verwijzingen naar `{REGISTRY_URL}/{REGISTRY_ORG}`. Een image van
  ghcr.io, Docker Hub of een eigen registry van het project is niet aan ons — een
  witte lijst van registries zou legitiem gebruik breken;
- ze weigert alleen een tag met een eigenaar-prefix die van een **ander** project is;
- een tag **zonder** eigenaar-prefix (alles van vóór deze wijziging) blijft toegestaan.

## Migratie: wat gebeurt er met bestaande tags

Niets. Bestaande tags worden niet hernoemd en niet verwijderd. Een draaiende deployment
verwijst naar de referentie die in zijn projectbestand staat, die referentie verandert
niet, en de image achter die referentie blijft in de registry staan. Er is dus geen moment
waarop een herstart een andere image binnenhaalt dan gisteren — belangrijk, want met
`imagePullPolicy: Always` zou een verkeerde migratie pas bij de eerstvolgende herstart
zichtbaar worden.

Wat er wél verandert:

1. De **eerstvolgende push** van een project landt op een nieuwe, geprefixte tag. Ging de
   push mee met `deployment` en `component`, dan wordt het projectbestand meteen
   bijgewerkt en is er niets te doen. Zo niet, dan staat de nieuwe referentie in het
   antwoord en werk je die zelf bij.
2. De oude platte tags zijn **niet meer beschrijfbaar**. Elke push is voortaan geprefixt,
   dus niemand kan er nog overheen schrijven — ook de eigenaar niet. De oude tags zijn
   vanaf nu alleen-lezen historie, tot ze door een nieuwe push overbodig worden.

### Restrisico

Een oude, platte tag waarvan de image-naam toevallig begint met `{projectnaam}_` — dus
een image die iemand `mink_api` genoemd heeft terwijl er een project `mink` bestaat — valt
onder dezelfde naam als een nieuwe push van dat project. Dat vergt een naam die precies
samenvalt met de projectnaam van de ander, is niet door de aanvaller te sturen (de
projectnaam wordt gegenereerd), en de verzameling oude tags groeit niet meer. Het is
bewust niet dichtgezet met een registry-lookup per push: dat zou een netwerkafhankelijkheid
in het pushpad zetten voor een geval dat alleen kleiner wordt.

## Waar het staat

| Onderdeel | Bestand |
|---|---|
| Tagopbouw en eigenaar-afleiding | `opi/utils/naming.py` |
| Bestemming en validatie van het pushdoel | `opi/connectors/skopeo.py` |
| Endpoint | `opi/api/image_router.py` |
| Leescontrole | `opi/manager/project_validation.py` |
| Tests (twee projecten, twee sleutels) | `tests/test_tenant_isolation_registry.py` |
| Tests (connector) | `tests/test_skopeo_connector.py` |

## Afhankelijkheden

Skopeo in de container-image, en `REGISTRY_URL` / `REGISTRY_ORG` / `REGISTRY_USERNAME` /
`REGISTRY_PASSWORD` in de configuratie. Staat `REGISTRY_URL` of `REGISTRY_ORG` niet
ingesteld, dan is er geen platformregistry en doet de leescontrole niets; de push-endpoint
antwoordt dan met 501.
