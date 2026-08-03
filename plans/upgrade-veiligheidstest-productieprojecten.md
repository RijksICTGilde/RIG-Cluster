# Upgrade-veiligheidstest: blijven de huidige projectbestanden werken?

Status: plan, 3 augustus 2026. Nog niet gebouwd. Basis: `branches-samenvoegen-naar-main`.

## 1. Aanleiding

De release die nu op `branches-samenvoegen-naar-main` staat verandert veel aan hoe een projectbestand gelezen wordt: elke service heeft een eigen configmodel en schemafragment gekregen, `validate_service_configs` loopt over alle vier de lagen, `project_v2.json` is opgeruimd, en RC-17 voegt een scope-keuze en meerdere schema's toe. Al die wijzigingen zijn per tak groen getest, maar telkens tegen testdata. De vraag die niemand beantwoord heeft is de enige die er voor gebruikers toe doet: gaan de bestaande projectbestanden hier ongeschonden doorheen, of raakt iemand stilletjes iets kwijt.

Dat risico is niet theoretisch. `dp-bn7` viel maandenlang bij elke herverwerking om op een schemagat, zonder dat iemand het zag, en blokkeerde daarmee alle deploys en de auto-tuner van dat project. Die klasse fout is stil: de verwerking faalt, de gebruiker ziet geen foutmelding, en er verandert simpelweg niets meer.

## 2. Wat er al ligt, gemeten op 3 augustus

| Wat | Waar | Wat het doet |
|---|---|---|
| Projectomzetting | `scripts/migrate_project_to_sandbox.py` | Zet `clusters` en per deployment `cluster` en `base-domain` om, herversleutelt `age-private-key` van de productiesleutel naar de sandboxsleutel, laat `config.keycloak` vallen. Neemt meerdere projecten in één aanroep. |
| Projecten aansturen | `scripts/sandbox_project_tool.py` | Haalt een API-sleutel op, verwijdert een project, zet een serviceconfig. Via HTTP met een ondertekend sessiecookie, dus zonder browser. |
| Schemamigratie | `opi/services/schema_migration.py` | `migrate_to_latest()`, `LATEST_SCHEMA_VERSION = 2.6`. |
| Testwerklast | `images/e2e-allservices/` | Statisch Go-binair, bindt elke gekoppelde dienst en meldt het oordeel op `/status`. Sinds 3 augustus publiek op `ghcr.io/minbzk/base-images/e2e-allservices`. |
| Golden-diff patroon | `tests/test_golden_manifests.py` | Byte-vergelijking van gerenderde sjablonen tegen vastgelegde bestanden, connector-vrij en offline. Draait op een verzonnen matrix, niet op echte bestanden. |

Wat er níet is: het omzetscript raakt images en poorten niet aan, en er is geen enkele controle die echte productiebestanden door de nieuwe leesweg haalt.

## 2a. De sleutelgrens, en waarom die het plan stuurt

De server heeft de productiesleutel niet en hoort die ook niet te krijgen. Dat is geen hindernis maar een randvoorwaarde die bepaalt waar elke stap draait.

**De omzetting is een lokale stap.** `migrate_project_to_sandbox.py` ontsleutelt `age-private-key` met de productiesleutel en versleutelt hem opnieuw met de sandboxsleutel. Dat kan alleen op een machine die `security/key.txt` heeft, dus op de werkplek. Wat daarna naar de Forgejo van de server gaat is uitsluitend de omgezette uitvoer: die is versleuteld met de sandboxsleutel en de productiesleutel komt er niet in voor. De server ziet dus nooit productiegeheimen, ook niet versleuteld.

**Laag 1 heeft helemaal geen sleutel nodig.** Migratie en validatie werken op de structuur van het bestand; de AGE-blokken zijn voor dat werk gewoon ondoorzichtige strings die nergens ontsleuteld worden. Die controle kan dus draaien op de productiebestanden zoals ze zijn, zonder enige sleutel, waar dan ook.

Dat maakt de rolverdeling simpel: omzetten gebeurt lokaal en met de hand, uitvoeren gebeurt op de server met alleen sandboxgeheimen. Het draaiboek moet dat expliciet zeggen, anders zoekt iemand later alsnog naar een manier om die sleutel op de server te krijgen.

## 3. Twee lagen, en de goedkope eerst

De verleiding is om meteen een cluster op te tuigen. Dat is de dure laag en hij dekt maar een steekproef. De goedkope laag dekt alles en draait in seconden, dus die hoort eerst.

### Laag 1: offline replay over alle productiebestanden

Neem alle productie-projectbestanden, haal ze door `migrate_to_latest()` en daarna door de volledige validatieketen van de nieuwe code, en rapporteer per bestand. Geen cluster, geen git, geen SOPS. Dit vangt precies de dp-bn7-klasse: een bestand dat de nieuwe validatie niet haalt en dus bij de eerstvolgende verwerking stil vastloopt.

Belangrijk detail dat eerder is misgegaan: valideer op de gemigreerde data, niet op het rauwe bestand. De verwerking migreert eerst in het geheugen en valideert daarna, dus een controle op het rauwe bestand meet iets anders dan wat productie doet.

Twee vergelijkingen zijn zinvol. De eerste is oud versus nieuw: haalt een bestand het bij de oude code en niet meer bij de nieuwe, dan is dat een regressie die deze release introduceert. De tweede is de migratie-uitkomst zelf: wat verandert `migrate_to_latest()` aan het bestand, en is elke verandering verklaarbaar.

Dit hoort een test te worden die in CI draait, niet een script dat iemand handmatig aanroept, zodat de volgende schemawijziging er automatisch tegenaan loopt.

