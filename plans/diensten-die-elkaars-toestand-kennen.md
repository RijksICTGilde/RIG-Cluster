# Diensten die elkaars toestand kennen, en een gezondheidscheck die dat gebruikt

Status: plan, 5 augustus 2026. Niet gebouwd. Aanleiding: een herverwerking meldde een mislukte deploy terwijl ArgoCD de applicatie Synced en Healthy noemde. De melding klopte niet, en de reden daarvoor is structureel.

## Wat er gebeurde

Een slapende deployment schaalt zijn component naar nul en sleep-mode zet er een waker voor in de plaats. Die waker draagt bewust hetzelfde `app`-label, want hij moet de Service van het component overnemen om het verkeer op te vangen dat de applicatie wakker maakt.

De gezondheidscheck selecteerde op dat ene label. Gemeten op het draaiende cluster:

```
app=productie-frontend            -> productie-frontend-waker-...  Running
app=productie-frontend,!zad-role  -> (niets)
```

Hij vond dus de waker, las diens `ImagePullBackOff`, en rapporteerde:

> productie - frontend: image ophalen mislukt ... zad-waker:latest

voor een component dat niet draaide en dat image niet gebruikt. En het is niet cosmetisch: achter die melding hangt logica die het component **uitschakelt** bij een image-pull-fout, dus een waker die even niet kan pullen kon het echte component uit de lucht halen.

De labelkant is opgelost (commit `4b86aed7`): `zad-role` is nu een platformbegrip en applicatiebrede opzoekingen sluiten alles uit dat het draagt. Dat is de voorwaarde, niet de oplossing.

## Waarom dat niet genoeg is

Wat er ontbreekt is dat de ene dienst niets **weet** van de situatie die de andere heeft veroorzaakt. De gezondheidscheck leidt af uit een label dat er geen applicatiepods zijn; hij hoort te horen dat deze deployment slaapt en dat nul pods daar de bedoelde toestand is.

Dat verschil telt zodra er een derde geval bijkomt. Elke dienst die iets naast de applicatie zet, of die de applicatie tijdelijk stillegt, veroorzaakt hetzelfde soort verwarring, en dan is een label per geval weer een pleister.

En er is een tweede scheefheid: **de gezondheidscheck is zelf geen dienst.** Hij staat als platformcode in `opi/services/oom_watcher.py`, terwijl `resource-tuning`, `platform`, `user-env-vars` en `aliases` wél systeemdiensten zijn. Dezelfde soort logica leeft dus op twee plekken met twee vormen.

## Voorstel

**A. Een dienst mag toestand bijdragen aan een deployment.** Een nieuw haakpunt naast `AFTER_SYNC`, waarin generieke code de registry vraagt: wat weten jullie over deze deployment? Sleep-mode antwoordt "slaapt sinds X, gewekt via Y". Dat is dezelfde vorm als `detail_page_sections` en `collect_deployment_actions`: de dienst bezit zijn eigen kennis en de algemene laag noemt geen dienst bij naam.

Let op de aard van het antwoord. Dit is **geen** gezondheidsoordeel maar een feit over de deployment, met een vlag erbij of het verwachte aantal applicatiepods nul is. Dat onderscheid moet scherp blijven, anders wordt "ik ben aan het slapen" ongemerkt "en dus is alles in orde", en dan verbergt een dienst een echte storing.

**B. De gezondheidscheck wordt een systeemdienst.** `ServiceKind.SYSTEM` bestaat al en heeft al vier bewoners; `resource-tuning` is er de duidelijkste voorbeeldbewoner van, want die observeert ook deployments. De check verhuist daarheen, vraagt eerst de toestand op bij A, en oordeelt daarna. Een deployment die volgens een dienst nul pods hoort te hebben is niet ziek, en dat is dan een uitspraak op basis van wat de dienst zegt, niet van wat het cluster toevallig laat zien.

**C. Die toestand hoort ook zichtbaar te zijn.** Op de deploymentweergave moet staan dat hij slaapt; nu kan iemand zich afvragen waarom er niets draait. Dat is dezelfde bijdrage uit A, alleen anders gerenderd, dus het hoort er in één keer bij en niet als apart mechanisme.

## Volgorde

1. Het haakpunt en het antwoordtype, met sleep-mode als eerste bewoner en zonder dat er iets van gedrag verandert. Verifiëren: een test die vastlegt dat een slapende deployment meldt dat hij slaapt en dat nul pods verwacht is.
2. De gezondheidscheck naar een systeemdienst, gedrag ongewijzigd. Verifiëren: dezelfde fouten worden nog steeds gevonden (OOM, CrashLoopBackOff, image-pull) op een deployment die niet slaapt.
3. De check laat de toestand meewegen. Verifiëren: een slapende deployment met nul pods geeft geen fout, en een niet-slapende deployment zonder pods nog steeds wél.
4. De toestand tonen op de deployment.

Stap 3 is de enige die gedrag verandert, en die wil je pas doen als 1 en 2 groen liggen.

## Waar op te letten

**Een dienst mag geen storing kunnen verbergen.** Dit is het echte risico van dit plan. Als "ik ben aan het slapen" volstaat om een deployment gezond te noemen, dan maakt een dienst met een verkeerde toestand een echte storing onzichtbaar. Daarom moet A een feit teruggeven en niet een oordeel, en moet de check zelf beslissen wat dat betekent. Neem een test op die faalt zodra een dienst een niet-slapende deployment gezond kan verklaren.

**De toestand komt uit het projectbestand, niet uit het cluster.** Sleep-mode houdt zijn toestand in het projectbestand bij (`sleeping` / `waking` / `awake`). De check leest nu het cluster. Die twee kunnen tijdens het wekken uit elkaar lopen, en `waking` is precies het moment waarop er wél weer pods horen te komen. Bepaal expliciet wat er in die tussentoestand geldt.

**Dit raakt de remediatie.** Het uitschakelen van een component bij een image-pull-fout hangt aan deze meldingen. Zolang stap 3 niet af is, blijft de labelfix uit `4b86aed7` het enige dat dat pad beschermt, en die is dus niet optioneel.
