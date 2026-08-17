# Keycloak-config bewerkbaar, zonder dat er iets verdwijnt

Status: plan, 12 augustus 2026. Gemeten op `operations-manager/python` op `naar-het-nieuwe-componentensysteem`.

## Twee dingen tegelijk, en de volgorde is niet vrij

De aanleiding is dat een deel van de keycloak-config niet via de UI te bewerken is, met `additional_redirect_uris` als voorbeeld. Dat is de vraag. Maar bij het uitzoeken kwam er iets onder vandaan dat eerst moet: **wie vandaag zo'n blok via de UI bewerkt, raakt de inhoud kwijt.** In productie zijn `additional-clients`-vermeldingen daadwerkelijk weggeschreven.

Een veld bewerkbaar maken in een systeem dat lijsten leegschrijft, maakt het probleem groter en niet kleiner: dan is er ineens een knop die de schade veroorzaakt. Dus eerst de val dichten, dan de velden aanbieden.

## De directe oorzaak: geregistreerd maar niet getekend

`catalog/keycloak/__init__.py` bouwt de configsectie met twee lijsten die verschillende dingen doen. `editables=[...]` zegt welke velden bij deze sectie horen, `layout=[...]` zegt wat er op het scherm komt.

```python
editables=[
    KEYCLOAK_TEMPLATE, KEYCLOAK_REDIRECT_URIS, KEYCLOAK_RESTRICT_ACCESS,
    KEYCLOAK_RESTRICT_ACCESS_ROLE, KEYCLOAK_RESTRICT_ACCESS_ERROR_MSG,
    KEYCLOAK_ADDITIONAL_CLIENTS,
],
layout=[
    Fieldset(legend="Template", ...),
    Fieldset(legend="Toegangsbeperking", ...),
    Fieldset(legend="Extra Keycloak clients", ...),
]
```

`KEYCLOAK_REDIRECT_URIS` staat in de eerste lijst en in de tweede niet. Het veld is dus **geregistreerd maar niet getekend**, en dat is de slechtst denkbare combinatie van de twee: omdat het in `editables` staat telt het mee voor wat de stroom mag schrijven, en omdat het niet in `layout` staat toont het formulier het nooit en dient het dus altijd leeg in.

Elke keer dat iemand de Keycloak-configuratie opslaat, schrijft het scherm daarmee een lege `additional_redirect_uris` over wat er stond. Er is geen foutmelding, geen waarschuwing en geen zichtbaar veld dat leeg lijkt: het veld bestaat op het scherm gewoon niet.

Dat de twee lijsten uit elkaar kunnen lopen zonder dat iets klaagt, is het eigenlijke ontwerpprobleem. Een editable die in `editables` staat maar in geen enkele layout voorkomt, is ofwel een vergeten veld ofwel een schrijfrecht dat niemand bedoeld heeft, en in beide gevallen hoort dat op te vallen.

## De val eronder, precies

`opi/forms/editables/merge.py` is de enige deep merge die de formulierpijplijn gebruikt, en hij is expliciet over lijsten:

```python
"""Recursively overlay *overlay* onto *base*, in place. Non-dict values replace."""
```

Een lijst is geen dict, dus hij wordt in zijn geheel vervangen. Elke wizardsectie draagt een volledige kopie van de projectgegevens (`services_merge.py` noemt dat: "Wizard sections each contribute a full copy of `services`"). Een sectie die `additional-clients` niet bewerkt maar wel meedraagt met een lege lijst, overschrijft daarmee een gevulde lijst met `[]`.

Dat verklaart ook wat in een projectbestand te zien is:

```yaml
- keycloak:
    config:
      template: sso-only
      additional_redirect_uris:
        - http://localhost:8080/*
      additional-clients: []      # niet leeg begonnen: leeggeschreven
```

De bedoelde bescherming bestaat al en heet `write_set.py`, "What a flow is allowed to write, derived from the flow itself". De docstring vertelt ook waarom hij er is:

> Naming the fields to protect one by one does not hold - four separate leaks proved that. Deriving the permitted set from the flow does.

Alleen: `_writable_path()` kapt een pad af bij het eerste jokerteken, dus `additional-clients[*]/name` levert schrijfrecht op de **hele lijst** `services/keycloak/config/additional-clients`. Een stroom die dat veld kent mag de lijst dus vervangen, ook wanneer hij hem leeg indient. Default-deny op padniveau is hier niet genoeg, want het pad staat toe wat we juist willen tegenhouden.

## Wat er nu wel en niet bewerkbaar is

Gemeten aan `KeycloakConfig` tegen `catalog/keycloak/editables.py`:

| Veld | Editable? |
|---|---|
| `template` | ja |
| `additional_redirect_uris` | ja |
| `restrict-access/enabled`, `/realm-role`, `/error-message` | ja |
| `additional-clients[*]/name`, `/redirect-uris` | ja, maar zie hierboven |
| `realm-roles[*]/name`, `/description` | ja |
| `restrict-access/role` | **nee** |
| `account-link` | **nee** |
| `variables` | **nee** |
| `realms` | nee, en terecht: door het platform geschreven |

