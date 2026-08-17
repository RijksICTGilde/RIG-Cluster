# Waar stel je een dienst in (en wat het scherm daarover zegt)

Vink je in de bewerk-wizard (`modal-edit-services`) een dienst aan, dan verwacht je een
configuratiescherm. Voor `sleep-mode` kwam dat, voor `health-check` niet: geen scherm, geen
foutmelding, geen toelichting. Waargenomen op `dimp-r0v`.

Dat was geen kapotte stap. De projectbrede dienstenstap kan alleen secties tonen voor
`ConfigLayer.PROJECT`, en `health-check` draagt zijn config uitsluitend op de componentlaag.
Het scherm wist dat wel, maar zei het niet.

## Twee begrippen die op elkaar leken

| | Beantwoordt | Lees het voor |
|---|---|---|
| `ServiceDefinition.binding` | Kiest een los component deze dienst, of krijgt een heel deployment hem | Selectie |
| `service.config_layers()` | Op welke niveaus van het projectbestand deze dienst instellingen draagt | Configuratie: op welk scherm je iets bewerkt |

Het `binding`-veld heette tot RC-33 `scope`, en dat las als een antwoord op "waar stel ik
dit in" -- terwijl het dat nooit was. De projectdetailkaart rendeerde het letterlijk als
"Component scope". Bij `keycloak` staat daar dus "component" terwijl zijn config
projectbreed is: één realm voor het hele project, elk component kiest alleen of het erachter
staat. Wie de verkeerde van de twee leest, vertelt de gebruiker iets onjuists.

De twee blijven bestaan (ze beantwoorden verschillende vragen) maar zijn uit elkaar
getrokken in naam en documentatie. `instructions/services.md` heeft de regel;
`tests/test_service_config_location.py` legt vast welke van de twee de bron van waarheid is,
met `keycloak` als tegenvoorbeeld, en faalt als de hele catalogus ooit gelijkloopt (dan is
samenvoegen de betere oplossing en zegt die test dat).

## De terugkoppeling

`opi/services/config_location.py` maakt van de lagen een zin:

```python
project_step_config_hint(ServiceType.HEALTH_CHECK)
# "Geen projectbrede instellingen; u stelt deze dienst per component, bij Componenten in."

project_step_config_hint(ServiceType.KEYCLOAK)
# None -- die heeft wel een projectsectie, er valt niets uit te leggen
```

Een aangevinkte kaart op de dienstenstap toont die regel; een niet-aangevinkte kaart niet.
De projectdetailpagina toont hem ook, onder de dienstbeschrijving, waar dezelfde dienst geen
"Configureer"-knop krijgt.

De zin is afgeleid uit `config_layers()` en `config_form_section(PROJECT)`, dus:

- er staat geen dienstnaam in een sjabloon;
- een nieuwe dienst krijgt de regel zonder dat er iets aan de UI verandert;
- een dienst die *wel* een projectsectie krijgt, verliest de regel automatisch.

Diensten die op geen enkele laag config dragen (`platform`, `namespace-redis`) zwijgen: er
is geen "maar hier dan wel" om naar te wijzen.

## Beoordeling per dienst: hoort er een projectsectie bij?

Gemeten op 5 augustus 2026 via de registry (niet via bestandsnamen -- `persistent-storage`
en `temp-storage` delen een config-model via `catalog/shared/storage.py` en lijken bij een
bestandsscan onterecht onvolledig). Zeven diensten dragen componentconfig zonder
projectsectie. Per dienst is de vraag inhoudelijk, niet mechanisch:

| Dienst | Componentconfig | Hoort er projectbrede config bij? |
|---|---|---|
| `publish-on-web` | `tls`-modus per component | Nee. TLS is per component een echte keuze (één component termineert zelf). Het projectbrede deel dat deze dienst al heeft -- het `domains`-blok -- wordt via de webadres-wizard en de goedkeuringsinterface bewerkt, niet via een configsectie. |
| `metrics-scraper` | scrape-poort en -pad | Nee. Elk component exposeert zijn eigen poort en pad; een projectbrede waarde zou nooit voor alle componenten kloppen. |
| `health-check` | schema, poort, liveness/readiness-pad | Nee. Idem: dit hangt aan het beeld dat het component draait. |
| `persistent-storage` | lijst volumes (naam, grootte, mountpad) | Nee. Een mountpad is per component. Wat projectbreed *wel* betekenis heeft is een opslagquotum, en dat is een platformgrens, geen dienstconfig; het bestaat vandaag niet en zou een eigen voorstel zijn. |
| `temp-storage` | idem | Nee, om dezelfde reden. |
| `user-env-vars` (systeem) | variabelen per component en per deployment-component | Nee. Projectbrede variabelen voor alle componenten zijn een aparte functiewens; RC-25 heeft bewust per component gekozen. Bereikbaar via het componentformulier en het env-vars-blok op de detailpagina. |
| `aliases` (systeem) | koppeling platformvariabele -> eigen naam | Nee. Een alias bestaat alleen in de context van één component. |

Uitkomst: geen van de zeven krijgt een projectsectie. Een lege of kunstmatige projectsectie
is slechter dan een zin die zegt waar je moet zijn -- en die zin staat er nu.

De twee systeemdiensten hebben geen kaart in de dienstenkiezer (`ServiceKind.SYSTEM`), dus
voor hen speelt de terugkoppeling op de dienstenstap niet; hun config is en blijft
bereikbaar in het componentformulier. Dat elke laag met config *ergens* bewerkbaar is, is
een aparte, al bestaande garantie: `tests/test_service_config_layers.py`
(zie `features/service-config-per-layer-editable.md`).

## Bestanden

- `opi/services/config_location.py` -- `project_step_config_hint`, `config_hint_for_value`,
  `binding_label`
- `opi/forms/widgets/roos.py` -- `render_service_cards` hangt de regel aan een aangevinkte kaart
- `opi/templates/widgets/service_cards.html.j2` -- rendert hem
- `opi/templates/project-details/section-services.html.j2` -- detailkaart: binding-label + regel
- `tests/test_service_config_location.py` -- bron van waarheid, de zeven diensten, de kaarten
