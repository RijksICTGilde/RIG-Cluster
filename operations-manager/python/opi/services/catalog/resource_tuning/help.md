# Resource tuning

Een systeemdienst: deze staat altijd aan en is niet iets wat je aanvinkt. Het platform kijkt naar wat je componenten werkelijk aan geheugen en CPU gebruiken en stelt de grenzen daarop bij.

## Wat doet het voor je?

- Loopt een component tegen zijn geheugengrens aan (een OOM-kill), dan wordt die grens verhoogd
- Vraagt een component structureel te veel, dan wordt het teruggebracht naar wat het nodig heeft
- Je hoeft zelf geen limieten te gokken bij het aanmaken van een component

## Wat gebeurt er precies?

Na een uitrol en 's nachts worden de metingen bekeken, uit de VPA-recommender of uit Prometheus. Nieuwe waarden worden in je projectbestand geschreven, met een regel in de geschiedenis die zegt waarom. Ze worden dus zichtbaar en navolgbaar doorgevoerd, net als elke andere wijziging.

Je kunt zelf grenzen blijven instellen bij een component. Zet je een waarde zelf, dan is dat je keuze en laat het platform juist dat veld met rust, zolang je wilt. Op het tabblad Componenten zie je per veld of het platform hem bijstelt of dat jij hem hebt vastgezet, en daar geef je hem met een knop weer terug aan het platform.

Alleen bij een geheugentekort (een OOM-kill) wordt een vastgezette geheugenlimiet toch verhoogd: anders blijft je component liggen.

Wil je niet dat het platform meestuurt voor een bepaald component, dan kun je dat per component uitzetten.
