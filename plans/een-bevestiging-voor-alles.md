# Eén bevestiging voor alles, ook voor verwijderen

Status: plan, 5 augustus 2026. **Deels gebouwd, opnieuw gemeten op 7 augustus.** De helft die Eric Wout meldde is dicht, maar niet op de manier die dit plan vraagt.

**Wat er sinds 5 augustus veranderd is, gemeten:**

- `showDangerConfirmation` en zijn hardgecodeerde titelmap zijn **weg** (RC-29). Verwijderen en herverwerken lopen langs dezelfde bevestiging als een service-actie.
- Het blokkeermechanisme werkt: `edit_modal.js` negeert Escape en klikken-buiten zolang `window.isEditSubmitting` aanstaat.

**Wat er nog steeds niet uniform is, en dat is de kern van dit plan:** het mechanisme is half gedeeld. De **blokkade** staat in de gedeelde module (`static/js/edit_modal.js`), maar de **trigger** die de vlag aanzet staat inline in één template (`project-details.html.j2`, rond regel 800). Vijf templates openen de gedeelde modal en drie gebruiken het voortgangsfragment; alleen de projectdetailpagina zet de vlag. Elke andere pagina die de modal opent met een lopende actie is dus onbeschermd, en dat is niet zichtbaar aan iets: het werkt gewoon niet.

Dat is precies wat dit plan wilde voorkomen. De regel hoort een eigenschap van het gedeelde pad te zijn, niet iets dat elke pagina zelf aanzet.

Niet gebouwd. Aanleiding: Eric Wout meldde dat de bevestigingsmodal weg te klikken is terwijl de actie nog loopt. Bij het uitzoeken bleek dat symptoom van iets groters, en bleek de helft van het probleem inmiddels vanzelf opgelost.

## Wat er inmiddels wel goed gaat

De projectpagina heeft sinds vandaag een generieke bevestiging voor service-acties. Die opent de gedeelde modal op een fragment, en het endpoint antwoordt met het gedeelde voortgangsfragment dat erin geswapt wordt.

Daar hangt een mechanisme aan dat precies Eric Wouts probleem afdekt:

```js
document.addEventListener('htmx:afterSwap', function(evt) {
    if (evt.detail.target.querySelector('.edit-progress-view')) window.isEditSubmitting = true;
    if (evt.detail.target.querySelector('.edit-progress-actions')) window.isEditSubmitting = false;
});
```

Zolang die vlag aan staat negeren Escape en klikken-buiten de modal. Het taak-voortgangsfragment draagt die klasse, dus slapen en wekken zijn nu vanzelf beschermd, zonder dat daar iets voor geschreven is.

## Wat er niet goed gaat

De **andere** dialoog, `showDangerConfirmation` in `templates/project-details.html.j2`, loopt daar helemaal buitenom. Die doet zes dingen:

| actionType | wat het doet |
|---|---|
| `project` | project verwijderen |
| `deployment` | deployment verwijderen |
| `component` | component verwijderen |
| `attachment` | bijlage verwijderen |
| `refresh` | project herverwerken |
| `deployment-refresh` | deployment herverwerken |

Hij verbergt bij het starten zijn eigen knoppen en toont een eigen voortgangsblok, dus je ziet een venster zonder knoppen met alles grijs. De Escape-afhandeling is alleen afgeschermd met `window.isEditSubmitting`, en die vlag wordt op dit pad nooit gezet, want er wordt geen `.edit-progress-view` ingeswapt. Er is daarnaast een klik-buiten-pad dat hetzelfde doet. Vandaar de melding.

Daar komt bij dat de dialoog een hardgecodeerde titelmap draagt:

```js
var titles = { project: 'Project verwijderen', deployment: 'Deployment verwijderen', ... };
```

Elke nieuwe bevestiging moet dus in die map, en in de knoplogica die per actionType kiest welke van de twee knoppen zichtbaar is. Dat is dezelfde vorm die we vandaag bij de service-acties juist hebben weggehaald.

## Voorstel

Los het niet op met een extra vlag, maar hef de oude dialoog op in de generieke. Dan verdwijnt Eric Wouts bug **per constructie**: het voortgangsfragment draagt de klasse die het wegklikken blokkeert, dus er valt niets te vergeten.

Wat dat concreet vraagt:

