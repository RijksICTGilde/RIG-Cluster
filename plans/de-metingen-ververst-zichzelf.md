# De metingen: ruimte, een melding als er niets is, en zichzelf verversen

Status: plan, 13 augustus 2026. Drie punten op `/projects/metrics/<naam>`, klein en bij elkaar horend.

## 1. De tags plakken tegen de kaart

Boven het metingenblok staan tags die de kaart eronder raken. Dezelfde geur als de wizardknoppen die vandaag zijn gerepareerd: twee blokken naast elkaar zonder iets dat ruimte geeft. Een stack, geen CSS.

## 2. Geen metingen is een toestand, geen leegte

Is er nog niets gemeten, dan staat er niets. Dat is juist het normale geval vlak na een start, en het is dan niet te onderscheiden van "er is iets stuk". Er hoort een melding te staan die zegt dat er nog geen metingen zijn en waarom dat kan (net gestart, of de dienst levert ze niet).

Let op het verschil met een echte storing: Prometheus die niet antwoordt is iets anders dan Prometheus die antwoordt met niets, en die twee horen niet dezelfde tekst te krijgen.

## 3. Het blok ververst zichzelf elke minuut

Nu staat er `hx-trigger="intersect once"` op `#metrics-content-<naam>` (`bg/project-tabs.html.j2` rond regel 735): één keer ophalen zodra het in beeld komt, en daarna nooit meer. Voor een grafiek die over de tijd gaat is dat te weinig; wie kijkt of het aantrekt, ververst nu met F5.

`hx-trigger="intersect once, every 60s"` is de hele wijziging.

**Meet eerst één ding, want daar hangt het aan.** De reden dat er `intersect once` staat, is dat verborgen blokken nooit in beeld komen en dus niets ophalen. Het sjabloon loopt op dit moment nog over **alle** deployments (`{% for deployment in project.deployments | sort(attribute='name') %}`) en zet `is-hidden` op alles behalve de geopende, dus die blokken staan wel degelijk in de DOM. Een `every 60s` peilt ook wat verborgen is.

Klopt het dat er inmiddels maar één deployment per tabblad getoond wordt, dan is die lus een restant en mag hij weg, en is `every 60s` zonder meer veilig. Klopt dat niet, dan moet het verversen zich tot het zichtbare blok beperken. **Meet het, en ruim in het eerste geval die lus op** in plaats van er een polling naast te zetten die om een probleem heen werkt dat niet meer bestaat.

## De toets

- de tags raken de kaart niet meer;
- geen metingen levert een melding op die zegt wat er aan de hand is, en die verschilt van de melding bij een onbereikbare Prometheus;
- het blok haalt zichzelf elke minuut op, en een project met meerdere deployments levert daarbij niet meer bevragingen op dan er zichtbare blokken zijn;
- is de lus over alle deployments een restant, dan is hij weg en niet omzeild.

## Waar op te letten

**Kijk naar het scherm.** `scripts/kijk_sandbox.py /projects/metrics/<naam>` logt in en zet de pagina op beeld. Alle drie de punten zijn met een groene test niet te zien.

**Niet en passant de grafieken verbouwen.** Dit gaat over de ruimte eromheen, de lege toestand en het moment van ophalen.
