# De restore is van buiten niet te vinden, en zijn foutcategorie zegt niets

Status: plan, 13 augustus 2026. Vraag 10 en 11 uit `plans/vragen-uit-zad-cli.md`. Allebei raken ze het werk van RC-81 en RC-82, en allebei zeggen ze hetzelfde: het vermogen is er, maar van buitenaf niet te gebruiken.

## 10. Geen enkele naam wordt geaccepteerd

`restore database` zonder doelvelden hoort in de eigen database terug te zetten (RC-81). De CLI krijgt de referentienaam niet geraden, en heeft er zes geprobeerd: `backup`, de componentnaam, de deploymentnaam, de projectnaam, `database` en `postgresql-database`. Alle zes een 404.

En dit is het scherpe deel: **de twee leesendpoints noemen `backup`**, en juist die naam wordt geweigerd.

```
zad backup list  -> reference_name: "backup"
zad restore list -> pvc_name: "backup"
POST .../restore/database/.../backup -> 404 "No deployment ... has a database backup named 'backup'."
```

Zoek uit welke naam de route wél verwacht, en beantwoord dan de vraag die eronder zit: **is die naam van buitenaf te vinden?** Staat hij alleen in het projectbestand, dan is de nieuwe weg onbereikbaar en is de echte opdracht dat de leesendpoints de naam noemen die de schrijfkant accepteert. Twee endpoints die een naam publiceren die een derde weigert, is erger dan geen naam publiceren.

Kijk ook of de 404-melding kan zeggen welke namen er dan wél zijn. Nu noemt hij alleen wat niet bestond.

## 11. `error_category` zegt `Unknown` bij het schoolvoorbeeld

RC-82 leverde `InvalidTarget` voor een bestemming die de aanroeper opgaf. Het veld komt mee, maar bij `doel.invalid` (een gereserveerd domein dat per definitie niet resolvet) staat er `Unknown` en is het antwoord een 500.

De doorgifte werkt dus en de classificatie niet. Zoek uit of `InvalidTarget` ergens wél geproduceerd wordt of dat de bedrading niet is aangesloten, en meet dat met precies dit geval.

**Let op de valkuil die de CLI zelf noemt.** Ze gaan niet op de logtekst raden, en terecht: `could not translate host name` is een formulering van PostgreSQL en die verandert. De classificatie hoort te komen uit wat de restore-pod doet, niet uit hoe zijn foutmelding luidt. Kan dat niet, dan is dát het antwoord, en dan weten zij tenminste waar ze aan toe zijn.

Ter kennisname: zij behandelen `"Unknown"` inmiddels als "niet toe te wijzen" en niet meer als "probeer opnieuw". Dat is een redelijke conclusie uit ons antwoord, en het betekent dat een verkeerd ingevulde `Unknown` hun pijplijn niet meer laat hangen maar wel de verkeerde kant op stuurt.

## De toets

- een restore zonder doelvelden is te doen met een naam die uit `backup list` of `restore list` komt, of die endpoints noemen de naam die werkt;
- de 404 zegt welke namen er wel zijn;
- `doel.invalid` levert `InvalidTarget` op, of er staat opgeschreven waarom dat niet kan;
- de classificatie leunt niet op de tekst van een logregel;
- beide `### Antwoord`-plekken in `plans/vragen-uit-zad-cli.md` zijn ingevuld, in dezelfde toon als de antwoorden op 1 tot en met 9.

## Waar op te letten

**Dit is de derde ronde over dezelfde weg.** RC-81 maakte de doelvelden optioneel, RC-82 gaf de foutcategorie. Beide zijn per stuk getoetst en geen van beide is van buitenaf gebruikt. Toets dit met een echte aanroep van buiten, met een API-sleutel en zonder kennis van het projectbestand - dat is de positie waarin de CLI staat.
