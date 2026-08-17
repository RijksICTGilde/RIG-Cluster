# Eén voortgangsweergave, server-gerenderd

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: datums werden in JavaScript opgemaakt, en bij het uitzoeken bleek dat het symptoom van twee implementaties van dezelfde weergave.

## Twee wegen naar hetzelfde scherm

`templates/partials/task_progress_fragment.html.j2` doet het zoals de rest van de applicatie: de server rendert, htmx haalt op.

```
hx-get="{{ progress_url }}"
hx-trigger="every 2s"
```

Subtaken komen server-gerenderd mee (acht plekken in dat sjabloon), en datums lopen via het `dutch_date`-filter. RC-30 heeft dat gisteren nog uitgebreid met de stappen die een taak zet.

`templates/project-progress.html.j2` doet het anders. 579 regels, waarvan 42 JavaScript, met een eigen poller op een JSON-endpoint (`/ui/tasks/{id}/status`) die zelf HTML in elkaar zet. Daar zitten ook de eigen datums:

```js
${task.created_at ? `<div class="task-time">Gestart: ${new Date(task.created_at).toLocaleTimeString('nl-NL')}</div>` : ''}
```

Dat werkt toevallig goed voor een Nederlandse browser, maar het gaat langs `dutch_date` heen en wijkt af zodra iemand een andere tijdzone-instelling heeft dan de server.

Ze zijn geen exacte dubbelen: het fragment zit in modals, de pagina is de volledige weergave na het aanmaken van een project. Maar het werk is hetzelfde, en de nieuwe eigenschappen (stappen, subtaken, correcte tijden) landen alleen in het fragment.

## Voorstel

De volledige pagina gebruikt hetzelfde fragment. De pagina blijft bestaan als omhulsel, maar de voortgang binnenin komt van de server, net als in de modal.

Wat dat oplevert, en die volgorde is ook de waarde-volgorde:

1. **Eén plek waar voortgang wordt weergegeven.** Wat RC-30 aan stappen toevoegde verschijnt dan ook op deze pagina, zonder dat iemand dat apart moet nabouwen.
2. **Geen datums meer in JavaScript.** Die verdwijnen met de handgemaakte HTML.
3. **42 regels JavaScript minder**, plus het JSON-endpoint als daar niemand anders op zit.

## Volgorde

1. Vastleggen wat de pagina vandaag toont dat het fragment niet toont. Dat is de echte vraag van dit plan: als de pagina iets kan wat het fragment niet kan, dan is dat een gat in het fragment en geen reden om de pagina te houden.
2. De pagina op het fragment zetten, met de bestaande weergave ernaast om te vergelijken.
3. De JavaScript en het JSON-endpoint weghalen. Verifiëren: `new Date(` komt in `project-progress.html.j2` niet meer voor.
4. Nakijken of `/ui/tasks/{id}/status` nog een andere gebruiker heeft voordat het weggaat.

## Waar op te letten

**Niet alle JavaScript-datums zijn hetzelfde.** Van de negen plekken zijn er maar drie een getoonde datum die de server ook kan renderen. De rest is iets anders en hoort te blijven: het dashboard rekent een relatieve tijd uit ("2 minuten geleden") die meeloopt terwijl de pagina open staat, `project-details` maakt een bestandsnaam voor een logdownload, en twee plekken zetten unix-seconden om in aslabels van een grafiek. Haal die niet weg in een opruimactie; ze doen werk dat op de server niet kan.

**De pagina is wat een gebruiker ziet na het aanmaken van een project.** Dat is een eerste indruk en een moment waarop iemand niet weet of het goed gaat. Vervang hem dus niet door iets dat minder laat zien, en bewaar de vergelijking uit stap 1 tot dat vaststaat.

**Het fragment pollt elke twee seconden en vervangt zichzelf.** Op een volledige pagina met veel deployments en subtaken is dat meer werk dan in een modal. Kijk of dat houdbaar is voordat je de oude weg weggooit.
