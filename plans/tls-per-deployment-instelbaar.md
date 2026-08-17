# Een eigen certificaat per deployment instelbaar

Status: plan, 12 augustus 2026. Aanleiding: productie en staging zitten in één project met verschillende domeinen. Productie moet een eigen certificaat kunnen serveren terwijl staging op Let's Encrypt blijft, of andersom. Dat kan al in de gegevens, maar niet in de wizard.

## Wat er nu is, gemeten

Publish-on-web verdeelt zijn configuratie over drie vragen: het **project** zegt op welke domeinen gepubliceerd mag worden, de **deployment** hoe het adres wordt samengesteld (`subdomain`, `domain_mode`, `domain_format`), en het **component** of het de dienst gebruikt en hoe TLS wordt afgehandeld (`tls`, `attachment`).

En er is een vierde laag die precies voor deze vraag bestaat. Uit de code: *"Component and deployment-component: the same tls/attachment pair, since the deployment-component entry exists precisely to override the component's."*

| laag | model | editables |
|---|---|---|
| project | `PublishOnWebProjectConfig` | 0 |
| component | `PublishOnWebComponentConfig` | 2 |
| deployment | `PublishOnWebDeploymentConfig` | 8 |
| **deployment-component** | `PublishOnWebComponentConfig` | **0** |

Daar zit het gat, en het is kleiner dan het lijkt: **de velden bestaan al**. Op de componentlaag zijn er twee editables voor `tls` en `attachment`, en die zie je bij het aanmaken van een project. Ze zijn alleen niet voor de deployment-component-laag gedeclareerd, terwijl het model daar hetzelfde is (`PublishOnWebComponentConfig`, tweemaal in de tabel hierboven).

Dit is dus geen ontbrekend vermogen maar een ontbrekende declaratie voor één laag. Kijk eerst of `config_editables(ConfigLayer.DEPLOYMENT_COMPONENT)` de bestaande twee kan hergebruiken met een verlegd pad, zoals `replace_segment_visualizer` elders doet, in plaats van nieuwe te schrijven. Twee kopieën van hetzelfde veld lopen uit de pas zodra er iets aan verandert.

**Gevolg:** de override is te zetten via de API en door het projectbestand te bewerken, maar niet in `modal-edit-deployment-<n>`, en daar wordt hij verwacht.

## Wat er moet gebeuren

1. **Via de DIENST, niet in het formulier.** De haak bestaat al: `deployment_component_service_visualizers()` in de registry verzamelt wat elke dienst voor die laag declareert, en het deploymentformulier neemt dat over. Publish-on-web stopt er alleen niets in.

   Declareer de editables voor `tls` en `attachment` dus in het pakket van de dienst zelf (`opi/services/catalog/publish_on_web/`), zoals hij dat voor de andere lagen ook doet via `config_editables(layer)`. Niets in `wizard_sections.py` of in een sjabloon: dan is de volgende dienst weer handwerk, en de hele opzet van RC-36 was dat alles van een dienst in zijn eigen map staat.

   Toets dat ook zo: na afloop hoort `deployment_component_service_visualizers()` de nieuwe velden te bevatten zonder dat het formulier iets over publish-on-web weet. Nu levert die haak er precies één, van `user-env-vars`.

2. **Toon dat het een override is.** Een leeg veld betekent hier "volg het component", niet "geen TLS". Zonder dat onderscheid weet je niet of je naar een instelling kijkt of naar een erfenis, en dan zet iemand hem per ongeluk uit. Laat zien wat het component zegt, en laat leeg betekenen dat dat blijft gelden.

## De vraag die eerst beantwoord moet worden

**Kan een deployment-component het `provided` van het component ook UITZETTEN?** Dat is de andere helft van de vraag en het is niet uit de code af te lezen: overschrijft `tls: standard` daar een `tls: provided` van het component, of vult de override alleen aan waar het component niets zegt?

Meet dat vóór je een formulier bouwt. Kan het niet, dan is het formulierveld misleidend en is de echte opdracht dat eerst mogelijk te maken.

Let daarbij op de bestaande regel dat `tls: provided` zonder `attachment` door het model verworpen wordt. Bij een override moet duidelijk zijn waar dat certificaat vandaan komt: van deze laag, of van het component eronder.

## De toets

- op `modal-edit-deployment-<n>` staat per component een TLS-keuze en een certificaatveld;
- leeg laten verandert niets: de deployment volgt het component, en dat is te zien;
- een eigen certificaat op productie en Let's Encrypt op staging levert twee verschillende ingressen op, met het juiste certificaat per deployment;
- een override die `provided` uitzet doet dat werkelijk, of het veld biedt die keuze niet aan;
- het projectbestand valideert na afloop, en er staat nergens een verwijzing naar een bijlage die niet bestaat.

## Waar op te letten

**Het adres is al per deployment geregeld.** `subdomain`, `domain_mode` en `domain_format` staan op de deployment en werken. Dit plan gaat alleen over het certificaat; verplaats niet en passant iets anders.

**De bijlage is projectbreed.** Een certificaat is een attachment in de projectcatalogus, en meerdere deployments kunnen naar dezelfde verwijzen. Verwijderen is daarom al beveiligd met een bevestiging; zorg dat een override in die telling meetelt, anders denkt de verwijdercontrole dat een certificaat ongebruikt is.

**Niet en passant de andere lagen aanpassen.** Dat het project nul editables heeft is een eigen vraag (de toegestane domeinen worden elders beheerd) en hoort niet in deze taak.