`restrict-access/role` is de opvallendste: het model kent zowel `role` (een clientrol) als `realm-role`, en de UI biedt alleen de tweede. Wie op een clientrol wil beperken kan dat niet via het scherm, en wat er met een handgeschreven waarde gebeurt bij een UI-bewerking is precies de vraag die het eerste deel van dit plan beantwoordt.

## Nog een inconsistentie, klein maar verwarrend

`additional_redirect_uris` is het enige samengestelde veld **zonder** koppelteken-alias. Alle broers en zussen hebben er wel een: `restrict-access`, `additional-clients`, `realm-roles`, `account-link`. In hetzelfde configblok staan dus twee schrijfwijzen door elkaar, en dat is precies het soort ding waar iemand een keer `additional-redirect-uris` typt en niets ziet gebeuren. `KeycloakConfig` heeft `extra="allow"`, dus zo'n typefout wordt stil geaccepteerd en genegeerd.

Een alias toevoegen is goedkoop en achterwaarts compatibel (`populate_by_name=True` staat al aan). Wat het niet mag worden is een migratie die bestaande bestanden herschrijft: beide schrijfwijzen lezen is genoeg, en een `extra="allow"` die typefouten slikt is een eigen gesprek dat hier niet in hoort.

## De fasering

**Fase 0: `additional_redirect_uris` op het scherm, en een poort op de kloof.** Het veld krijgt zijn plek in `layout`, in een eigen fieldset of bij de template. En er komt een controle die faalt zodra een dienst een editable in `editables` heeft staan die in geen enkele layout van diezelfde sectie voorkomt. Datagedreven over de catalogus, niet met de hand voor keycloak, want dit kan bij elke dienst gebeuren en het is bij deze dienst alleen toevallig opgemerkt. Verifieerbaar: die controle faalt aantoonbaar op de huidige toestand en slaagt na de reparatie, en hij vindt in één keer of andere diensten hetzelfde gat hebben.

**Fase 1: een lijst kan niet meer per ongeluk leeg.** Een sectie die een lijst niet daadwerkelijk heeft gerenderd, mag hem niet vervangen. De vorm die daarbij past is de bestaande: leid het af van de stroom in plaats van veldnamen op te sommen, want dat is de les die al in `write_set.py` staat opgeschreven. Concreet is de vraag die beantwoord moet worden: hoe onderscheidt de schrijfkant "de gebruiker heeft de laatste regel verwijderd" van "deze sectie ging er niet over". Dat verschil is de hele fase; wie het niet expliciet maakt, verruilt gegevensverlies voor een lijst die je niet meer leeg kunt maken.

Verifieerbaar op de uitkomst: een project met twee `additional-clients` gaat door een UI-bewerking van een andere sectie en houdt zijn twee vermeldingen, gemeten op het opgeslagen bestand. En een bewerking die de lijst wél bewerkt en leegmaakt, levert wel degelijk een lege lijst op.

**Fase 2: een regressietest die dit klasse-breed dekt.** `test_modal_edit_nondestructive.py` beschermt deployments en componenten en kent geen enkele toets op dienstconfiguratie; dat is precies waarom dit kon gebeuren. Breid hem uit met de dienstlaag, met per lijstveld een geval. Doe dat datagedreven over de catalogus, niet met de hand per dienst: dan dekt hij ook `realm-roles`, de opslagmontages en wat er later bij komt.

**Fase 3: de ontbrekende velden aanbieden.** `restrict-access/role`, `account-link` en `variables` krijgen een editable, met de visualizers erbij zodat ze op het scherm komen. `variables` verdient een eigen afweging: het is een vrije sleutel-waardeafbeelding die de realm-template invult, en dat is iets anders dan een lijstje met een vaste vorm. Als het niet veilig te bedienen is, hoort het expliciet buiten de UI te blijven staan met een reden in de dienstdocumentatie, in plaats van er stil buiten te vallen.

**Fase 4: de alias.** `additional-redirect-uris` als alias naast `additional_redirect_uris`, zodat het configblok één schrijfwijze kent. Geen migratie, geen herschrijving van bestaande bestanden.

## Waar op te letten

**Dit is een gegevensverliesbug, geen vormgevingsklus.** De verleiding is om met fase 3 te beginnen, want dat is de gevraagde functionaliteit. Maar zolang fase 1 er niet is, bouw je een scherm dat gegevens weggooit.

**Toets op het opgeslagen bestand.** Elke bevinding in dit plan komt uit wat er in het projectbestand terechtkomt, niet uit een tussenfunctie. Een test die de merge-functie los aanroept had dit nooit gevonden, want die functie doet precies wat zijn docstring belooft.

**De regel staat al ergens.** `write_set.py` bestaat omdat vier eerdere lekken bewezen dat velden een voor een beschermen niet werkt. Wie hier een uitzonderingslijst voor `additional-clients` toevoegt, herhaalt de fout die dat bestand documenteert.

**`extra="allow"` maakt fouten stil.** Zowel `KeycloakConfig` als `KeycloakClientEntry` accepteren onbekende sleutels. Dat is met opzet, want redirect-uris en andere geavanceerde sleutels zijn bewust doorlaatpost. Het betekent wel dat een typefout geen foutmelding geeft, en dat is een eigen punt dat hier alleen gemeld wordt.
