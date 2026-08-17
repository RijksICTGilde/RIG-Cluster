# Alles van een dienst in één map

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: een dienst overnemen en aanpassen zou moeten zijn "kopieer de map en pas hem aan", en dat kan nog niet.

## De maatstaf

Niet "is het netjes verdeeld", maar: **kun je een map kopiëren, hernoemen, en werkt het dan zoals je verwacht?** Als het antwoord ja is, is een nieuwe dienst maken navolgbaar en reproduceerbaar, en hoef je niet te weten in welke gedeelde bestanden je nog iets moet bijschrijven.

Vandaag is het antwoord nee, en het scheelt drie dingen.

## Wat er al goed staat

Het servicepakket draagt inmiddels het meeste. Voor keycloak:

```
opi/services/catalog/keycloak/
    __init__.py            de Service-klasse met zijn haken
    config_model.py        het typemodel van zijn config
    editables.py           zijn velden
    visualizers.py         hoe die velden eruitzien
    keycloak.v1.0.json     het vastgelegde schemafragment
    section-detail.html.j2 zijn blok op de projectpagina
    otp-code.html.j2       zijn eigen fragment
```

Config, editables, weergaven, acties, toestand, keuzelijsten en het schemafragment zijn er allemaal naartoe verhuisd. Dat is de kant die al klopt.

## Wat er nog buiten staat

**1. Het `ServiceDefinition`-blok.** Eenentwintig ervan in `opi/services/services.py`, met naam, omschrijving, icoon, kleur, `binding`, `secret_class`, `requires`, `cleanup_strategy` en de variabelenlijst. Een nieuwe dienst toevoegen betekent dus een gedeeld bestand bewerken, en een dienst weghalen laat daar een gat achter.

**2. De variabelen-enum.** `KeycloakVariables` en zijn soortgenoten staan ook in `services.py`, terwijl ze puur van één dienst zijn.

**3. De uitlegtemplate.** Drieëntwintig bestanden in `opi/templates/help/`, terwijl `section-detail.html.j2` van diezelfde dienst wél in zijn eigen map staat. Twee sjablonen van één dienst, twee plekken.

## Wat centraal blijft, en waarom

`ServiceType` blijft waar hij is. Dat geldt voor alles wat abstract is over een dienst: de enum, de haakpunten, `ServiceBinding`, `ConfigLayer`, `CleanupStrategy`, het `Service`-basiscontract. Dat zijn de begrippen waarin diensten worden uitgedrukt, niet eigenschappen van één dienst.

Praktisch kan het ook niet anders: `ServiceType` is wat alles aan elkaar knoopt, en een dienst die zijn eigen lid zou declareren geeft een importcirkel. Dat is vermoedelijk de reden dat deze verhuizing nooit is gedaan.

## Voorstel

1. **De definitie verhuist naar het pakket.** Elke dienst declareert zijn eigen `ServiceDefinition` in zijn `__init__.py`, naast de haken die daar al staan. `services.py` bouwt de registry dan op uit wat de diensten zelf melden in plaats van uit een handgeschreven lijst.
2. **De variabelen-enum gaat mee**, naar dezelfde map.
3. **De uitlegtemplate gaat mee**, naast `section-detail.html.j2`. Dat vraagt dat de sjabloonlader ook in de servicepakketten kijkt; dat mechanisme bestaat al voor de andere sjablonen daar, dus het is aansluiten en niet bouwen.
4. **Eén test die de maatstaf bewaakt:** geen enkel gedeeld bestand noemt nog een dienst bij naam, en elke dienst levert zijn eigen definitie. Dat is wat "kopieer de map" waarmaakt, en zonder die test kruipt het terug.

## Volgorde

Dit is eenentwintig keer hetzelfde, dus doe het niet in één klap.

1. Het mechanisme bouwen met één dienst als bewoner, met de oude lijst er nog naast. Verifiëren: de registry levert dezelfde definitie als daarvoor, veld voor veld.
2. De overige twintig verhuizen, in groepen, met na elke groep dezelfde vergelijking.
3. De lijst in `services.py` weghalen zodra hij leeg is, en de variabelen-enums en uitlegtemplates meenemen.
4. De test uit punt 4 toevoegen, en pas dan is het af.

## Waar op te letten

**Importcirkels zijn hier het echte risico.** `services.py` mag de pakketten niet importeren als die pakketten `services.py` nodig hebben. De registry (`opi/services/registry.py`) doet dat vandaag al goed, dus kijk daar hoe het opgelost is voordat je iets nieuws verzint.

**De volgorde van de registry is zichtbaar.** `SERVICE_DEFINITIONS` is een dict en de invoegvolgorde bepaalt hoe diensten in de keuzelijst staan. Bij het opbouwen uit pakketten is die volgorde niet vanzelf gelijk; leg hem expliciet vast, anders verschuift de UI zonder dat iemand daarom vroeg.

**Doe het niet half.** Twintig diensten verhuisd en één achtergebleven is slechter dan nul, want dan moet iemand twee patronen kennen. Als een dienst niet mee kan, schrijf dan op waarom, in zijn eigen map.

**De maatstaf is de test, niet het gevoel.** "Alles bij elkaar" is pas waar als een gedeeld bestand geen enkele dienstnaam meer noemt. Meet dat, want het is precies het soort eigenschap dat langzaam wegsijpelt.
