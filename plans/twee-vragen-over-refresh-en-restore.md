# Een samengevoegde refresh, en een restore die de aanroeper de schuld geeft

Status: plan, 12 augustus 2026. Aanleiding: vraag 8 en 9 uit `plans/vragen-uit-zad-cli.md`, gesteld door het zad-cli-project. Onder beide staat `<!-- ruimte voor RIG-Cluster -->`; dit plan vult die plekken.

Twee losse onderwerpen die alleen samen in één taak zitten omdat ze klein zijn en dezelfde lezer bedienen. Doe ze in deze volgorde: 9 is een ingreep, 8 is eerst een meting.

## 9. Een restore die faalt op de opgegeven bestemming

### Wat er nu is

Een restore naar een hostnaam die niet resolvet levert een **500** met de pod-logs in `message`, en zonder `ErrorCategory`. De CLI kent zijn exit code toe op de statuscode, dus 500 wordt bij hen "platform, probeer later opnieuw" en een pijplijn blijft een typefout in `--target-host` herhalen.

Hun verzoek is bescheiden en juist: een 4xx **of** een `ErrorCategory`, één van beide is genoeg. En ze zeggen er expliciet bij dat ze niet op de tekst van een logregel gaan raden, want dat was hun fout bij vraag 1. Die houding verdient een antwoord dat ze niet dwingt alsnog te raden.

### Wat er moet gebeuren

De fout komt uit de restore-pod, dus je weet pas achteraf dat het aan de invoer lag. Onderscheid daarom bij het afhandelen van een mislukte pod of het een **bestemmingsfout** is, en geef dat door.

`ErrorCategory` staat in `opi/api/v2/models.py:35` en is bewust breder dan letterlijke Kubernetes-redenen; een categorie erbij die zegt "de opgegeven bestemming was niet bruikbaar" past in die opzet. Kies zelf of het antwoord daarnaast ook een 4xx wordt; de CLI heeft aan één van beide genoeg, en een 4xx is de duidelijkste omdat elke client daar al op stuurt.

**Wat NIET de bedoeling is:** de logtekst afspeuren naar `could not translate host name`. Dat is dezelfde valkuil waar de CLI net uitgeklommen is, en het breekt zodra PostgreSQL zijn melding herformuleert. Als de pod-uitkomst niet genoeg is om de oorzaak te bepalen, is dat de echte vondst en hoort die in het antwoord.

Let op de grens: een bestemming die wél resolvet maar een verkeerd wachtwoord heeft, is óók invoer van de aanroeper. Een database die halverwege wegvalt is dat niet. Zeg in het antwoord welke gevallen onder de categorie vallen en welke niet.

## 8. Twee refreshes over elkaar heen

### De vraag

Een tweede `project refresh` tijdens een lopende geeft hetzelfde `task_id` terug: er komt geen tweede taak en de eerste wordt niet afgebroken. De CLI mat dat een wijziging die **na** de start van de eerste taak werd opgeslagen tóch werd uitgerold, en vraagt of dat gegarandeerd is of geluk met de timing.

Het gevaar dat zij benoemen is precies het juiste: een wijziging die net te laat komt valt stil buiten die refresh terwijl `pending` op 0 gaat. Van buitenaf is dat verschil niet te zien.

### Dit is een MEETVRAAG, geen bouwopdracht

Beantwoord hem door te meten, niet door de code te lezen en te concluderen. De kern is één ding: **leest de lopende taak het projectbestand opnieuw, of één keer bij de start?**

* Leest hij opnieuw, dan is het gedrag echt en mag het gedocumenteerd worden. Zeg dan ook op welk moment hij leest, want dat bepaalt hoe groot het venster is.
* Leest hij één keer, dan bestaat het venster wel degelijk en hadden zij geluk. Zeg dan waar het zit en hoe groot het is, en of `pending` op 0 komt terwijl er nog iets openstaat. Dat laatste is het gevaarlijke deel.

Het samenvoegen zelf zit rond `opi/core/task_manager.py:206` ("only if one isn't already running"); dat is het aanknopingspunt, niet de conclusie.

**Verzin geen oplossing voordat de meting er is.** Blijkt er een venster te zijn, dan is de vraag of dit plan het moet dichten of alleen benoemen; dat besluit hoort bij de uitkomst en niet vooraf. Een refresh die na afloop opnieuw kijkt of er intussen iets veranderd is, is één mogelijke uitkomst, maar niet iets om nu al in te bouwen.

## De toets

- vraag 9: een restore naar een onbereikbare of geweigerde bestemming levert een antwoord waaruit een client zonder de logtekst te lezen kan afleiden dat het aan zijn invoer lag;
- een restore die faalt door iets aan onze kant blijft onderscheidbaar van die eerste;
- vraag 8: er staat een gemeten antwoord, met het moment van lezen erbij, en als er een venster is ook hoe groot en of `pending` dan liegt;
- beide `<!-- ruimte voor RIG-Cluster -->` in `plans/vragen-uit-zad-cli.md` zijn ingevuld, in dezelfde toon als de antwoorden op 1 tot en met 7.

## Waar op te letten

**Geen credentials in de melding.** Vraag 9 gaat over een bestemming die de aanroeper opgaf, inclusief een wachtwoord. Wat er terugkomt mag zeggen dát de bestemming niet bruikbaar was en welk veld het betrof, nooit de waarde.

**RC-81 raakte ditzelfde pad.** De doelvelden zijn daar optioneel geworden. Een restore zonder doelvelden kan per definitie geen bestemmingsfout van de aanroeper zijn, want dan koos het platform de bestemming; die gevallen horen dus niet in de nieuwe categorie.
