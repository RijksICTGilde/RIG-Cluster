# Deployments als tabel, met zoeken en sorteren

Status: plan, 11 augustus 2026. Aanleiding: het tabblad Deployments toont alle deployments onder elkaar als volledige panelen. Bij een handvol is dat te doen, bij een project met veel PR-omgevingen is het onleesbaar. Het voorstel is dezelfde informatie in tabelvorm, met zoeken en sorteren zoals de projectenlijst dat heeft.

## Wat er nu is, gemeten

Elke deployment krijgt een eigen paneel met zes secties: componenten, helm charts, helmfile, publieke links, gebruikersconfiguratie en de acties. Er staat er **één tegelijk** in beeld; `switchDeployment()` wisselt ze met de klasse `is-hidden`, en een keuzelijst bovenaan bepaalt welke.

Dat is dus geen lijst maar een kiezer plus een detailweergave. Wie wil weten *welke* deployments er zijn, moet de keuzelijst uitklappen; wie ze wil vergelijken, kan dat niet.

**De projectenlijst heeft de vorm die hier gevraagd wordt**, en die is server-side: `?q=` filtert, `?sort=` ordent, en `PROJECT_SORTERINGEN` in `opi/web/lotc_switch.py` legt de sorteringen vast als lijst van (sleutel, label, sorteerfunctie). Zoeken en sorteren gebeuren op de server, "dan werkt het ook zonder JavaScript". Neem die vorm over in plaats van iets nieuws te bedenken.

## De vraag die vooraf beantwoord moet worden

**Wat staat er in de tabel, en wat blijft detail?** Een deploymentpaneel draagt zes secties; die passen niet in een rij, en dat hoeft ook niet. De tabel hoort te beantwoorden: welke deployments zijn er, hoe staan ze ervoor, en welke wil ik openen.

Voorstel voor de kolommen, maar dit is precies het deel dat bewust gekozen hoort te worden:

| kolom | waarom |
|---|---|
| naam | waar je op zoekt |
| cluster | onderscheidt anders identieke namen |
| status | gezond, uitgeschakeld, niet gesynchroniseerd; dit is waarvoor je komt kijken |
| componenten | een aantal, als ingang naar het detail |
| laatste sync | zegt of je naar iets actueels kijkt |

En dan één rij die opent naar het bestaande paneel, in plaats van alles tegelijk te tonen.

**Kies bewust hoe het detail opengaat.** Een rij die uitklapt, een aparte pagina, of de bestaande kiezer die meebeweegt met de tabel. Alle drie zijn verdedigbaar; wat niet werkt is een tabel die alleen maar dubbelop staat naast het huidige paneel.

## Waar op te letten

**De klassen zijn geen vormgeving.** `deployment-section`, `deployment-actions-<naam>`, `is-hidden` en de id's: daar hangt `switchDeployment()` aan, plus de blokken die zichzelf inladen (`argocd-<naam>`). Vervang je de kiezer, dan verandert dat mee, en dat is de plek waar dit stil kan breken.

**De blokken die zichzelf ophalen.** De ArgoCD-status en het backupblok laden per deployment via htmx, en het backupblok pas als het in beeld komt (`hx-trigger="intersect once"`, bewust: per deployment een verzoek opende evenzoveel Kopia-verbindingen). Een tabel met twintig rijen die allemaal hun status ophalen is precies wat daar vermeden werd. Bedenk hoe de statuskolom gevuld wordt zonder twintig gelijktijdige verzoeken.

**Zoeken en sorteren op de server.** Zoals bij de projectenlijst, en om dezelfde reden. Een filter in de browser lijkt sneller en breekt zodra iemand de pagina deelt of terugknop gebruikt.

**Doe het niet halverwege.** Een tabel erbij zetten en het panelenblok laten staan geeft twee weergaven van hetzelfde die uit de pas gaan lopen. Als de tabel er komt, is hij de ingang.

## De toets

- een project met veel deployments: de lijst is te overzien zonder scrollen door panelen;
- zoeken op een deel van een naam laat de juiste rijen over, en de telling zegt hoeveel er in totaal zijn (zoals `Totaal: 1 project van 3`);
- sorteren werkt zonder JavaScript, en de URL draagt de keuze zodat hij deelbaar is;
- alles wat het huidige paneel kon aanroepen (bewerken, herverwerken, logs, backups) kan nog steeds, en wijst naar dezelfde plek;
- geen twintig gelijktijdige statusverzoeken bij twintig rijen.
