# Uitleg bij elke service

Elke platformdienst heeft een lange uitleg die een gebruiker opent met het vraagteken naast
de servicenaam. De eenregelige omschrijving op de kaart is te kort om een keuze op te
baseren; de uitleg is waar iemand daadwerkelijk beslist of hij een service nodig heeft.

## Waar je het ziet

Op twee plekken, en op beide dezelfde:

- **De servicekeuze** in de aanmaak-wizard en in de modal *Services beheren*.
- **Het serviceoverzicht** (`/services`), waar je services naast elkaar vergelijkt.

Beide renderen de macro `service_block` uit
`opi/templates/widgets/_macros.html.j2` (icoon, naam, omschrijving, help-knop). Daarvoor
bouwde elk sjabloon zijn eigen servicekaart, en waren ze uit elkaar gegroeid: alleen de
wizard had de knop. Bouw dus geen tweede servicekaart.

Het vraagteken opent een modal
(`opi/templates/widgets/service_help_modal.html.j2`) die de uitleg ophaalt bij
`GET /forms/wizard/help/{template_name}` en toont. Voor die modal heeft een pagina
`/static/css/wizard.css` en `/static/js/wizard.js` nodig.

## Waar de teksten staan

Eén bestand per service in zijn eigen map: `opi/services/catalog/<pakket>/help.md`, met
`help_template="<pakket>/help.md"` op de `ServiceDefinition` van diezelfde service. De
uitleg hoort bij de service, dus staat hij naast zijn andere sjablonen en niet in een
gedeelde map (RC-36).

**Het is markdown, en dat is de enige bron** (RC-59). Twee lezers, één bestand: de portal
rendert het naar dezelfde ROOS-componenten die de modal altijd toonde, en
`GET /api/v2/services/{name}` geeft het onbewerkt terug in `explanation`. Daarvoor stond de
proza in componentopmaak, en dan krijgt een client of een agent die vraagt wat een dienst
doet een lap HTML met `utrecht-*`-klassen en zinnen die naar knoppen wijzen. Een `help.md`
naast een `help.html.j2` is geen oplossing maar twee bronnen die uit elkaar gaan lopen.

De omzetting staat in `opi/services/help_text.py` en kent bewust weinig:

| Markdown | Wordt |
|---|---|
| `# Titel` | `<c-heading type="h2">` met het icoon van de service |
| `## Kopje` | `<c-heading type="h3">` |
| een alinea | `<c-p>` |
| `- regel` | `<c-ul>` / `<c-li>` |
| `**vet**` | `<c-strong>` |
| `\*` | een letterlijke asterisk naast de vet-tekens |

Meer syntaxis zou een component vragen dat niet bestaat, en een vorm die in één van de twee
lezers als letterlijke tekens verschijnt is erger dan een vorm die er niet is. Het icoon
staat niet in de markdown maar op de definitie, waar het voor de kaart al stond, zodat de
modal en de kaart niet uit elkaar kunnen lopen. Accolades in de proza worden ontsmet
(`&#123;`), want de markup gaat door de Jinja-omgeving heen.

Dezelfde haak bestaat op veldniveau (`Editable.help_template`, bijvoorbeeld
`container-image.html.j2`) en werkt via dezelfde modal. Uitleg die van geen enkele
service is blijft als Jinja-sjabloon in `opi/templates/help/`; de route herkent alle
vormen aan de extensie en aan het mapsegment.

## De vorm van een uitleg

1. Kop met het icoon en de kleur van de service zelf, zodat de modal en de kaart bij elkaar
   horen.
2. Eén alinea **wat is het**, in gewone taal.
3. **Wanneer gebruik je dit?** als lijstje met herkenbare situaties.
4. **Wat wordt er ingesteld?** met wat er technisch gebeurt, welke variabelen je component
   krijgt en welke andere services meekomen.

Twee afwijkingen:

- **Systeemdiensten** (`platform`, `resource-tuning`, `user-env-vars`, `aliases`) kan een
  gebruiker niet aanvinken. Die hebben geen "wanneer gebruik je dit"; leg in plaats daarvan
  uit dat ze altijd draaien en wat ze voor je doen zonder dat je iets kiest.
- **Diensten die op elkaar lijken** (`redis` en `namespace-redis`, `postgresql-database` en
  `namespace-postgresql-database`, `persistent-storage` en `temp-storage`) benoemen expliciet
  waarin ze verschillen en wanneer je welke kiest. Dat is de vraag waar een gebruiker op
  vastloopt.

Houd het kort: liever acht regels die kloppen dan dertig die niemand leest.

## Een nieuwe service

Schrijf het bestand, zet `help_template` op de definitie, klaar. `tests/test_service_help.py`
bewaakt dat, want beide fouten falen stil in de UI:

| Fout | Wat de gebruiker ziet |
|---|---|
| Geen `help_template` | Geen vraagteken, geen foutmelding |
| `help_template` wijst naar een bestand dat niet bestaat | Wel een vraagteken, "Help-informatie kon niet geladen worden" |
| Een sjabloon dat niet rendert | Idem, pas zichtbaar bij het klikken |
| Een sjabloon zonder het icoon van de service | Modal en kaart horen zichtbaar niet bij elkaar |
| Nog een `help.html.j2` naast de `help.md` | Twee bronnen; de niet-gerenderde veroudert stil |

De test controleert ook dat de servicekeuze en het overzicht de macro blijven gebruiken en
niet opnieuw hun eigen kaart bouwen.

```bash
cd operations-manager/python
uv run pytest tests/test_service_help.py tests/test_service_help_markdown.py -q
```

## Verwant

- `instructions/services.md` - het servicesysteem, en het stappenplan voor een nieuwe service
- `features/service-describe-api.md` - de andere lezer van dezelfde tekst
