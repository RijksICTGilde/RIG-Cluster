# De tweede generale repetitie, en deze keer met de klok erbij

Status: plan, 13 augustus 2026. Aanleiding: de eerste doorloop (`docs/generale-repetitie-2026-08-12.md`) adviseerde nog niet uit te rollen en noemde drie blokkades. Die zijn opgelost (RC-88), plus vijf reparaties die onderweg bovenkwamen. Dit is de herhaling die moet uitwijzen of de weg nu vrij is.

Lees dat eerste verslag; wat daar al is aangetoond hoeft niet opnieuw bewezen te worden, alleen opnieuw gedraaid.

## Wat er sinds die doorloop is veranderd

* **De dubbele `id` op elk aanvinkvakje is weg**, aan beide kanten: LOTC `762e570` zet de `id` op `<nldd-checkbox-field>` in plaats van op de omhulling, en onze kant geeft hem alleen nog als prop mee. **Let op: dat is een pakketwijziging, dus het image moet herbouwd worden, een sjabloonsynchronisatie is niet genoeg.**
* **Een taak waarvan het werk mislukte meldt dat nu in zijn status.** Een handler kan op drie manieren falen en de worker las er één; een `{"status": "failed"}` uit een component- of dienstenhandler eindigde op `completed`.
* **Een geredigeerde aliaswaarde wordt niet meer afgekeurd.** Dit blokkeerde elke volgende opslag van de componenten-modal voor elk component dat ooit een alias had, ook voor wie iets heel anders wijzigde.
* **De sandbox-suite komt aan zijn eigen toetsen toe**, en elke test zet zijn eigen uitgangspunt klaar.
* **Knopmaten en -varianten hebben een regel met een bewaker** (`opi/core/buttons.py`), en **een aanvinkvakje volgt de opgeslagen waarde** (elk vakje met een converter stond aan).
* **De dashboardbanner liegt niet meer**: "alle" staat er alleen als het er ook alle zijn, en elke toestand krijgt zijn eigen regel.

## De harde eis van deze doorloop: het wachten

De vorige run duurde bij elkaar bijna **acht uur** over meerdere sessies, terwijl de tests zelf minuten kosten. Dat verschil zat in wachten, niet in werken. Dat mag niet nog een keer.

**Het budget: een project aanmaken en weer opruimen kost samen nooit meer dan een half uur.** Zit je daarboven, dan is dat een bevinding en geen omstandigheid: schrijf op wat er zolang duurde.

De regels staan in `workflow/build.md` en gelden hier onverkort:

* **wacht op de voorwaarde, niet op de klok.** `until <check>; do sleep 5; done`, niet `sleep 300`;
* **vraag het ding dat het weet.** Een taak vraag je aan het taakeindpunt (`wait_for_task()` geeft de UITKOMST), gezondheid en sync aan ArgoCD (`opi/services/argocd_overview.py` doet een heel project in één bevraging), een pod met `kubectl wait` / `kubectl rollout status`, de stand van een project aan de API;
* **een time-out is een vangnet.** Eindigt je wachten altijd op de time-out, dan wacht je niet maar gok je.

Kom je een plek tegen waar de code zelf slecht wacht, dan is dat een bevinding die je opschrijft. Repareer hem alleen als het evident is; is het een keuze, dan hoort hij in het verslag.

## Houd de tijd bij, per stap

Dit is een expliciete opdracht en geen bijvangst: **noteer per stap hoe lang hij duurde en waar die tijd heen ging.** Zonder dat blijft "het duurt te lang" een gevoel en kunnen we het niet verbeteren.

Per stap minstens: de wandkloktijd, en waar de tijd zat (wachten op ArgoCD, op een pod, op een taak, op een testsuite, op iets anders). Sluit af met een tabel van de duur per stap en een korte lijst van wat het langst duurde en waarom.

Wat wij daaruit willen kunnen aflezen: welke stap het langst duurt, of dat komt door echt werk of door wachten, en welke wachtplek het meeste oplevert als hij verbeterd wordt.

## Wat de doorloop moet aandoen

Begin bij een **schone sandbox** (`task sandbox:setup`), en controleer vooraf dat de sleutels kloppen: de sandbox heeft een eigen AGE-sleutel (`security/sandbox-key.txt`, niet `security/key.txt`) en draait op `*.sandbox.rijksapp.dev`. Klopt dat niet, dan faalt alles daarna om de verkeerde reden.

Draai **de volledige e2e-suite**: zowel `-m "e2e and not sandbox"` als `-m "e2e and sandbox"` met `E2E_BASE_URL` gezet.

Daarna dezelfde zes stappen als de vorige keer, want die dekten de keten:

1. **Conversie** van de projectbestanden zoals ze in de projects-repo staan: migreren naar de huidige schemaversie en daarna valideren, op de GEMIGREERDE gegevens.
2. **Aanmaken via de wizard**, meerdere diensten aan, tot de pods draaien.
3. **Hetzelfde via de API**, inclusief impliciete dienstselectie.
4. **Een tweede deployment**, met de TLS-override per deployment-component (RC-78) die de vorige doorloop expliciet NIET heeft getoetst.
5. **Backup maken en terugzetten**, met en zonder doelvelden, en een bestemming die niet resolvet.
6. **Reprocess** van een bestaand project.

En één toets erbij die uit de vorige doorloop volgt: **een afgewezen handeling moet nu een taak opleveren waarvan de status zegt dat het misging.** Dat was bevinding 5 en het is de reden dat een client kon denken dat het goed ging.

## De toets

- er is een verslag met per stap de duur en waar die tijd heen ging, plus een tabel en een oordeel over wat het meeste oplevert om te verbeteren;
- aanmaken plus opruimen van een project blijft onder het half uur, of er staat opgeschreven waarom niet;
- geen dubbele `id`'s meer, gemeten in de browser;
- een afgewezen handeling levert een taak op waarvan de status dat zegt;
- de TLS-override per deployment-component doet wat hij belooft;
- de volledige e2e-suite heeft gedraaid en wat rood is, is benoemd;
- een oordeel: kan dit naar productie of niet.

## Waar op te letten

**De paginamarge-test blijft rood** en dat is bekend: hij bewaakt een kolombreedte onder 1400 terwijl de gekozen bovengrens 1440 oplevert. Dat is een getal dat de eigenaar moet kiezen, geen fout. Noem hem, repareer hem niet.

**Repareer niet stilzwijgend.** Vind je iets kapot, dan is dat de opbrengst. Een kleine, evidente fout mag je meenemen; iets dat een besluit vraagt niet. De vorige doorloop leverde vijf reparaties op die het werk blokkeerden, en dat is precies de goede grens.

**De sandbox is gedeeld.** Ruim je testprojecten op en gebruik namen waaraan te zien is dat ze van deze doorloop zijn.
