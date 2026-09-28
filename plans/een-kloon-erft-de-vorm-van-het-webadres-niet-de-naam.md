# Een kloon erft de vorm van het webadres, niet de naam

Status: plan, 22 september 2026. Niet gebouwd. Hoort bij issue #167.

Aanleiding: een deployment die met `clone-from` is aangemaakt krijgt de componenten van de bron mee, maar niet diens `publish-on-web`-config op de deploymentlaag. De hostnaamgeneratie valt dan terug op de platformdefault, en die default is tussen 21 juni en 15 juli 2026 veranderd van een hostnaam per deployment naar een hostnaam per component.

In project `asses-k2n` werden daardoor alle PR-previews stil onbruikbaar. De SPA roept zijn API same-origin aan (`/api/v1/...`); op een frontend-eigen hostnaam bestaat die route niet, valt het verzoek in de nginx-catch-all voor client-side routing en komt `index.html` terug met **HTTP 200**. Geen 404, geen 502, dus het viel twee maanden niet op.

## Waar het misgaat

Bij het aanmaken van een gekloonde deployment (`project_manager.py:7564`) gebeurt dit, in deze volgorde:

1. de bron wordt in zijn geheel gekopieerd, inclusief zijn `services`-blok;
2. `clear_domain_settings(new_deployment)` wist het hele webadres;
3. de eigen aanvraag van de beller wordt teruggeschreven, zodat die van de bron wint;
4. was het subdomein van de bron gelijk aan zijn deploymentnaam, dan krijgt de kloon zijn eigen naam als subdomein.

Stap 2 is de oorzaak. `clear_domain_settings` (`services/catalog/publish_on_web/domain_config.py:226`) loopt over **alle** zes leden van `DomainSetting` en popt ze:

| veld | wat het bepaalt | mag het mee? |
|---|---|---|
| `base-domain` | de naam | nee, botst met de bron |
| `subdomain` | de naam | nee, botst met de bron |
| `domain-format` | de vorm | **ja, dit is het gat** |
| `root-component` | de vorm | waarschijnlijk |
| `expose-component-on-bare-domain` | de vorm | waarschijnlijk |
| `issuer` | het certificaat | onduidelijk, zie hieronder |

De docstring motiveert het wissen met "a clone uses its own (target) domain setup and never the source's hostnames". Dat argument dekt de eerste twee rijen. `domain-format` is geen hostnaam maar de vórm waarin een hostnaam wordt opgebouwd, en die botst met niemand.

Wat het stil maakt: een ontbrekende `domain-format` is geen fout. `get_domain_setting(dep, DomainSetting.DOMAIN_FORMAT)` geeft `None` (`project_manager.py:5626`), en de lezers vullen dat aan met de default van vandaag. De kloon vraagt dus niet om een vorm, hij erft de vorm van het moment waarop hij wordt verwerkt.

## De keuze die dit plan open laat

Het issue noemt twee richtingen, en ze sluiten elkaar niet uit.

**Richting 1, het directe geval.** Laat de kloon de vormvelden overnemen en alleen de naamvelden wissen. Klein, lokaal, en lost `asses-k2n` op.

**Richting 2, de hele klasse.** Leg bij het aanmaken van een deployment de **effectieve** `domain-format` vast, ook als de aanvrager niets koos. Dan verandert een nieuwe platformdefault nooit meer met terugwerkende kracht een bestaande omgeving. Dit raakt elke deployment, niet alleen klonen.

Richting 2 is de echte bescherming: we pinnen `zad-actions` op een SHA en dachten daarmee deterministisch te zijn, maar een leeg veld betekent "de default van vandaag". Richting 1 is nodig ongeacht richting 2, want een kloon hoort de vorm van zijn bron te volgen en niet de default.

**Voorstel: bouw richting 1 nu, en beslis richting 2 apart.** Richting 2 is een migratievraag (wat doe je met de bestaande deployments die nu een lege `domain-format` hebben?) en die hoort niet ongemerkt in deze fix.

## Wat er moet gebeuren

1. **Splits de zes instellingen in naam en vorm.** Zet dat onderscheid in `domain_config.py`, naast `DomainSetting`, zodat er één plek is die zegt welk veld een hostnaam noemt en welk veld een vorm beschrijft. Niet als losse lijst bij de aanroeper, want dan loopt hij bij het volgende veld weer uit de pas.
2. **`clear_domain_settings` krijgt een tegenhanger die alleen de naamvelden wist**, en de kloonweg gebruikt die. De bestaande functie blijft zoals hij is voor de aanroepers die het hele adres willen wissen. *Verify:* een kloon van een bron met `domain-format: deployment-project` houdt dat veld, en heeft geen `base-domain` of `subdomain` van de bron.
3. **Beslis wat `issuer` doet.** Die hangt aan het domein: wist je `base-domain` maar houd je de issuer van de bron, dan draagt de kloon een certificaatuitgever die bij een domein hoort dat hij niet meer heeft. Mijn aanname is dat `issuer` bij de naamvelden hoort en dus weg moet, maar dat is een aanname en geen meting. *Verify:* een kloon van een bron met een eigen domein en een eigen issuer levert een geldige certificaataanvraag op.
4. **Een regressietest op het stille geval.** Niet alleen dat het veld meekomt, maar dat de hostnaam van de kloon dezelfde vorm heeft als die van de bron. Dat is de assertie die `asses-k2n` had gevangen. *Verify:* de test wordt rood als je stap 2 terugdraait.
5. **Werk `features/`-documentatie bij** waar het kloongedrag beschreven staat, zodat er staat wat een kloon wel en niet overneemt.

## Assertie

```bash
cd operations-manager/python
uv run pytest tests/ -k "clone and domain" -x -q --tb=short
uv run ruff check . --fix && uv run ruff format . && uv run pyright
```

Klaar als:

- een kloon van een deployment met een expliciete `domain-format` diezelfde vorm heeft, en de test rood wordt zodra je het wissen terugzet;
- die kloon geen `base-domain` en geen `subdomain` van zijn bron draagt;
- een kloon van een bron **zonder** `domain-format` zich niet anders gedraagt dan vandaag, want daar valt niets over te nemen. Dit is de grens van richting 1, en precies de reden dat richting 2 apart op tafel blijft;
- het onderscheid naam/vorm op één plek staat en niet bij de aanroeper.

## Wat dit NIET oplost

Een bestaande deployment met een lege `domain-format` blijft de default van vandaag volgen. Verandert die default opnieuw, dan verschuift die deployment opnieuw. Dat is richting 2 en die staat open.
