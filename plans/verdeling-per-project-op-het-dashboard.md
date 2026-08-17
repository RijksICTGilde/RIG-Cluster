# Per project CPU en geheugen op het dashboard

Op `/dashboard`, onder de kaart Resource Usage, hoort per project te staan wat dat project gebruikt: **geheugen en CPU**, gesorteerd op **geheugen**. De vorm is die van de kaart **Resourcegebruik (heel project)** op een projectpagina (`/projects/<naam>/details`), niet een nieuwe.

Er staat nu een kaart "Verdeling over projecten" met alleen CPU. Die is het vertrekpunt.

## Waarom geheugen, en waarom sorteren daarop

Geheugen is waar een pod op omvalt als het opraakt; CPU wordt geknepen. Op een rustig cluster is het CPU-cijfer bovendien vrijwel nul, waardoor de kaart in de praktijk leeg is terwijl er wel degelijk geheugen in gebruik is. Sorteren op geheugen zet daarom bovenaan wat er het meest toe doet.

## De vorm die overgenomen moet worden

`opi/templates_lotc/bg/_resource-usage.html.j2`. Per regel:

- een label met icoon links (`cylinder-split`/donkerblauw voor CPU, `database`/groen voor geheugen),
- rechts de gebruikte waarde vet, dan de limiet MET eenheid en het percentage,
- daaronder pas de balk (`<c-progress-bar value-display="tooltip"`, `color="info"` voor CPU en `color="success"` voor geheugen).

Neem ook de twee schrijfwijze-macro's over die bovenin dat bestand staan: `cores()` (onder een core in millicores, daarboven in cores met een decimaal) en `gib()` (onder 0,1 GiB in MiB, daarboven in GiB). Die staan er niet voor de sier; zonder die macro's staat er een rij nullen op elke regel.

**Deel ze in plaats van ze te kopiëren.** Twee kaarten die dezelfde getallen anders schrijven is precies hoe dit uit elkaar gaat lopen. Zet ze in een eigen sjabloon (bijvoorbeeld `bg/_resource-formats.html.j2`) en importeer ze op beide plekken, of gebruik `{% from "bg/_resource-usage.html.j2" import cores, gib %}` als dat schoon werkt. Kies één van de twee en schrijf op waarom.

## Wat er nu staat

- De kaart: `opi/templates_lotc/bg/_dashboard-usage.html.j2`, het blok `{% if total_cpu_usage > 0 %}` onderaan (`panel("Verdeling over projecten", ...)`).
- De gegevens: `collect_dashboard_metrics` in `opi/web/router.py` (rond regel 844). De lus rond regel 1014 zet **alleen** `cpu_cores` per project. Dat is waarom er geen geheugen te tonen is: het wordt nooit gemeten.
- Twee routes gebruiken die functie: de dashboardroute zelf en het fragment `/dashboard/resource-usage`.

## Taken

### 1. Meet ook het geheugen per project

In de lus in `collect_dashboard_metrics` naast `cpu_cores` ook `memory_mb` zetten, met dezelfde query die de projectkaart gebruikt:

```
sum(container_memory_working_set_bytes{namespace=~"<namespaces van dit project>",container!=""})
```

Working set en niet de limiet: dat is wat er werkelijk in gebruik is, en dat is dezelfde meting als op de projectpagina. Deel door 1024² voor MiB. Zet bij een mislukte query of een project zonder namespaces `0.0`, net als de CPU-tak dat doet.

Overweeg meteen de **limiet** per project op te halen (`sum(kube_pod_container_resource_limits{namespace=~"...",resource="memory"})` en dezelfde voor `cpu`). De vorm van de projectkaart toont "gebruikt / limiet (percentage)", en zonder limiet is er geen percentage en geen balk om tegen af te zetten. Zonder limieten wordt het aandeel van het totaal, en dan wijkt de vorm af van wat gevraagd is. **Dit is de enige echte ontwerpbeslissing in dit plan: leg vast wat de balk voorstelt, en schrijf op waarom.**

Let op de kosten: dit is twee tot vier Prometheus-queries per project erbij, op een fragment dat al lazy geladen wordt. Meet hoeveel projecten er in de praktijk zijn en of het merkbaar is. Is het merkbaar, dan kan één query met `by (namespace)` alle projecten in één keer leveren in plaats van een query per project; dat is dan de betere weg en meteen een verbetering voor CPU.

Verifieer: `/dashboard/resource-usage` levert per project een `memory_mb` groter dan nul zodra er pods draaien.

### 2. Geef door wat het sjabloon nodig heeft

Het fragment berekent `total_memory_usage` al maar geeft het **niet** mee in de context (`opi/web/router.py`, de `render(...)`-aanroep van `dashboard_resource_usage_fragment`). Een sjabloon dat ernaar vraagt krijgt dan `Undefined`, en dat is een 500 op het hele fragment. Controleer beide routes op dezelfde omissie.

### 3. De kaart

Vervang het blok in `bg/_dashboard-usage.html.j2` door een lijst per project in de vorm van taak "De vorm die overgenomen moet worden": per project de naam, daaronder de regel voor geheugen en de regel voor CPU.

Sorteren op geheugen, aflopend.

Twee dingen die in dit sjabloon eerder zijn misgegaan en die je een ronde schelen:

- De omgeving staat op **StrictUndefined**. `selectattr('memory_mb', 'defined')` valt daardoor al om bij het LEZEN van een sleutel die een project zonder meting niet draagt, en `sort(attribute='memory_mb')` net zo. Bouw de lijst met `.get()`.
- De guard moet tellen wat er getoond gaat worden, niet het totaal. Op het totaal stond hij, en toen toonde de kaart zijn kop met daaronder niets: het totaal was net boven nul terwijl geen enkel project door de lus kwam. En als er niets te tonen is, zeg dat dan (`<c-alert type="info">`) in plaats van de kaart te laten verdwijnen; een kaart die zonder uitleg weg is, leest als kapot.

### 4. Een test die op het antwoord meet

Er is geen enkele test op deze kaart, en dat is waarom dit drie keer mis kon gaan zonder dat iets rood werd.

1. Het sjabloon met testdata renderen: twee projecten met geheugen en CPU, één zonder enige meting. Assertie: beide lijsten staan er, de volgorde is aflopend op geheugen, en het project zonder meting laat de render niet omvallen.
2. De lege toestand: geen project met een meting geeft de kaart MET een melding, niet een kop zonder inhoud en niet een verdwenen kaart.
3. De schrijfwijze: 512 MiB komt eruit als `512 MiB` en 2 GiB als `2.0 GiB`; 0,03 core als `30m`. Dit is de test die borgt dat de twee kaarten dezelfde getallen hetzelfde schrijven.

### 5. Nakijken op het scherm

`scripts/kijk_sandbox.py /dashboard` en er daadwerkelijk naar kijken. De kaart hoort direct onder Resource Usage te staan en op één scherm te lezen zijn.

## Wat er buiten valt

- De kaart Resource Usage zelf (de vier meters) blijft zoals hij is.
- De kaart op de projectpagina verandert niet; hij is hier de bron van de vorm, niet het onderwerp.
- Geen historie of grafiek per project. Dit is de toestand van nu.

## Volgorde

1. Meten (taak 1) → verifieer: `memory_mb` komt per project binnen.
2. Doorgeven (taak 2) → verifieer: het fragment geeft 200 en geen 500.
3. De kaart (taak 3) → verifieer: op het scherm, in de vorm van de projectkaart.
4. Test en nakijken (taak 4 en 5).