1. **Verwijderen en herverwerken langs dezelfde weg als een service-actie.** De generieke bevestiging is nu geadresseerd met een actiesleutel die uit de services van het project wordt afgeleid. Verwijderen komt niet van een service, dus daar moet een tweede, even smalle ingang voor komen die dezelfde eigenschap houdt: het doel-endpoint komt nooit uit het verzoek. Dat is de eis om scherp te houden, want dit pad verwijdert projecten.
2. **De titelmap en de knopkeuze vervallen.** De aanroeper zegt wat er staat en waar het heen post, net als een `DeploymentAction`.
3. **Herverwerken is al een taak** (`_create_task_and_render_progress`), dus die twee kunnen meteen het gedeelde voortgangsfragment gebruiken. Verwijderen moet je nakijken: als dat synchroon is, geldt daar hetzelfde verhaal als bij slapen, en dan is dit meteen de plek om er een taak van te maken.
4. **Een resultaat tonen in plaats van de gebruiker laten raden.** Dat stond al in de oorspronkelijke melding en is met het gedeelde fragment vanzelf zo.

## Wat er nog te doen is (7 augustus)

De vier punten hierboven zijn grotendeels gebouwd door RC-29. Wat resteert is smal en scherp:

1. **De trigger verhuist naar het gedeelde pad.** De luisteraar die `window.isEditSubmitting` aanzet bij het inswappen van het voortgangsfragment staat inline in `project-details.html.j2` (rond regel 800). Hij hoort in `static/js/edit_modal.js`, naast de blokkade die er al staat. Dan geldt de regel voor elke pagina die de modal opent, en niet alleen voor die ene.

2. **De inline luisteraar daar weghalen**, zodat er niet twee zijn die elkaar overschrijven.

3. **Nakijken of alle openers gedekt zijn.** Vijf templates openen de gedeelde modal (`project-details`, `admin/approvals` en zijn `_modal`, `section-deployment-actions`, `section-actions`, `section-components`) en drie gebruiken het voortgangsfragment. Na stap 1 hoort dat automatisch te kloppen; controleer dat en niet aannemen.

4. **Een test die het vasthoudt.** Een browsertest die een actie start vanaf een andere pagina dan de projectdetailpagina en aantoont dat Escape en klikken-buiten daar ook genegeerd worden. Zonder die test verschuift dit terug zodra iemand een pagina toevoegt.

**Wat er NIET meer hoeft:** `showDangerConfirmation`, de titelmap en het eigen voortgangsblok zijn weg (gemeten: nul vermeldingen). Verwijderen en herverwerken lopen al langs de generieke bevestiging. Punt 1 tot en met 4 van het voorstel hierboven zijn daarmee gedekt; lees ze als achtergrond, niet als opdracht.

## Volgorde

1. Eerst het gedrag vastleggen dat er nu is: welke zes acties er zijn, wat ze aanroepen, en wat er gebeurt bij succes en bij fout. Zonder dat vangnet is dit een verbouwing aan het verwijderpad op gevoel.
2. Herverwerken (`refresh`, `deployment-refresh`) omzetten. Dat is de veilige helft: het is al een taak, dus alleen de dialoog verandert.
3. Verwijderen omzetten, per soort, met de E2E-test uit stap 1 eronder.
4. `showDangerConfirmation`, de titelmap en het eigen voortgangsblok verwijderen. Verifiëren: het woord komt niet meer voor in de sjablonen.

## Waar op te letten

**Dit raakt het verwijderpad van een project.** Dat is het zwaarste dat de UI kan doen en het is onomkeerbaar. Neem de E2E-test uit stap 1 serieus: de haken staan in `tests/e2e/helpers/` en er is een `WizardHelper`. Een bevestiging die per ongeluk de verkeerde actie post is hier erger dan een lelijke dialoog.

**Het endpoint mag nooit uit het verzoek komen.** Bij de service-acties is dat opgelost door de actie server-side opnieuw af te leiden en op een sleutel te matchen; een parameter waar een URL in past zou een open POST-doel zijn. Voor verwijderen geldt dat dubbel.

**ROOS zet attribuutwaarden opnieuw uit in dubbele quotes.** JSON in `hx-headers` of `hx-vals` op een `<c-button>` breekt daarop, en een `id` wordt naar een binnenelement gekopieerd, wat dubbele id's geeft. Zet zulke attributen op een omhullende `div`; htmx erft ze. Dit heeft ons vijf keer geraakt, de laatste keer vanavond.

**Niet zelf CSS toevoegen.** De pagina heeft componenten; gebruik `c-alert` en de bestaande voortgangsklassen in plaats van een eigen zichtbaarheidsregel.
