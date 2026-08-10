# Geen roos-HTML meer in een LOTC-pagina

Status: uitgevoerd in RC-64 (PR #63), 10 augustus 2026. Het plan zoals goedgekeurd staat
hieronder; wat er van geworden is staat in `features/lotc-geen-roos-html.md`.

## Wat een gebruiker zag

Op de projectdetailpagina stond de sectie Bijlagen als kale, ongestileerde HTML: een kop,
een regel tekst, een bestandsnaam, `id: test`. Geen kaart, geen opmaak. En in een dialoog
stond een knop met RVO-vormgeving tussen componenten die dat niet hadden:

```html
<button class="utrecht-button utrecht-button--rvo-md utrecht-button--primary-action"
        data-roos-component="button" type="button" onclick="location.reload()">Sluiten</button>
```

Dat `data-roos-component` is het bewijs van de oorzaak: LOTC zet dat attribuut niet. Deze
HTML is door jinja-roos gerenderd en daarna in een LOTC-pagina geplakt.

## De oorzaak

Twee plekken injecteerden bewust ROOS-HTML in een LOTC-pagina, allebei gedocumenteerd als
een afweging:

1. **De detailblokken die diensten zelf leveren.** `bg/project-tabs.html.j2` rendeerde ze
   met `render_roos()`, omdat die sjablonen bij hun dienst staan en in roos-componenten
   geschreven zijn - en twee componentsystemen kunnen niet in een Jinja-omgeving.
   Drie diensten: attachments, invite en keycloak (37 roos-componenttags, 16 rvo-klassen).
2. **Het voortgangsfragment.** `web/task_progress.py` rendeerde onvoorwaardelijk uit de
   ROOS-omgeving, terwijl er een LOTC-tegenhanger bestond die door niets werd gekozen.

## Waar de afweging stukliep

Het commentaar zei: zo'n blok ziet er anders uit, en dat is zichtbaar onaf. Dat
veronderstelt dat de rvo-klassen nog iets doen. RC-62 had gemeten dat dat niet zo is: de
LOTC-omgeving laadt `["lotc-layout", "nldd", "lotc-forms"]` en `lotc_rvo` staat er niet
bij. Het resultaat was dus niet "zichtbaar anders" maar volledig onopgemaakt.

## Wat er is gebeurd

- **Fase 1:** het voortgangsfragment gaat door `lotc_switch`. De LOTC-tegenhanger was zelf
  kapot (dode afsluitknoppen, ROOS-iconnamen, een foutmelding zonder suggestie) en is
  meegerepareerd.
- **Fase 2:** attachments, invite en keycloak leveren een `section-detail-lotc.html.j2` in
  hun eigen map; `lotc_counterpart()` kiest, `render_roos()` blijft de ondergrens. Ook
  `keycloak/otp-code.html.j2` kwam op een LOTC-pagina uit en heeft een tegenhanger.
- **Fase 3:** een poort die elke LOTC-pagina rendert en faalt op `data-roos-component` of
  `rvo-`, plus een poort die een dienst zonder tegenhanger tegenhoudt en een die beide
  vormgevingen op gedrag vergelijkt.

## Gemeld, niet opgelost

54 `lotc_onclick_N`-variabelen in 32 templates worden gezet en nooit aan hun knop gehangen.
Geen van die bestanden is vanaf een gebruikersroute bereikbaar; het is een eigen taak.
