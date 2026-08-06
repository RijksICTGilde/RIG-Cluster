# Cross-domain toegang invulbaar maken

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: de projectstap voor cross-domain-access is in de create-wizard niet in te vullen en niet op te slaan, en de trapsgewijze keuze die het idee was bestaat maar voor een kwart.

Niet in dit plan: de knop "Item toevoegen" die het hele blok liet verdwijnen. Dat was een aparte bug in de gedeelde zichtbaarheidscheck en is gerepareerd in `13f74663`.

## Wat er al staat, en wat dus niet opnieuw gebouwd moet worden

De verleiding bij deze dienst is te denken dat er niets is omdat het scherm niet werkt. Het tegendeel is waar; gemeten:

```
opi/services/catalog/cross_domain_access/   1035 regels over 6 modules
manifests/service-network-policy.yaml.jinja   62 regels
tests/test_cross_domain_access.py            488 regels, 37 tests
features/cross-domain-access.md              bestaat
```

De NetworkPolicies worden echt gegenereerd, per deployment, via `contribute_deployment_manifests`. Het schema bestaat en valideert. En de patch-semantiek die dit plan in de UI wil ontsluiten is niet alleen bedacht maar getest, met tests die letterlijk heten:

```
test_stored_root_rule_may_leave_peer_deployment_open
test_patch_only_peer_deployment_inherits_the_rest
test_patch_that_overrides_nothing_keeps_root
```

**Dit plan bouwt dus geen dienst, het bouwt de invoer ervoor.** Wie eraan begint moet `merge.py`, `resolve.py`, het schema en de manifestgeneratie met rust laten: die kant werkt en is gedekt. Het gat zit uitsluitend in het formulier.

## Wat er nu is, gemeten

Het **datamodel is niet half af**, en dat is de kern van dit plan: de UI moet ernaartoe groeien, niet andersom. Het dienstschema zegt het zelf:

> *"Name of this rule; a deployment-level entry with the same name patches the project rule."*
> *"Deployment of the peer; may be left open on a project-level rule and filled in per deployment."*

En `config_layers` staat op `['project', 'deployment']`. Dat is precies het beoogde gebruik: je beschrijft een regel één keer op projectniveau, en per deployment stel je zo nodig bij met welke deployment van de andere partij die praat. Meestal praat jouw `prod` met hun `prod`, maar soms praat elke deployment van jou met dezelfde deployment van hen, en juist daarom staat de regel op projectniveau.

Wat er in de UI tegenover staat:

| | inkomend | uitgaand |
|---|---|---|
| naam | tekst | tekst |
| **peer project** | **select** (leeg in de create-wizard) | **select** (idem) |
| **peer deployment** | **vrije tekst** | **vrije tekst** |
| **peer component** | **vrije tekst** | **vrije tekst** |
| eigen component | select | select |
| poort | select van **eigen** poorten | idem |

En verder:

- `config_form_section(ConfigLayer.DEPLOYMENT)` geeft **niets** terug. De laag bestaat in de data en niet in het formulier, dus de override die het schema beschrijft is nergens in te vullen.
- Drie velden zijn `required=True` terwijl hun keuzelijst in de create-wizard leeg is. `CrossDomainProjectOptionsProvider` leest `_cross_domain_projects`, dat door `modal_wizard_init` wordt gezet, en `CrossDomainPortOptionsProvider` leest het voorberekende `_cross_domain_ports`. Beide zijn leeg buiten de edit-flow. Gevolg: je krijgt een lijst met alleen een uitleg-regel, kunt niets kiezen, en de stap is niet op te slaan. De foutmelding wijst dan naar een veld dat je onmogelijk goed kunt invullen.
- De eigen-componentenlijst is het gunstige geval: de componentenstap komt vóór deze stap, dus die lijst kan daar wél gevuld zijn.

## Een aanname die niet klopt en die eerst weg moet

In `providers.py` staat als reden voor de voorberekende poortenunie:

> *"The framework cannot filter options per row"*

Dat is niet waar, en zolang het blijft staan stuurt het iedereen de verkeerde kant op. De renderer bouwt al een `item_context` **per rij** en verrijkt die voor een ander geval (`exclude_references` voor de componentreferentie-provider). Een provider krijgt precies de kwargs die hij in zijn `__init__` declareert, via `_filter_provider_kwargs`. De trapsgewijze keuze is dus gewoon te bouwen met editables en value providers; er is geen frameworkwijziging voor nodig.

