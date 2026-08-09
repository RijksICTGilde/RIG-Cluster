# Alles van een dienst in één map

Een dienst overnemen en aanpassen is "kopieer de map, hernoem hem, en pas hem aan". Alles
wat een dienst *is* staat in zijn eigen map onder `opi/services/catalog/<pakket>/`; geen
enkel gedeeld bestand beschrijft nog een afzonderlijke dienst.

## Wat er in de map staat

```
opi/services/catalog/keycloak/
├── __init__.py             de Service-subklasse met zijn ServiceDefinition en zijn haken
├── config_model.py         het typemodel van zijn config
├── editables.py            zijn velden
├── variables.py            de omgevingsvariabelen die hij aan een deployment geeft
├── visualizers.py          hoe die velden eruitzien
├── help.md                 de uitleg achter het vraagteken (markdown, RC-59)
├── section-detail.html.j2  zijn blok op de projectpagina
└── keycloak.v1.0.json      het vastgelegde schemafragment
```

Alleen `__init__.py` is verplicht.

## Hoe het werkt

**De definitie.** Elke `Service`-subklasse declareert zijn eigen `definition` als
klasse-attribuut, naast `service_type`. `Service.__init_subclass__` weigert een subklasse
die wel een `service_type` maar geen `definition` opgeeft, dus dit kan niet half.

```python
class KeycloakService(Service):
    service_type = ServiceType.KEYCLOAK
    definition = ServiceDefinition(
        name="Keycloak Authentication",
        description="Inloggen via SSO Rijk en via lokale Keycloak-accounts ...",
        help_template="keycloak/help.md",
        icon="sleutel",
        color="groen",
        binding=ServiceBinding.COMPONENT,
        variables=[var.value for var in KeycloakVariables],
        ...
    )
```

`opi/services/registry.py` stelt `SERVICE_DEFINITIONS` samen uit wat de diensten melden,
in de volgorde van `ServiceType`. Die volgorde is zichtbaar (de backup-labels en de
keuzelijst volgen hem), dus hij is expliciet vastgelegd in plaats van afgeleid van de
toevallige volgorde van `SERVICES`.

`ServiceAdapter.SERVICE_DEFINITIONS` blijft bestaan als lees-view op die samenstelling.
De opzoeking is uitgesteld tot eerste gebruik: de dienstpakketten importeren
`services.py` voor `ServiceDefinition`, dus `services.py` kan de registry niet op
moduleniveau importeren.

**De variabelen.** `variables.py` in de map van de dienst die ze levert. Twee varianten
van dezelfde dienst delen er één (`namespace-redis` leest `RedisVariables` uit het
redis-pakket), zoals ze ook hun manager en hun secret delen.

**De uitleg.** `help.md` in de map van de dienst, aangewezen als
`help_template="<pakket>/help.md"`. Het is markdown, en die ene bron wordt zowel door de
popup in het portaal als door `GET /api/v2/services/{name}` gelezen (RC-59). De
sjabloonlader heeft de catalogusmap al op
zijn zoekpad staan voor `section-detail.html.j2`, dus dit sluit daarop aan. De route
`GET /forms/wizard/help/{template_name}` accepteert beide vormen: met mapsegment (een
dienst) en zonder (de paar uitlegteksten die van geen enkele dienst zijn, zoals
`container-image.html.j2`).

## Wat centraal blijft, en waarom

`ServiceType`, de haakpunten, `ServiceBinding`, `ConfigLayer`, `CleanupStrategy` en het
`Service`-basiscontract. Dat zijn de begrippen waarin diensten worden uitgedrukt, geen
eigenschappen van één dienst. `ServiceType` kan ook niet anders: het is wat alles aan
elkaar knoopt, en een dienst die zijn eigen lid zou declareren geeft een importcirkel.

De registratieregel in `opi/services/registry.py` blijft ook: de map moet ergens
aangesloten worden. Dat is één regel, en de dekkingsbewaking faalt als je hem vergeet.

## De bewaking

`tests/test_service_package_is_self_contained.py` meet de maatstaf in plaats van erop te
vertrouwen:

- elke dienst declareert zijn eigen `definition`, in een module binnen de catalogus;
- `SERVICE_DEFINITIONS` is exact wat de diensten declareren, in `ServiceType`-volgorde;
- een subklasse zonder `definition` wordt geweigerd;
- geen enkel gedeeld bestand bouwt een `ServiceDefinition` of een `VariableDefinition`;
- de uitleg van elke dienst staat in zijn eigen map, en in `templates/help/` ligt geen
  dienstuitleg meer.

Zonder die test kruipt het terug: één dienst die "even hier" in een gedeeld bestand komt
te staan breekt vandaag niets, en een jaar later moet je twee patronen kennen.