### Laag 2: echte upgrade op de server-sandbox, met een steekproef

Er is geen ruimte om alle projecten uit te rollen, dus dit is bewust een steekproef: **wies, regelrecht, moza en amt**. Genoeg variatie in diensten om de interessante paden te raken, klein genoeg om de doorlooptijd te zien.

De volgorde is één keer wisselen, niet heen en weer per project:

1. Sandbox op de server, OPI op het image dat productie nu draait (vastgepind, niet `latest`).
2. De vier projecten omzetten en inrichten. Alles laten provisioneren tot alle apps healthy zijn.
3. De gegenereerde staat vastleggen: de zad-deployments-repo op dat moment is het ijkpunt.
4. Het OPI-image wisselen naar de nieuwe build. Eén keer.
5. Elk project heropenen en verversen, zodat alles opnieuw gegenereerd wordt.
6. Vergelijken.

Dat oude OPI een nieuw schema niet kan lezen klopt, maar dat bijt alleen als je teruggaat, en teruggaan hoeft alleen bij afwisselen. Wil je de test herhalen, dan zet je de omgeving schoon opnieuw op; dat is toch wat je wilt voor een eerlijke tweede meting.

## 4. Hoe "klopt het nog" mechanisch wordt

Dit is het scharnierpunt van het hele plan. Zonder dit zit iemand schermen te vergelijken en mist hij precies het stille geval.

De maatstaf is de zad-deployments-repo. Die bevat alles wat OPI voor een project genereert: manifests, secrets, configmaps, RBAC, netwerkbeleid. Leg na stap 3 het commitpunt vast, en doe na stap 5 een `git diff` tegen dat punt. Elke verdwenen omgevingsvariabele, secret-sleutel, ingress, mount of schema verschijnt als een verwijderde regel. Daarmee is "raakt iemand iets kwijt" een leesbare diff.

Wat een diff niet ziet, en waar de live-omgeving juist voor is: databaserechten en `search_path`, of Keycloak-realms, clients en rollen nog kloppen, of buckets en hun beleid er nog zijn, en of ArgoCD alles gesynchroniseerd en healthy krijgt. Daar is de e2e-allservices-probe voor: die bindt elke dienst en meldt per dienst op `/status` of hij hem echt heen en weer krijgt.

Een verschil is niet per se fout. Deze release verandert bewust dingen, bijvoorbeeld de eenmalige migratie naar v2.6. De uitkomst van deze test is dus geen groen vinkje maar een beoordeelde diff: elk verschil is verklaard en gewenst, of het is een bug.

## 5. Wat er gebouwd moet worden

1. **Laag-1-test** die alle productiebestanden migreert en valideert, met een rapport per bestand en een duidelijke uitkomst per klasse fout.
2. **Uitbreiding van het omzetscript** met twee dingen die het nu niet doet: het image van elk component vervangen door `e2e-allservices`, en de poort meeverhuizen. De probe luistert op 8080 terwijl productiecomponenten eigen poorten declareren, dus zonder die herschrijving falen de gezondheidscontroles. Overweeg dit als aparte vlag zodat het omzetscript ook zonder die vervanging bruikbaar blijft.
3. **Een taakje dat de vergelijking doet**: ijkpunt vastleggen, na de upgrade diffen, en de diff samenvatten naar verdwenen sleutels per project.
4. **Een draaiboek** in `features/` zodat de test herhaalbaar is bij de volgende release, en niet eenmalig werk blijft.

## 6. Open beslissingen

1. **Draait laag 1 op de echte productiebestanden of op een kopie?** Een sleutel is er niet voor nodig (zie 2a), dus dat is de blokkade niet. De vraag is waar de bestanden vandaan komen: rechtstreeks uit de projecten-repo, die alleen-gelezen mag worden en waar niets naartoe gecommit mag worden, of uit een vastgelegde momentopname in de testdata. Het eerste is altijd actueel maar maakt de test afhankelijk van een checkout die op een bouwmachine niet staat; het tweede draait overal maar veroudert. Een middenweg is de test overslaan met een duidelijke melding als de map ontbreekt, zodat hij lokaal en op de server met projecten-checkout wél bijt.
2. **Wat is de uitkomst bij een verschil?** Faalt de test, of produceert hij een rapport dat iemand beoordeelt? Voor laag 1 lijkt falen juist; voor de diff van laag 2 is beoordelen realistischer, want gewenste verschillen bestaan.
3. **Nemen we de projecteigen images mee of vervangen we alles door de probe?** Alles vervangen is sneller en betrouwbaarder, maar test niet of echte werklasten nog starten. Voorstel is vervangen, want het doel is het projectbestand en de dienstverlening, niet de applicaties van gebruikers.
4. **Hoe komt de oude OPI erin?** Een vastgepind image van wat productie draait. Te bepalen of dat de CalVer-tag uit de odcn-overlay is of de commit waar productie nu op staat.
5. **Wat doen we met de vier projecten na afloop?** Laten staan voor een volgende ronde, of opruimen zodat de sandbox leeg blijft. Opruimen via `sandbox_project_tool.py delete` doorloopt de echte teardown en test dus meteen het verwijderpad.
6. **Hoort de invite-service erbij?** Vier projecten in productie gebruiken invites en die staan nog als top-level `invites:` in het bestand. Geen van de vier steekproefprojecten is er een, dus overweeg er één te ruilen of een vijfde toe te voegen.

## 7. Buiten scope

Niet in deze test: de projecteigen applicaties draaiend krijgen, prestatiemetingen, en de productieomzetting zelf. Dit plan levert het bewijs dat de release veilig is voor bestaande bestanden; het uitrollen is een aparte beslissing.
