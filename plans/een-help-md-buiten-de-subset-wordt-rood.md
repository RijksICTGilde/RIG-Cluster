# Een help.md buiten de subset van de renderer wordt rood, niet stil

Status: plan, 17 september 2026. Niet gebouwd. Komt uit de review van RC-167 (PR #161), destijds buiten die taak gehouden.

## Wat er is, gemeten

`markdown_to_components` (`opi/services/help_text.py:156`) ondersteunt met opzet een kleine subset: `# Titel`, `## Kop`, `- opsomming`, `**vet**` en links. De module legt in zijn eigen docstring uit waarom die subset klein is.

Alles daarbuiten, dus tabellen, code-fences, `###` en `*`-opsommingen, komt als LETTERLIJKE markdown in een `<c-paragraph>` op het scherm van de hulpdialoog. Er gaat niets stuk, er komt geen melding, het staat er gewoon als tekst.

Dat is in RC-167 een keer echt gebeurd: de herschreven `vlam/help.md` had 11 van de 34 alinea's letterlijke markdown, en de hele testsuite bleef groen. Het is met de hand ontdekt en met de hand gerepareerd.

`tests/test_service_help_markdown.py` heeft vandaag 22 tests. Die toetsen dat het bestand bestaat, dat het met een titel begint, dat er geen `<c-...>` onvertaald blijft staan en dat er een icoon in zit. Geen van die vier ziet een tabel die als tekst op het scherm belandt. Er zijn nu 24 help.md-bestanden.

## Wat er moet gebeuren

### 1. De grendel

Een geparametriseerde test over de diensten, naast de bestaande tests in `tests/test_service_help_markdown.py`: render de help.md, pak de inhoud van de `<c-paragraph>`-knopen, en weiger een alinea die begint met `|`, `#`, een code-fence, `- ` of `*`, of die een tabelrij bevat.

Toets de GERENDERDE uitkomst, niet de brontekst. Een test op het bronbestand zou hetzelfde lijken te doen maar iets anders meten: wat telt is wat er op het scherm komt.

Vandaag is dit op alle bestanden groen; dat is nagemeten. De tegenproef is de vorige versie van `vlam/help.md` (commit `79d77881`), die er 11 geeft. Gebruik die als vastlegging dat de test echt iets vangt, bijvoorbeeld door die tekst als fixture in de test te zetten.

### 2. De foutmelding doet het werk

De waarde van deze test zit niet in het rood worden maar in wat de schrijver van de volgende help.md eruit leest. Noem de vorm en het alternatief: "tabel in help.md wordt letterlijke tekst, gebruik een opsomming". Een melding als "alinea 7 voldoet niet" laat diegene zelf de subset uitzoeken.

### 3. De tekstfout uit dezelfde ronde

`opi/services/catalog/vlam/help.md:42`, de Java-regel. De `keytool -importcert`-aanroep is bij het herschrijven `-storepass changeit` kwijtgeraakt. Zonder die vlag vraagt keytool om het wachtwoord, en in een image-build is er geen tty om het in te typen, dus de opdracht faalt. `changeit` is bovendien het standaardwachtwoord van `cacerts`, dus juist het deel dat een lezer niet zelf verzint. Zet het terug.

## Buiten scope

De renderer uitbreiden. Dit gaat er niet over dat de subset klein is, maar erover dat je het merkt wanneer je erbuiten schrijft.

## De toets

- de nieuwe test is groen op alle 24 huidige help.md-bestanden;
- met de tekst van `vlam/help.md` uit commit `79d77881` is hij rood, en de melding noemt de vorm die het probleem veroorzaakt;
- een help.md met een tabel, een code-fence, een `###` of een `*`-opsomming wordt elk afzonderlijk afgekeurd;
- de bestaande 22 tests blijven groen;
- `grep -n "storepass" opi/services/catalog/vlam/help.md` geeft de keytool-regel terug.
