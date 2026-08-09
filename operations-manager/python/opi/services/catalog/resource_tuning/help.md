# Resource tuning

Een systeemdienst: deze staat altijd aan en is niet iets wat je aanvinkt. Het platform kijkt naar wat je componenten werkelijk aan geheugen en CPU gebruiken en stelt de grenzen daarop bij.

## Wat doet het voor je?

- Loopt een component tegen zijn geheugengrens aan (een OOM-kill), dan wordt die grens verhoogd
- Vraagt een component structureel te veel, dan wordt het teruggebracht naar wat het nodig heeft
- Je hoeft zelf geen limieten te gokken bij het aanmaken van een component

## Wat gebeurt er precies?

Na een uitrol en 's nachts worden de metingen bekeken, uit de VPA-recommender of uit Prometheus. Nieuwe waarden worden in je projectbestand geschreven, met een regel in de geschiedenis die zegt waarom. Ze worden dus zichtbaar en navolgbaar doorgevoerd, net als elke andere wijziging.

Je kunt zelf grenzen blijven instellen bij een component. Wil je niet dat het platform meestuurt voor een bepaald component, dan kun je dat per component uitzetten.
