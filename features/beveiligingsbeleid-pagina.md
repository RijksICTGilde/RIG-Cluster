# Het beveiligingsbeleid in het portaal

`SECURITY.md` uit de root van de repo staat als pagina op `/security`, met een link "Beveiliging" onder "Platform" in de voettekst.

## Wat het doet

- De pagina is publiek, zonder inloggen: wie een kwetsbaarheid meldt is meestal niet ingelogd.
- `opi/web/security_policy.py` zet het bestand met `markdown-it-py` om naar HTML, één keer per proces. Ruwe HTML in de markdown wordt tekst.
- Een relatieve link (een pad in de repo) wordt een link naar `https://github.com/RijksICTGilde/RIG-Cluster/blob/main/<pad>`. Ankers en absolute links blijven staan, en elke kop krijgt het anker dat GitHub ervoor maakt.

## Configuratie

Geen. De Dockerfile kopieert `SECURITY.md` naar `/app`, naast `opi/`; `.dockerignore` sluit `*.md` in de root uit en maakt voor dit bestand een uitzondering. In een checkout leest de pagina het bestand uit de root van de repo.

## `/.well-known/security.txt`

Die route bestond al en verwijst door naar het centrale bestand van het NCSC, volgens de richtlijn voor de Rijksoverheid (`opi/server.py`).

## Tests

`tests/test_security_pagina.py`; de link in de voettekst loopt mee in `tests/e2e/test_lotc_links.py`.
