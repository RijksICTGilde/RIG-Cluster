# Statische bestanden met een hash in de URL

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: een browser die een oud `wizard.js` vasthoudt geeft een bug die bij de een wel en bij de ander niet optreedt. Er is vandaag niets dat dat tegenhoudt.

## Wat er nu is, gemeten

Gemeten op de draaiende app, niet afgeleid uit de code:

```
/static/js/wizard.js
  cache-control    AFWEZIG
  etag             "2265dbe84b5671c46d7166ed3be87b29"
  last-modified    Thu, 06 Aug 2026 13:54:04 GMT
herbevraging met die etag -> 304
```

Dus: de correctheidsmachinerie staat er al. Een browser die hervraagt krijgt netjes een 304 en haalt niets opnieuw over de lijn. Wat ontbreekt is de *opdracht* om te hervragen. Zonder `Cache-Control` valt een browser terug op zijn eigen vuistregel, meestal "vers gedurende 10% van de leeftijd van het bestand". Dat gedrag is niet uitgesproken maar geërfd, en het verschilt per browser.

Verder gemeten:

- **18** echte `src="/static/` of `href="/static/` verwijzingen, verdeeld over **7** templatebestanden.
- **0** verwijzingen met een versieparameter.
- **0** plekken waar JavaScript zelf een `/static`-URL ophaalt, dus er is geen tweede weg die apart behandeld moet worden.
- `/static/roos/dist` is een **aparte mount** (`server.py`), en die URL's stuurt ROOS zelf uit. Wij hebben ze niet in de hand.
- Skaffold synct `static/**/*` naar de pod, dus in de ontwikkellus springt de mtime naar nu en is de vuistregel toevallig ongeveer nul. Dat maakt het probleem in dev klein en in productie niet.

## Waarom dit de moeite waard is, en waarom niet

**Niet voor de snelheid.** We hebben al ETags. Vandaag kost een paginalading een stuk of tien kleine 304's; met hashes worden dat nul verzoeken. Dat merkt niemand.

**Wel voor de klasse bugs die verdwijnt.** "Bij mij werkt het wel" door een browser die een vervangen bestand vasthoudt, is duur om te vinden: het symptoom zit in de UI, de oorzaak in een HTTP-header, en niets in de code wijst die kant op. Met een hash in de URL kan een oude kopie definitief niet meer geserveerd worden, want het is een andere URL.

**Het is geen oplossing voor het incident dat de aanleiding was.** Op 6 augustus stond er tussen 06:24 en 06:34 code in die de meldingsregel van elke servicekaart wiste bij het eerste vinkje. Dat was echt kapotte code, geen caching. Met dit plan erin had de gebruiker dat net zo goed gezien. Dit voorkomt de *volgende* keer dat een gerepareerd bestand niet aankomt, niet die ene.

## Voorstel

1. **Eén helper die de URL samenstelt.** Werknaam `static_url("js/wizard.js")` (voorstel, geen bestaande naam), geregistreerd als Jinja-global in `opi/core/templates.py`, die `/static/js/wizard.js?v=<8 tekens>` teruggeeft. De hash is een content-hash, gecached op `(pad, mtime, grootte)`. Dat ene `os.stat` per render is verwaarloosbaar en is precies wat het in de skaffold-lus laat werken: een gesynct bestand krijgt een nieuwe mtime, de hash wordt opnieuw berekend, de URL verandert, zonder herstart.

2. **De cache-header hangt aan de aanwezigheid van die parameter.** Een verzoek mét `?v=` krijgt `public, max-age=31536000, immutable`; alles zonder krijgt `no-cache`. Dit is de veilige vorm van het patroon en de reden is concreet: de ROOS-mount stuurt URL's uit die wij niet versieren, en een kale `immutable` op heel `/static` zou die een jaar lang vastzetten, inclusief de ROOS-bestanden die in dev live gesynct worden.

3. **De 18 verwijzingen omzetten** in die 7 bestanden.

4. **Een test die het gat dichthoudt**: faalt op elke kale `src="/static/` of `href="/static/` in `opi/templates/`, zodat wie er een toevoegt het meteen weet in plaats van over een half jaar.

5. **De ROOS-uitzondering apart nakijken.** `base.html.j2` propt de scripttags als string in een ROOS-attribuut (`additionalJs='<script src="/static/js/json-enc.js">...'`). ROOS hergeeft attribuutwaarden in dubbele quotes en heeft ons daar al drie keer mee te pakken gehad. Een `?v=` voegt geen quotes of brackets toe, dus het hoort te werken, maar dat moet in de gerenderde HTML gecontroleerd worden en niet aangenomen.

## Volgorde

1. De helper plus zijn test, zonder dat er één template verandert. Verifiëren: bestand wijzigen, opnieuw renderen, de URL is veranderd.
2. De cache-header. Verifiëren: twee verzoeken, met en zonder parameter, headers vergelijken.
3. De templates, en als laatste de ROOS-constructie in `base.html.j2`, met de gerenderde HTML als bewijs.
4. De guard-test, pas nadat alle 18 om zijn, anders is hij meteen rood.
5. Volledige suite plus de e2e-tests, want die laden echte statics in een echte browser.

## Waar op te letten

**`immutable` is een belofte van een jaar.** Hij mag alleen op een URL die de inhoud identificeert. Als stap 2 vóór stap 3 in productie komt, of als er één verwijzing wordt vergeten, zet je een bestand een jaar vast bij iedereen die het al opgehaald heeft. Daarom hangt de header aan de parameter en niet aan het pad: een vergeten verwijzing valt dan terug op `no-cache` en is hooguit suboptimaal, nooit kapot.

**De ROOS-mount is geen vergeten geval maar een bewuste uitzondering.** Schrijf op waarom die geen hash krijgt, anders repareert iemand dat later als "inconsistentie" en zet hij de ROOS-bestanden alsnog een jaar vast.

**Meet de headers, lees ze niet.** De meting bovenaan dit plan is gedaan met een echte request tegen de gemounte app. Doe dat na afloop opnieuw; een `Cache-Control` die je in de code ziet staan is geen bewijs dat hij ook op de lijn komt, zeker niet met middleware ertussen.

**Dit raakt elke pagina.** Een fout in de helper geeft geen nette foutmelding maar een pagina zonder styling of zonder JavaScript. Houd de suite en de e2e-tests groen na elke stap, want dat is het enige dat dit vroeg zichtbaar maakt.
