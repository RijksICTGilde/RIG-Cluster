# De deploymentpagina opnieuw

Status: plan, 12 augustus 2026. Vervangt RC-75, die het juiste bouwde op de verkeerde plek. Dat lag aan mijn opdracht en niet aan de uitvoering: er werd naar `/projects/details/<naam>` gewezen (het tabblad **Overzicht**, met de Deployment Status-kaarten) en ik stuurde de taak naar het tabblad Deployments.

De branch van RC-75 mag hergebruikt worden: het server-side zoeken en sorteren (`DEPLOYMENT_SORTERINGEN`, `filter_lotc_deployments`) volgt netjes de vorm van de projectenlijst en is bruikbaar. De weergave zelf moet opnieuw.

## Vier dingen

### 1. De tabel hoort op Overzicht, in plaats van de statuskaarten

Op `/projects/details/<naam>` staat nu "Deployment Status": een kaart per deployment. Bij veel deployments is dat onleesbaar. Daar hoort de tabel, en die **vervangt** die kaarten; ze staan er niet naast.

**En in die tabel hoort de ArgoCD-status wél te staan.** Dat is precies waarvoor je naar dat blok kijkt. RC-75 liet hem weg omdat elke rij anders zijn eigen bevraging doet, en die redenering was goed maar de uitkomst is verkeerd: een statusoverzicht zonder status is geen overzicht.

Dus is de opdracht hier het omgekeerde: **haal die statussen op een manier die wél schaalt.** Eén gebundelde bevraging voor alle deployments van een project, in plaats van N losse. Kan dat niet in één keer bij ArgoCD, dan is een korte cache in het geheugen de weg; leg de vervaltijd vast met de reden, want een verouderde "Healthy" is erger dan geen status.

### 2. De tabs krijgen een echte URL

`?tab=deployments` wordt `/projects/deployments/<projectnaam>`, en zo voor de andere tabbladen. Nu leest een querystring als een filter terwijl het een pagina is.

Let op wat eraan hangt: `active_tab` komt uit `request.query_params`, de tabs zijn links met `?tab=`, en tests en schermafbeeldingen gebruiken die vorm. Kies of de oude URL blijft doorverwijzen; een gedeelde link hoort niet dood te gaan.

### 3. Het tabblad Deployments toont er één, met een keuze

Daar blijft de kiezer: één deployment selecteren, zijn gegevens zien en hem kunnen bewerken. Dat is de detailweergave; de tabel op Overzicht is de ingang.

### 4. De deploymentinformatie staat er twee keer

Op dat tabblad staat dezelfde deployment in twee blokken: eerst een statuskaart (naam, cluster, de rode melding, Uitgeschakeld/Synced, laatste sync) en daaronder "Deployment: <naam>" met de acties, componenten en publieke links.

Voeg ze samen, **met het tweede blok als uitgangspunt**: dat draagt de acties en de inhoud. De melding en de statusgegevens uit het eerste blok gaan daarin mee.

## Wat er misging bij RC-75, en wat dat betekent voor de toets

De tabel rendert **als één kolom**: "Naam / Cluster / Status / Componenten" staan onder elkaar in plaats van als koprij, en elke rij daaronder ook. Er is wel een browsertest geschreven, maar niemand heeft naar het beeld gekeken.

**Kijk dus naar het scherm.** Een test die groen is zegt niet dat een tabel een tabel is; dat is de afgelopen dagen zes keer de dader geweest. Maak een schermafbeelding van de tabel met meerdere rijen en beoordeel die.

Waarschijnlijk zit het in het gebruik van `c-table`: kijk of dat component een koprij als kinderen verwacht of als gegevens, en of `c-th` daar hoort te staan waar hij nu staat.

## De toets

- de tabel is een **tabel** in de browser: kolommen naast elkaar, één koprij;
- de ArgoCD-status staat erin, en twintig rijen leveren **niet** twintig bevragingen;
- zoeken en sorteren werken zonder JavaScript, de URL draagt de keuze;
- `/projects/deployments/<naam>` werkt, en de oude `?tab=`-vorm doet wat er besloten is;
- op het tabblad Deployments staat de informatie **één keer**, met de acties en de statusmelding bij elkaar;
- alles wat de kaarten en het paneel konden aanroepen kan nog steeds, en wijst naar dezelfde plek.

## Waar op te letten

**De klassen zijn geen vormgeving.** `deployment-section`, `deployment-actions-<naam>`, `is-hidden`, `argocd-<naam>`: daar hangt `switchDeployment()` aan en de blokken die zichzelf inladen.

**Het backupblok is een ander geval.** Dat laadt lui vanwege Kopia-verbindingen en staat los van de ArgoCD-vraag hierboven. Niet door elkaar halen.

**Doe het niet halverwege.** Een tabel naast de kaarten, of een nieuwe URL naast de oude zonder besluit, geeft twee waarheden die uit de pas lopen.
