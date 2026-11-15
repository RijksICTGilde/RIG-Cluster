# Tekstnormen

Deze normen gaan voor op de algemene checklist van de tekstfase.

## Welke taal waar

- **Code is Engels**: namen, commentaar, docstrings en testnamen. Bestaande Nederlandse code wordt niet met terugwerkende kracht vertaald, behalve in een ronde die daar expliciet voor is.
- **Tekst die de gebruiker kan zien is Nederlands, en blijft letterlijk staan.** Dat is meer dan wat in een template staat. Ook deze strings gelden als tekst voor de gebruiker, ook al staan ze in Python:
  - de melding in een `raise` (`ValueError`, `HTTPException`, een eigen exceptie) die via een API-antwoord, een taakfout of een flash-melding bij de gebruiker kan komen;
  - `detail=`, `message`, `error` en `error_message` in een antwoord of taakresultaat;
  - teksten voor `fail_task`, `fail_subtask`, `fail_project` en andere voortgangsmeldingen;
  - labels, hulpteksten en validatiemeldingen in editables en formulieren.

  Vertaal zo'n string nooit naar het Engels, ook niet als de code eromheen Engels wordt. Twijfel je of een string de gebruiker bereikt, laat hem dan staan en zet hem in je verslag.
- Een logbericht (`logger.*`) is code en mag Engels worden. Het logt nooit een waarde.

## Toon en begrippen

- De gebruiker is "je", nooit "u" (zie `build.md`).
- Vaktermen blijven Engels; de woordenlijst staat in `CLAUDE.md` ("Woordenlijst").
- Nooit em dashes of en dashes.
