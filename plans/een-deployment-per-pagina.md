# Eén deployment per pagina, en de keuze blijft staan bij het wisselen van tabblad

Status: plan, 13 augustus 2026. Aanleiding: de tabbladen Deployments en Metingen renderen op dit moment **alle** deployments en verbergen er alles behalve één met CSS. Dat is niet wat er bedoeld is, het kost werk voor blokken die niemand ziet, en de gekozen deployment gaat verloren zodra je van tabblad wisselt.

## Wat er nu is, gemeten

In `bg/project-tabs.html.j2` staat op beide tabbladen dezelfde vorm:

```jinja
{% for deployment in project.deployments | sort(attribute='name') %}
    <div class="deployment-section{% if deployment.name != deployment_open %} is-hidden{% endif %}"
```

Alle deployments komen dus in de DOM en `switchDeployment()` wisselt ze met de klasse `is-hidden`. De keuze wordt in de browser onthouden (`static/js/deployment_switch.js`, per project) en de server geeft zijn keuze door via `data-deployment-open` op `#deployments-weergave`.

Dat heeft drie gevolgen die allemaal in dezelfde richting wijzen:

* **Werk voor het niets.** Elk verborgen blok draagt zijn eigen lazy-laders. Dat is vandaag afgevangen met `hx-trigger="intersect once"`, precies omdat verborgen blokken nooit in beeld komen, maar dat is een omweg om een probleem dat niet zou moeten bestaan. Het blokkeert ook het verversen van de metingen (RC-91): een `every 60s` peilt wél wat verborgen is.
* **De keuze overleeft geen tabbladwissel.** Deployments en Metingen hebben elk hun eigen weergave van dezelfde keuze, en die weten niets van elkaar.
* **De URL zegt niet waar je bent.** Je kunt een deployment niet delen of terugvinden met de terugknop.

## Wat er moet gebeuren

**Eén deployment per pagina, en die staat in de URL:**

```
/projects/tfc-nfv/deployments/productie
/projects/tfc-nfv/metrics/productie
```

De server rendert dan alleen die ene. De lus over alle deployments verdwijnt, en met hem `is-hidden` op dat niveau.

1. **De routes krijgen de deploymentnaam.** RC-76 heeft de tabbladen al eigen adressen gegeven (`/projects/deployments/<project>`); dit is de deployment erachter. Kies of de bestaande vorm blijft doorverwijzen: een gedeelde link hoort niet dood te gaan.
2. **Zonder deployment in het pad kiest de server er een**, en zegt welke. Dat is de eerste op naam, of de onthouden keuze; kies bewust en houd het bij één regel, want twee bronnen voor "welke staat open" is precies wat dit plan opruimt.
3. **De keuze reist mee tussen de tabbladen.** Wissel je van Deployments naar Metingen, dan blijft dezelfde deployment geselecteerd. Nu de naam in de URL staat is dat een kwestie van de tabbladlinks die naam meegeven, niet van iets onthouden.
4. **De kiezer wijst naar een adres.** Een andere deployment kiezen is dan navigeren, geen JavaScript die blokken toont en verbergt.

## Wat er van `switchDeployment()` overblijft

Vermoedelijk niets, en dat is de opbrengst. Maar controleer het: `deployment_switch.js` onthoudt de keuze per project, en er hangt meer aan die klassen dan de kiezer alleen (`deployment-actions-<naam>`, `argocd-<naam>`, en de blokken die zichzelf inladen). Wat wegvalt moet aantoonbaar niet meer nodig zijn, niet stilzwijgend verdwijnen.

Laat geen dood JavaScript staan dat nergens meer op aanslaat; dat is precies het soort restant dat later iemand op het verkeerde been zet.

## De toets

- `/projects/<project>/deployments/<naam>` toont die ene deployment, en de pagina bevat de andere niet;
- hetzelfde voor `/projects/<project>/metrics/<naam>`;
- van Deployments naar Metingen wisselen houdt dezelfde deployment vast, en andersom;
- een andere deployment kiezen verandert de URL, en de terugknop werkt;
- zonder deployment in het pad opent er een, en de URL zegt daarna welke;
- de oude adressen doen wat er besloten is;
- alles wat het paneel kon aanroepen (bewerken, herverwerken, logs, backups) kan nog steeds;
- er staat geen JavaScript meer dat blokken toont en verbergt, of wat blijft heeft aantoonbaar een taak.

## Waar op te letten

**Dit maakt het verversen van de metingen pas veilig.** RC-91 wil `hx-trigger="intersect once, every 60s"`; dat kan zodra er nog maar één blok op de pagina staat. Stem af zodat die twee elkaar niet in de weg zitten: is RC-91 al klaar, dan is dit de opruiming die zijn voorbehoud weghaalt.

**De klassen zijn geen vormgeving.** `deployment-section`, `deployment-actions-<naam>`, `argocd-<naam>`: daar hangen de blokken aan die zichzelf inladen. Vervalt de wikkel, dan verandert dat mee, en dat is de plek waar dit stil kan breken.

**Niet en passant de panelen verbouwen.** Dit gaat over hoeveel er gerenderd worden en wat er in de URL staat, niet over wat er in een paneel staat.