## Voorstel

1. **Die docstring corrigeren**, met de verwijzing naar het bestaande per-rij-mechanisme erbij. Dit is stap één omdat het de rest van het plan pas geloofwaardig maakt.

2. **De rij-context uitbreiden met de waarden van de rij zelf**, in dezelfde `item_context` waar `exclude_references` al ontstaat. Daarmee kan een provider antwoorden op "welke deployments heeft het project dat in déze rij gekozen is".

3. **De cascade afmaken**: peer deployment en peer component worden keuzelijsten, gevoed door het gekozen peer project respectievelijk de gekozen peer deployment. Vrije tekst blijft mogelijk als terugval voor een peer die de lezer niet kan uitlezen, maar dan zichtbaar als terugval en niet als normaal geval.

4. **De poortenlijst omdraaien naar de ontvangende kant.** Nu toont hij jouw eigen poorten; de poort hoort bij de pod die bereikt wordt. Voor een inkomende regel is dat inderdaad de jouwe, voor een uitgaande regel niet. Dat verschil moet het veld maken, niet de gebruiker.

5. **De create-wizard dezelfde context geven als de edit-flow.** `_cross_domain_projects` en `_cross_domain_ports` worden nu alleen door `modal_wizard_init` gezet. Zet ze op één plek die beide flows gebruiken, zodat "werkt in de ene flow, leeg in de andere" niet nog een keer ontstaat.

6. **Een formulier voor de deployment-laag**, met alleen wat daar betekenis heeft: de regelnaam als verwijzing naar de projectregel, en het peer-deployment-veld als override. Niet de hele regel opnieuw, want dan is het geen patch meer maar een tweede waarheid.

7. **`required` per laag laten kloppen.** Een veld dat op projectniveau bewust open mag blijven (peer deployment) mag daar niet verplicht zijn.

## Volgorde

1. De docstring, en de rij-context met een test die aantoont dat een provider de waarden van zijn eigen rij ziet.
2. Peer deployment als keuzelijst. Verifiëren in de browser: een ander project kiezen verandert de deploymentlijst.
3. Peer component, op dezelfde manier afhankelijk van de gekozen deployment.
4. De poortenlijst naar de ontvangende kant, met een test per richting.
5. De gedeelde context, zodat de create-wizard invulbaar en opslaanbaar wordt. Verifiëren: de stap in de create-wizard doorlopen en opslaan zonder validatiefouten.
6. De deployment-laag als laatste, want die leunt op alles ervoor.

## Waar op te letten

**Er is een open vraag over wie je mag noemen.** `CrossDomainProjectOptionsProvider` beperkt de peer projecten tot projecten waarvoor de ingelogde gebruiker geautoriseerd is. Dat is veilig, maar het betekent dat cross-domain toegang alleen te leggen is naar je eigen projecten, en de dienst heet niet voor niets cross-domain. Of dat de bedoeling is, is een beslissing die vóór stap 5 genomen moet worden, want die stap zet dezelfde lijst op een tweede plek en verdubbelt dus ook de keuze.

**De ontvanger beslist, en dat moet zo blijven.** Een inkomende regel is een toestemming die het ontvangende project geeft. Een cascade die de andere kant makkelijk maakt mag dat niet stilzwijgend omdraaien: dat je een deployment van een ander project kunt kiezen betekent niet dat je daar binnen mag.

**Een keuzelijst die de andere kant uitleest, leest een ander projectbestand.** Dat is een nieuwe afhankelijkheid tijdens het renderen van een formulier. Houd het lui en gecached, en zorg dat een onleesbaar of verwijderd peer project een nette terugval geeft in plaats van een kapotte stap.

**Een opgeslagen waarde mag nooit uit de lijst vallen.** De bestaande providers doen dit al goed: een waarde die niet meer bestaat blijft selecteerbaar met een label erbij. Neem dat gedrag mee in elke nieuwe lijst, anders gooit een save stilzwijgend een regel weg die iemand bewust had gezet.

**Deze stap heeft geen bruikbare browsertest gehad.** De bug die hieraan voorafging was in de create-wizard triviaal zichtbaar en is toch dagen blijven staan. Wat hier gebouwd wordt, hoort een e2e-test te krijgen die de stap echt doorloopt en opslaat, niet alleen een render-test.
