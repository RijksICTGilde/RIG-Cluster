# Vervolg op het sessieherstel van de authorization wall

**Status**: plan. Niets uit dit document is gebouwd.
**Aanleiding**: commit `e51b5ad25` op `claude/authwall-error-page`.
**Eindproduct van de implementatie**: `features/authorization-wall.md` bijgewerkt volgens de projectconventie.

Doelgroep: de sessie die dit bouwt. De namen van nieuwe schemaversies en vlagwaarden in dit document zijn **voorstellen** en als zodanig gemarkeerd; ze zijn nog niet vastgesteld.

---

## 1. Waar het staat

Commit `e51b5ad25`, bijgesteld op 5 oktober, pakt het probleem uit de logs aan: paden onder `/api/` en statische bestanden krijgen een kale 401 in plaats van een 403 met de inlogpagina als HTML, en de cookie-instellingen staan uitgesproken in plaats van op de default. Het tussenscherm van de muur is daarbij ongemoeid gelaten (`--skip-provider-button` blijft `false`), omdat dat een andere afweging is en de banner van vijf projecten eraan hangt. Gebouwd, getest en vastgelegd in het golden manifest.

Niet gedaan: gepusht, gemerged, uitgerold. De branch heeft geen upstream. Ook de foutpagina uit `5747a3133` staat nog niet in productie; de draaiende pod logt bij het starten nog `Could not load file /etc/oauth2-proxy/templates/error.html`.

Het bewijs waarop dit alles rust staat in de productielogs van `democonsole-test-mpfm-w3h` (namespace `rig-prd-mpfm-w3h`, container `authorization-wall`), venster 30-09 09:52 tot 01-10 07:31 UTC: 163 keer een 403, elk met precies een bijbehorende regel `No valid authentication in request`, en 32 van de 47 geauthenticeerde page loads verloren hun stylesheet en scripts.

## 2. Wat dit plan niet doet

Uitrol. Dat gaat via git naar ArgoCD en is geen taak voor een agent-team. Zie punt 7 voor de volgorde waarin dat hoort te gebeuren.

## 3. Wat gemeten is, en wat nog open staat

**M1 is gemeten op 5 oktober 2026 en het antwoord was niet wat dit plan aannam.** De projects-repo staat lokaal in `rig-cluster-test-git-repositories/rig-cluster-projects-github/`. Zeven projecten zetten `banner`, vijf met echte tekst: hwmaw-ovh, jongo-lh2, dp-bn7, fp-unj ("Toegang op uitnodiging") en no-ks4 ("Dit is invite only."); dsm1j2-2ws en toets-hn7 hebben hem leeg. Bij fp-unj en no-ks4 vertelt die tekst iemand juist waarom hij geweigerd kan worden.

Daardoor is de oorspronkelijke taak A vervallen: `--skip-provider-button` staat terug op `false`, de inlogkaart blijft, en de banner werkt gewoon. Wat eromheen is uitgezocht, inclusief de schemapoort, is verhuisd naar `features/futures/authorization-wall-rolcontrole-in-de-proxy.md`, want het hoort bij de vraag of dat tussenscherm weg moet en niet bij deze branch.

Tegelijk gemeten: 11 projecten hebben een wall, en 5 daarvan draaien zonder rolpoort (`mpfb-8wh` en `mpfm-w3h` op `enabled: false`, `dsm1j2-2ws`, `jongo-lh2` en `tvas-7pb` zonder blok). Een wall zonder rol is dus normaal en niet de uitzondering.

