# Het startcommando van een component instelbaar maken

Status: gebouwd, 6 augustus 2026. Het veld bestaat nu als editable met validatie; wat hieronder stond als open vraag is beslist en de uitkomst staat onderaan.

## Wat er al is

`command` bestaat volledig in het datamodel en de generator:

- In `project_v2.json` op **twee** lagen: `components[].command` en `deployments[].components[].command`, als lijst van strings met minstens één element.
- In `manifests/deployment.yaml.jinja` wordt het gerenderd naar de container.

Wat ontbreekt is elke weg naar binnen: geen editable, geen formulierveld, geen API-veld. Wie het wil zetten moet het projectbestand met de hand bewerken.

## Waarom dit niet zomaar een veld erbij is

Een verkeerd `command` geeft een container die niet start, en de foutmelding wijst niet naar de oorzaak. Dat is niet hypothetisch: bij het opzetten van de testimages liep het stuk op

```
exec: "sh": executable file not found in $PATH
```

Dat is een correct commando voor de meeste images en een fataal commando voor een image zonder shell. De gebruiker ziet een pod die niet start; niets zegt "je commando bestaat niet in dit image".

Daar komt bij dat `command` in Kubernetes het `ENTRYPOINT` van het image **vervangt**. Een image dat zijn eigen opstartlogica in de entrypoint heeft, verliest die stilzwijgend. Dat is precies het soort fout dat pas maanden later opvalt.

## Wat er beslist moet worden

**Willen we dit aanbieden, en aan wie?** Het is een scherp mes. Voor een gevorderde gebruiker met een eigen image is het nuttig; voor iemand die een kant-en-klaar image draait is het vooral een manier om het te slopen.

**Wat gebeurt er bij leeg?** De vraag die de aanleiding was. Een leeg veld hoort het image zijn eigen entrypoint te laten houden, dus: niet schrijven in plaats van een lege lijst schrijven. Het schema eist `minItems: 1`, dus een lege lijst is sowieso ongeldig; `remove_when_none` op de editable is dan de juiste vorm. Dat mechanisme bestaat al en wordt door de invite-velden gebruikt.

**Op welke laag?** Het schema staat het op allebei toe. Component-niveau is "dit component start zo", deployment-component is "in deze omgeving anders". Dat tweede is zeldzaam en waarschijnlijk niet waard om in de UI te tonen, maar de API zou het moeten kunnen (zie `plans/diensten-ontsluiten-definieren-gebruiken-binden.md`, waar dezelfde vraag speelt).

**Kunnen we de gebruiker beschermen?** Een paar goedkope dingen zijn mogelijk: waarschuwen dat het de entrypoint vervangt, en na een uitrol expliciet melden dat de pod niet startte mét de exec-fout erbij in plaats van alleen "CrashLoopBackOff". Dat laatste past bij de stappen die taken sinds RC-30 tonen.

## Waarom nu opgeschreven

De mogelijkheid staat in het schema en in de generator, dus iemand die het projectbestand leest denkt terecht dat het kan. Zonder dit document is de enige manier om te ontdekken dat het niet via de UI kan, het zelf proberen.

## Wat er uiteindelijk gebouwd is

Het veld bestaat als `COMPONENT_COMMAND_EDITABLE` met een kind per argument, zichtbaar als "Startcommando" met een waarschuwing dat het de entrypoint van het image vervangt. Leeg laten schrijft niets (`remove_when_none`), dus het image houdt zijn eigen commando.

**Validatie op twee plekken, want er zijn twee wegen naar binnen.** De editable weigert een leeg of blanco argument en stuurtekens; het schema doet nu hetzelfde met `minLength: 1` en `maxItems: 10` op allebei de lagen. Dat tweede is nodig omdat de API en een handmatige bewerking niet langs het formulier komen.

**Een samengesteld commando kan, maar in één lijst.** Kubernetes kent `command` (entrypoint) en `args` (cmd); ZAD kent alleen `command`, en `manifests/deployment.yaml.jinja` rendert ook alleen dat. De vorm `command: ["/bin/sh", "-c"]` met een apart `args:` is dus niet uit te drukken. Het equivalent is `["/bin/sh", "-c", "<script>"]` in één lijst, en dat draait identiek: Kubernetes plakt `args` achter `command`, dus het verschil is puur waar je de knip legt. `args` toevoegen zou een schemaveld, een templateregel en een editable kosten voor nul extra mogelijkheden.

**Een commando kan het projectbestand niet openbreken.** De zorg bij een vrij tekstveld is een waarde die de YAML eromheen herschrijft. De canonieke schrijver zet een meerregelige waarde neer als literal block, dus wat teruggelezen wordt is één string en geen extra sleutels. Dat is een eigenschap van de schrijver, niet van dit veld, en staat daarom als aparte test in `tests/test_component_command_field.py`.

**Wat niet gebouwd is:** de laag `deployments[].components[].command` heeft geen formulierveld. Het schema staat het toe en de generator rendert het, maar "in deze omgeving een ander startcommando" is zeldzaam genoeg om er geen UI voor te maken. Via de API hoort het wel te kunnen, en dat hangt aan `plans/diensten-ontsluiten-definieren-gebruiken-binden.md`.

**Blijft staan als zwakke plek:** een verkeerd commando geeft nog steeds `CrashLoopBackOff` zonder dat iets de exec-fout doorvertelt. De waarschuwing bij het veld is preventie, geen diagnose.
