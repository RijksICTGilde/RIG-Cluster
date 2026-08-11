# Een build die de server niet omver trekt

Status: plan, 11 augustus 2026. Aanleiding: een sessie op de gedeelde server bouwde de operations-manager-image en trok de machine bijna om. Load 34,8, van de 15 GB nog 1 GB vrij, en de build faalde alsnog op onbereikbare pakketspiegels (`Ign:`-regels). Er draaiden op dat moment twee andere sessies.

## Wat er misging, gemeten

**De Dockerfile doet drie `apt-get`-rondes** (`operations-manager/Dockerfile`, regels 23, 37 en 64: basispakketten, kubectl, skopeo).

**De sandbox-bouwtaken gebruiken geen enkele cache.** `sandbox:build-operations-manager-image` en `sandbox:update-operations-manager` roepen `docker buildx build` aan zonder `--cache-from` en zonder `CACHE_IMAGE`. `publish-operations-manager` doet dat wél, via `docker-build-and-push` met `CACHE_IMAGE`; de sandboxkant is daar nooit aan toegevoegd.

Gevolg: elke sandboxbuild haalt die drie apt-rondes opnieuw op. Dat is normaal traag en vandaag fataal, want de spiegels waren slecht bereikbaar.

**Er staat geen grens op wat een build mag gebruiken.** Hij kan het hele werkgeheugen pakken, en dan sneuvelt alles eromheen: de andere sessies, en in het ergste geval het kind-cluster op diezelfde machine.

## Wat er moet gebeuren

1. **Cache aanzetten in de sandbox-bouwtaken.** Dezelfde vorm als `publish-operations-manager` al heeft. De apt-lagen veranderen bijna nooit; wat verandert is de code, en die zit in een latere laag. Dit is de kleinste wijziging met de grootste opbrengst.

2. **Een grens op het geheugen** van de build, zodat een build de machine niet kan opeten. Kies de waarde bewust en schrijf op waarom: te laag laat een build falen die het wel had gekund.

3. **Een controle vooraf.** Weigeren te beginnen als er te weinig vrij is, met een melding die zegt wat er draait. Nu begon de build gewoon terwijl er twee sessies stonden, en dat was van tevoren te zien.

4. **Opschrijven hoe een uitrol op de server hoort te gaan**, in `docs/`. Er is nu geen weg beschreven, en daardoor verzint iedereen er een. Dat is precies hoe dit ontstond.

## De vraag die er echt onder ligt

**Moet er op die machine wel gebouwd worden?** Er staat al een image `operations-manager:rc-66-` van een uur eerder; de bouw is niet het doel, een draaiende versie is het doel. Bouwen op een andere plek en het cluster laten trekken haalt dit probleem structureel weg.

Dat is een grotere verbouwing en hoort een eigen besluit te zijn, niet iets dat je er hier bij doet. Noteer hem, en doe eerst de vier stappen hierboven: die maken de huidige weg veilig genoeg om te gebruiken.

## Waar op te letten

**De schijf was niet het probleem** (10% vol), het geheugen wel. Ga niet aan opruimen van images beginnen; dat lost niets op en kost juist de cache die dit plan wil gebruiken.

**De `Ign:`-regels waren geen ruis.** Ze betekenen dat apt een spiegel niet kon bereiken en het bij de volgende probeerde. Wie ze afdoet als "traag maar vooruit" mist de oorzaak; met een cache komt apt er helemaal niet meer aan te pas.

**Toets het door hem twee keer te draaien.** De eerste build vult de cache, de tweede hoort merkbaar sneller te zijn en geen enkele apt-regel te tonen. Blijft apt draaien, dan pakt de cache niet en is stap 1 niet af.

**Draai die toets niet terwijl er andere sessies bezig zijn.** Dat is de fout die dit plan beschrijft, en hem herhalen tijdens het repareren zou pijnlijk zijn.