**M2 is gemeten in RC-240 (PR #213), en het antwoord weerlegt de hypothese achter taak B.** De meting hoefde niet in productie met devtools: ze staat herhaalbaar in `tests/integration/test_authorization_wall_proxy.py`, tegen een echte Keycloak en een echte oauth2-proxy (zie `features/authorization-wall-meetopstelling.md`). Splitsen gebeurt, al bij 80 realmrollen. Maar een gesplitste sessie werkt, dus de splitsing verklaart de halve pagina's niet. Wat ze wel doet is elk cookiedeel een enkelvoudig faalpunt maken: raakt er onderweg een deel kwijt, dan is de hele sessie weg, en dat levert exact het beeld uit de productielogs op.

## 4. Wat deze branch oplost, en wat niet

De branch is bewust versmald tot het probleem uit de productielogs: half ingeladen pagina's, verkeerde content-types en browsers die er niets van maken. Daarvoor zitten er twee dingen in.

`--cookie-refresh=3m` laat de sessie meeschuiven: zonder die vlag vernieuwt oauth2-proxy helemaal niet (`refresh:disabled` in zijn eigen log) en valt de hele paginalading in een keer om op de grens van `--cookie-expire`. De verklaring die hier eerder stond, een kudde die bij de vijfminutenklif tegelijk op een dode sessie aankomt, is in RC-240 nagemeten en weerlegd; wat er wel gebeurt staat in `features/authorization-wall.md`. `--api-route` voor `/api/` en voor statische bestanden maakt van het antwoord een kale 401 in plaats van een 403 met de inlogpagina als HTML, zodat een mislukte stylesheet als mislukte stylesheet aankomt en niet als tekst die de browser probeert te parsen. Daarbij `--cookie-expire=10h` om het cookie niet langer geldig te laten beweren dan de Keycloak-sessie, `--cookie-samesite=lax` expliciet, en `--cookie-csrf-per-request=true` tegen twee tabbladen die elkaars inlogpoging overschrijven.

Niet in deze branch, en bewust: het tussenscherm van de muur en de rolcontrole in de proxy. Beide staan in de futures-doc.

Let op bij het verifiëren: `--cookie-refresh` dekt de klif, maar niet het geval waarvan bewijs is dat het negen seconden na een verse login optreedt. M2 is inmiddels gemeten en heeft dat geval niet verklaard, dus het blijft staan: deze branch neemt niet aantoonbaar het hele probleem weg. Dat eerlijk houden in de PR-tekst hoort erbij.

## 5. Taak B: subresources die zonder cookie aankomen

Het restraadsel uit de analyse, en het enige deel van het oorspronkelijke probleem dat `e51b5ad25` niet verklaart.

Vastgesteld: binnen één geauthenticeerde page load haalt het document de sessie wel en halen `/bediening.css`, `/bediening.js` en `/inlogmuur.js` die niet, soms negen seconden na een verse login en dus ruim binnen de vijf minuten dat de sessie geldig is. De API-polls in dezelfde seconde slagen alle 937. oauth2-proxy logt geen enkele `Error loading cookied session`, dus het cookie wordt op die aanvragen niet meegestuurd en komt niet kapot aan. Uitgesloten met bewijs: browsercache (`Cache-Control: no-store` op zowel HTML als CSS), een kapotte router-pod (alle zes router-IP's leveren zowel 200 als 403), `crossorigin` op de tags (de HTML gebruikt kale relatieve verwijzingen) en een domeinfilter (`--email-domain=*`).

Hypothese: het sessiecookie wordt gesplitst in `_oauth2_proxy_0`, `_1`, `_2` omdat de hele tokenset (id_token, access_token, refresh_token) erin zit, en bij een gebruiker met veel Keycloak-claims past dat niet in één cookie. Dat `robbert.uittenbroek` nooit omvalt en `robbert.bos` en `kees.keulemans` wel, past daarbij.

**Weerlegd door M2.** Splitsen gebeurt, maar een gesplitste sessie werkt, dus de oorzaak van dit restraadsel is nog onbekend en geen van de richtingen hieronder neemt de halve pagina's aantoonbaar weg. Ze houden wel het cookie klein, en dat heeft zelfstandig nut: met een cookie valt er niets gedeeltelijk te verliezen.

Mogelijke richtingen, in oplopende zwaarte, alle drie **voorstellen**:

- `--session-cookie-minimal=true`: laat de tokens uit het cookie weg en houdt alleen wat de muur nodig heeft. Kleinste ingreep. Gevolg: zonder refresh token in het cookie kan de proxy niet meer stil vernieuwen, wat tegen de eis uit `e51b5ad25` in gaat. Eerst uitzoeken, niet aannemen.
- `--session-store-type=redis`: de sessie naar een Redis buiten het cookie, zodat het cookie alleen een verwijzing is. Vraagt een Redis per project of een gedeelde, en dat is een infrastructuurbeslissing met eigen gevolgen (beschikbaarheid, netpol, backup). De dienst `namespace_redis` bestaat al in de catalogus en is het logische startpunt.
- De claims in het Keycloak-token terugbrengen, zodat het cookie weer in één past. Raakt het realm en niet de muur, en kan andere consumenten van dat token treffen.

## 6. Taak C: een basis waar orch op kan bouwen

`orch` bouwt op `forgejo/main`, en `e51b5ad25` staat op een lokale branch zonder upstream. De lokale checkout loopt achter op `forgejo/main`, dus code daar ook tegen meten en niet tegen de lokale `main`. Begin met `dclaude sync-main`.

Let op twee dingen die eerder zijn beten: `orch stop` zonder `--delete-branch` hergebruikt een oude basis, en `orch ship` escaleert sinds dashboard `8e534c0`, waarvoor `orch add --base-commit` de omweg is. Volg bij het opvolgen van een draaiende taak `step` en niet `Status`: een approve tijdens `step=reviewing` slaat de review over. Mergen gaat met `orch merge TASK-ID`, en toets eerst met `git merge-tree` of het conflictvrij is, want `main` loopt door.

## 7. Taak D: de branch opsplitsen

`claude/authwall-error-page` draagt acht commits waarvan vier niets met de muur te maken hebben: `d4d9969d7` (registry-quotum), `e8d059551` en `26794cadd` (de negen uitgeschakelde componenten en het runbook) en `88112c326` (database-image langs de clusterrewrite). Die horen niet in een PR over de authorization wall.

De vier die er wel bij horen: `5747a3133` (foutpagina), `aedff3d5d`, `bd80ffb83` en `e74731c59` (de onbevestigde SSO-gebruiker en de 500) en `e51b5ad25` (dit sessieherstel).

## 8. Volgorde

1. **Taak C**: basis op `forgejo/main` zetten.
2. **Taak D**: branch splitsen, zodat er een PR over alleen de muur overblijft.
3. Uitrollen via git naar ArgoCD, in deze volgorde: eerst `5747a3133` (de foutpagina, die nu nog ontbreekt in de container), dan `e51b5ad25`. Verifieer na uitrol in de logs van `rig-prd-mpfm-w3h` dat de startregel `Could not load file error.html` weg is en dat er geen 403 meer op `/bediening.css` staat.

## 9. Verificatie van het geheel

Het eindproduct is pas af als dit alle vier waar is:

- Geen 403 meer op een stylesheet, script of favicon in de logs van `rig-prd-mpfm-w3h` over een venster van minstens een uur met echte gebruikers.
- Geen volledige herlogins meer waar een stille refresh had gekund: de `AuthSuccess`-regels horen zeldzaam te worden, want een stille refresh logt niets.
- `features/authorization-wall.md` beschrijft de eindtoestand.

Wat hier NIET bij hoort als maatstaf: dat `inlogmuur.js` uit `fbs-demo-console` kan verdwijnen. Dat script vangt het tussenscherm op, en dat tussenscherm blijft in deze branch bestaan. Zolang de muur een 403 met een inlogkaart antwoordt op een navigatie, houdt dat script zin. Het hoort bij de futures-doc, niet bij deze verificatie.

**M2** is in RC-240 gemeten en weerlegde de hypothese van taak B, dus taak B volgt daar niet uit. Zie paragraaf 3.
