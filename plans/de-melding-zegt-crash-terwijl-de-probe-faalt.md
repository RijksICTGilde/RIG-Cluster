# De melding zegt "crasht" terwijl de health-controle faalt

Status: plan, 13 augustus 2026. Waargenomen op `/projects/ma-axk/deployments/productie` (sandbox): de deployment meldt dat de pod steeds crasht, maar dat is niet wat er gebeurt. De pod draait; de health-controle faalt, vermoedelijk op een verkeerde poort. De melding wijst dus de verkeerde kant op.

Dat verschil is voor een gebruiker het hele verschil: bij "crasht" ga je je applicatie debuggen, bij "de probe komt er niet doorheen" pas je je health-instelling aan. Wie de eerste melding gelooft, zoekt in de verkeerde code.

## Wat er onderzocht moet worden

1. **Bevestig eerst wat er werkelijk aan de hand is** op `ma-axk`/`productie`. Kijk met `kubectl` (lezen mag) naar de pod: draait het proces, hoeveel herstarts staan er, en wat zegt de laatste `state`/`lastState`? Een pod die op een falende liveness-probe wordt gekild ziet er in de teller uit als een herstartende pod, maar de reden verschilt: `Error`/`OOMKilled` is iets anders dan een kill na een mislukte probe. Leg vast wat je ziet.
2. **Vergelijk de probe-poort met wat het component aanbiedt.** De health-instelling van een component staat in het projectbestand (`health_check`-dienst); de poort waarop de applicatie luistert staat bij de poorten van dat component. Loopt dat uiteen, dan is dat de oorzaak, en dan is de vervolgvraag of wij dat hadden kunnen weten toen het werd opgeslagen.
3. **Zoek waar de melding vandaan komt.** De rode melding op het deploymentpaneel komt uit de ArgoCD-kaart (`bg/_argocd-deployment-card.html.j2`) en/of uit `terminal_condition_message`. Zoek uit op welk signaal die "crasht" zegt en of dat signaal het onderscheid dat we willen maken überhaupt draagt.

## Wat er daarna moet gebeuren

**De melding moet zeggen wat er misgaat.** Een probe die faalt is een eigen toestand en geen crash. Als het onderliggende signaal dat onderscheid draagt (de `reason` van de laatste beëindiging, de events op de pod, de conditie op de deployment), gebruik dat. Draagt het dat niet, dan is dát de bevinding, en dan is de vraag welk signaal er wél bij gehaald moet worden.

Denk ook aan de kant die het kan voorkomen: **hadden we bij het opslaan al kunnen zien dat de probe-poort niet bestaat?** Een health-controle op een poort die het component niet aanbiedt, is een configuratiefout die je bij het invullen kunt melden in plaats van een half uur later als een crashende pod. Dat is een tweede, zwaardere verbetering; noem hem in elk geval, ook als hij niet in deze taak past.

## De toets

- er staat opgeschreven wat er op `ma-axk`/`productie` werkelijk aan de hand is, gemeten en niet afgeleid;
- een pod die door een falende probe wordt gekild levert een andere melding op dan een pod die crasht, en die melding noemt de probe;
- een pod die écht crasht meldt dat nog steeds als zodanig, dus het onderscheid werkt beide kanten op;
- als het signaal het onderscheid niet draagt, staat dat opgeschreven met wat er nodig zou zijn;
- de vraag of dit bij het opslaan te vangen is, is beantwoord.

## Waar op te letten

**Meet op het cluster, niet in de code.** Wat de melding zegt en wat er gebeurt lopen hier per definitie uiteen; alleen de pod zelf weet het.

**Productie is read-only.** Dit speelt op de sandbox, dus daar kan alles; op productie mag alleen lezen.

**Verzin geen derde melding voor een geval dat niet bestaat.** Als blijkt dat de pod tóch echt crasht en de probe daar het gevolg van is, dan is de melding gewoon goed en is dat het antwoord.
