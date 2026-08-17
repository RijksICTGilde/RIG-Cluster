# Veel acties op één project, zonder een commit en een push per handeling

**Status**: onderzoeksvraag, alleen een plan. Nog niet bouwen.

Agents gebruiken de CLI anders dan mensen: ze combineren commando's en vuren ze snel achter elkaar af. Bij ons is elke wijziging een eigen commit en een eigen push naar de projectenrepository. Bij tien handelingen op één project zijn dat tien keer clone-of-verversen, tien keer committen en tien keer pushen, en dat werkt vertragend.

De vraag is niet "hoe debouncen we de push" maar **waar de tijd werkelijk heen gaat en wat we mogen samenvoegen zonder iets stuk te maken.** Dit plan zet die vraag op, met de valkuilen die we al kennen.

## Meet eerst, ontwerp daarna

Niets bouwen voordat dit op tafel ligt, want het bepaalt of het antwoord "push uitstellen" is of iets heel anders:

1. **Waar gaat de tijd heen bij tien opeenvolgende acties op één project?** Splits per stap: het ophalen of verversen van de werkboom, valideren, committen, pushen, en het wachten op ArgoCD. Meet het, schat het niet. Het is heel goed mogelijk dat de push niet de duurste is en dat we het verkeerde probleem oplossen.
2. **Zet elke actie werkelijk een eigen werkboom op?** De opdrachtgever vermoedt van wel. Is dat zo, dan is dát de kostenpost en niet de push.
3. **Wat is de spreiding in de praktijk?** Komen die acties binnen milliseconden of binnen seconden? Een wachttijd van twee of drie seconden helpt alleen als het gedrag daar werkelijk onder valt.

## Vier richtingen, met hun prijs

**A. Alleen de push uitstellen.** Lokaal committen per wijziging, pas pushen als het een paar seconden stil is. Goedkoopst en de git-historie blijft intact. Prijs: er is een venster waarin een wijziging wel gecommit is maar nog niet gedeeld, en de vraag hieronder over het taakresultaat wordt scherp.

**B. Commits samenvoegen.** Eén commit voor een reeks handelingen. Leest prettiger, maar het herschrijft geschiedenis en dat raakt dingen die van die geschiedenis afhangen (zie de valkuilen).

**C. Samenvoegen in de takenwachtrij.** Staan er meerdere taken voor hetzelfde project klaar, verwerk ze dan in één doorloop met één commit en één push. Dit lijkt me de meest kansrijke, want het pakt ook het verversen en valideren mee, niet alleen de push. Prijs: de taken moeten samen te voegen zijn zonder dat hun volgorde of hun afzonderlijke resultaat verandert.

**D. Niets doen aan git, wel aan wat eromheen zit.** Als de meting laat zien dat de werkboom of ArgoCD de kosten maakt, is dit het eerlijke antwoord.

## De valkuilen die we al kennen

**Het taakresultaat is een belofte.** Vandaag betekent "klaar" dat de wijziging in git staat. Wordt de push uitgesteld, dan betekent het dat niet meer, terwijl de CLI juist op die status wacht. Elk ontwerp moet expliciet beantwoorden wat een client te zien krijgt en wanneer, anders ruilen we snelheid voor een leugen.

**De ProjectStore is per proces en niet globaal.** Een wachttijd die in één proces leeft, doet niets zodra er meer dan één instantie draait. Beantwoord wat er gebeurt bij twee replica's, ook al draaien we er nu één.

**Er hangt werk aan de git-historie.** De prune-stap reconstrueert de vorige YAML met een file-scoped diff; daar is eerder een ondiepe kloon op stukgelopen (`project_prune_diff_blobless`, issue #159). Commits samenvoegen of overslaan mag die reconstructie niet breken.

**Uit elkaar lopen is duurder dan traag zijn.** Een uitgestelde push betekent een groter venster waarin onze werkboom en de remote verschillen. Vandaag is precies dat de oorzaak geweest van een project dat deterministisch vastliep: een bestaanscontrole die op verouderde gegevens besliste.

**Verliezen mag nooit.** Valt het proces om binnen het wachtvenster, dan moet duidelijk zijn wat er met de niet-gepushte commits gebeurt en hoe ze alsnog landen.

## Wat dit plan oplevert

Een aanbeveling met cijfers eronder: waar de tijd heen gaat, welke van de vier richtingen (of welke combinatie) de winst pakt, wat het kost, en wat het antwoord is op de vier valkuilen hierboven. Plus een voorstel voor hoe het taakresultaat zich gedraagt, want dat is de enige echte gedragswijziging voor een client.

Geen implementatie in deze taak.
