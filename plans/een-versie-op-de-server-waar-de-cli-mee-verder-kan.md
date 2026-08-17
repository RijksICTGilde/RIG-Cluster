# Een versie op de server waar de CLI mee verder kan

Status: plan, 11 augustus 2026. Kleine, afgebakende taak: bouw en rol uit op de sandbox van de server, zodat het zad-cli-project zijn draaiboek opnieuw kan afspelen.

## Waarom

RC-66 heeft de zes bevindingen van de CLI gerepareerd, waaronder de blokkade die elke deployment onmogelijk maakte. Die reparaties zitten in `naar-het-nieuwe-componentensysteem`, commit **`b07489ea`**. De server draait nog `2d04342f` van 10 augustus, 31 commits terug, dus het draaiboek zou daar nu op precies dezelfde zes bevindingen stuklopen.

## Wat er moet gebeuren

1. **Haal `naar-het-nieuwe-componentensysteem` op** en ga naar `b07489ea` of nieuwer op diezelfde branch.

2. **Bouw en rol uit** op de sandbox van de server. Het moet een echte rebuild zijn en geen hot-sync: RC-65 heeft het componentenpakket bijgewerkt naar NLDD 0.8.80, en een afhankelijkheid komt alleen met een build mee.

3. **Controleer via `/version`** dat de juiste commit draait. Sinds 10 augustus schrijven alle bouwtaken eerst `opi/version.json`, dus dat is een betrouwbare controle geworden; daarvoor kon dat bestand achterlopen en meldde een pod een commit die er niet in zat.

4. **Meld terug welke commit er draait**, zodat het CLI-project weet waar het tegenaan test.

## Waar op te letten

**Niet meeliften op RC-67.** Die is op dit moment ROOS aan het verwijderen uit dezelfde branch. Zou die bouwen en uitrollen, dan staat er een half gesloopte vormgeving op de server. Deze taak pakt daarom een vaste commit, niet "de laatste stand".

**Alleen bouwen en uitrollen.** Geen reparaties onderweg, ook niet als je iets ziet. Wat er misgaat is informatie voor de volgende taak; het doel hier is dat er een bekende, verse versie draait.

**De sandbox is gedeeld.** Kijk of er geen andere run bezig is voordat je uitrolt, en meld het als je hem overneemt.
