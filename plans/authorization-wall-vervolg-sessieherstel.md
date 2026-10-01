# Vervolg op het sessieherstel van de authorization wall

**Status**: plan. Niets uit dit document is gebouwd.
**Aanleiding**: commit `e51b5ad25` op `claude/authwall-error-page`.
**Eindproduct van de implementatie**: `features/authorization-wall.md` bijgewerkt volgens de projectconventie.

Doelgroep: de sessie die dit bouwt. De namen van nieuwe schemaversies en vlagwaarden in dit document zijn **voorstellen** en als zodanig gemarkeerd; ze zijn nog niet vastgesteld.

---

## 1. Waar het staat

Commit `e51b5ad25` zet de muur zo neer dat hij een verlopen sessie zelf herstelt: een navigatie zonder sessie gaat rechtstreeks naar Keycloak, paden onder `/api/` en statische bestanden krijgen een 401, en de cookie-instellingen staan uitgesproken in plaats van op de default. Dat is gebouwd, getest (384 tests groen, ruff en pyright schoon) en vastgelegd in het golden manifest.

Niet gedaan: gepusht, gemerged, uitgerold. De branch heeft geen upstream. Ook de foutpagina uit `5747a3133` staat nog niet in productie; de draaiende pod logt bij het starten nog `Could not load file /etc/oauth2-proxy/templates/error.html`.

Het bewijs waarop dit alles rust staat in de productielogs van `democonsole-test-mpfm-w3h` (namespace `rig-prd-mpfm-w3h`, container `authorization-wall`), venster 30-09 09:52 tot 01-10 07:31 UTC: 163 keer een 403, elk met precies een bijbehorende regel `No valid authentication in request`, en 32 van de 47 geauthenticeerde page loads verloren hun stylesheet en scripts.

## 2. Wat dit plan niet doet

Uitrol. Dat gaat via git naar ArgoCD en is geen taak voor een agent-team. Zie punt 7 voor de volgorde waarin dat hoort te gebeuren.

## 3. Blokkades: twee metingen en een beslissing

Deze drie staan vóór alle bouwtaken. Taak A en B zijn zonder hen niet uitvoerbaar, en een team dat ze overslaat bouwt het verkeerde.

**M1. Gebruikt een productieproject het veld `banner`?**
Nodig voor taak A. Geen van de voorbeeldprojecten in `projects/` gebruikt het, en `mpfm-w3h` niet (geen `--banner` in de draaiende args). De projects-repo staat niet lokaal. Te meten langs twee wegen: de `zad-projects` git-repo doorzoeken op `banner:` onder een `authorization-wall`-blok, of clusterbreed de deployments met een `authorization-wall`-container nalopen op een `--banner`-arg. Bij de tweede weg geldt de les uit `reference_gegenereerde_manifests_machine_eigendom`: de git-repo is de waarheid, het cluster het resultaat. Doe dus bij voorkeur beide. Let op: bij het schrijven van dit plan weigerde de cluster-API verbindingen (`connection refused` op `147.181.48.65:443`), dus de clusterweg was niet beschikbaar.

**M2. Komt het sessiecookie gesplitst aan?**
Nodig voor taak B. Bij een getroffen gebruiker (`robbert.bos@rijksoverheid.nl` of `kees.keulemans@minbzk.nl`) in devtools, Network, de pagina herladen en op `/bediening.css` kijken: staat er een `Cookie`-header, hoe groot is hij, en staan er `_oauth2_proxy_0`, `_oauth2_proxy_1` en verder in. `robbert.uittenbroek@rijksoverheid.nl` had het probleem niet en is de controlemeting.

**D1. Wat gebeurt er met het veld `banner`?** Beslissing voor de opdrachtgever, niet voor het bouwende team. Zie taak A voor de opties en hun kosten.

## 4. Taak A: het veld `banner` afhandelen

Het veld is sinds `e51b5ad25` inert: `--banner` rendert uitsluitend op `sign_in.html`, en die pagina wordt niet meer getoond. De vlag gaat nog wel mee naar de sidecar zodat de plumbing intact bleef. Dat is een bewust uitstel, geen eindtoestand.

### De gevolgen die de keuze bepalen

Dit is geen veld dat je even weghaalt. Het is het **enige** configveld van deze dienst, en twee methodes leiden zich eruit af:

- `config_api_fields(PROJECT)` geeft `config_model_field_names()` terug, dus met een leeg model een lege lijst.
- `config_editables(PROJECT)` geeft `AUTH_WALL_BANNER_EDITABLE` terug, dus met een leeg model niets.
- `config_section_id = "auth-wall-config"` en `modal_flow_id` leveren dan een lege kaart op.
- `AUTH_WALL_COMPONENT_INFO_EDITABLE` is een wegwijzer die de gebruiker vanaf het componentformulier naar "tabblad Services, kaart Authorization Wall" stuurt om de banner te zetten. Die wijst na verwijdering naar een lege kaart.
- De comment bij `allows_implicit_project_selection = True` beredeneert die stand expliciet uit het feit dat het projectniveau alleen een optionele bannertekst draagt. Zonder veld klopt de conclusie nog, maar de onderbouwing moet mee.

### De poort die dit tot een slot maakt

`AuthorizationWallConfig` heeft `extra="forbid"` en het schemafragment `additionalProperties: false`. `project_validation.py` faalt dicht: een onbekende sleutel geeft `ProjectIntegrityError` met `extra_forbidden`. **Een projectbestand dat `banner:` nog bevat wordt dus onleesbaar zodra het veld uit het model verdwijnt**, en dat raakt niet alleen nieuwe verwerkingen maar elke reconcile van zo'n project.

Verwijderen kan daarom niet zonder migratie. De plek bestaat al: `migrate_config(config, from_version)` in `catalog/base.py` past geordende stappen toe van `from_version` naar de huidige. Dus:

1. `config_schema_version` omhoog (**voorstel**: `"1.1"`).
2. Een migratiestap `1.0 -> 1.1` die de sleutel `banner` uit het config-blok verwijdert.
3. Een nieuw schemafragment `authorization-wall.v1.1.json`. `tests/test_service_config_schema.py` is een drift lock en faalt in CI als het model wijzigt zonder regeneratie, dus die regeneratie is verplicht en niet optioneel.

Verifieer met een test die een projectblok op `from_version: "1.0"` met een gezette `banner` door de validatie haalt en groen krijgt. Zonder die test is de migratie niet aantoonbaar.

### De opties

**A1. Veld laten vervallen.** Raakt `config_model.py`, `authorization-wall.v1.0.json` (nieuw fragment), `editables.py`, `__init__.py` (de comment en mogelijk de wegwijzer), `help.md`, `features/authorization-wall.md`, en de tests `test_authwall_banner_yaml_injection.py` (het onderwerp van die test verdwijnt), de bannertest in `test_manifests.py` en de drift lock. Plus de migratie hierboven. Grootste diff, schoonste eindtoestand.

**A2. Tekst naar het Keycloak-inlogthema.** Het inlogscherm komt nu toch van Keycloak, dat draait het `nl-design-system`-thema. De banner hoort dan bij het realm, niet bij de muur. Vraagt uitzoeken of dat per realm instelbaar is zonder themawijziging per project, wat niet vanzelf spreekt. Zelfde verwijdering aan de ZAD-kant als A1, plus werk aan de Keycloak-kant.

**A3. Veld herbestemmen naar de foutpagina.** `error.html` bestaat sinds `5747a3133` en gebruikt `.SignInMessage` niet. De banner zou daar context kunnen geven bij een 403 ("dit account mag deze omgeving niet openen"). Kleinste diff, maar de betekenis van het veld verschuift van "tekst vóór het inloggen" naar "tekst bij een fout", en dat is iets anders dan wat de gebruiker heeft ingevuld. Alleen zinnig als M1 uitwijst dat niemand het veld gebruikt, want dan verandert er voor niemand iets.

**A4. Niets doen.** De vlag blijft inert meegaan. Goedkoopst vandaag, maar het laat een veld in het formulier staan dat niets doet, en dat is precies het soort stille leugen dat later een incident wordt.

Aanbeveling: wacht op M1. Wijst die uit dat niemand het veld gebruikt, dan is A1 de juiste en is de migratie een formaliteit in plaats van een risico.

## 5. Taak B: subresources die zonder cookie aankomen

Het restraadsel uit de analyse, en het enige deel van het oorspronkelijke probleem dat `e51b5ad25` niet verklaart.

Vastgesteld: binnen één geauthenticeerde page load haalt het document de sessie wel en halen `/bediening.css`, `/bediening.js` en `/inlogmuur.js` die niet, soms negen seconden na een verse login en dus ruim binnen de vijf minuten dat de sessie geldig is. De API-polls in dezelfde seconde slagen alle 937. oauth2-proxy logt geen enkele `Error loading cookied session`, dus het cookie wordt op die aanvragen niet meegestuurd en komt niet kapot aan. Uitgesloten met bewijs: browsercache (`Cache-Control: no-store` op zowel HTML als CSS), een kapotte router-pod (alle zes router-IP's leveren zowel 200 als 403), `crossorigin` op de tags (de HTML gebruikt kale relatieve verwijzingen) en een domeinfilter (`--email-domain=*`).

Hypothese: het sessiecookie wordt gesplitst in `_oauth2_proxy_0`, `_1`, `_2` omdat de hele tokenset (id_token, access_token, refresh_token) erin zit, en bij een gebruiker met veel Keycloak-claims past dat niet in één cookie. Dat `robbert.uittenbroek` nooit omvalt en `robbert.bos` en `kees.keulemans` wel, past daarbij.

**Niet bouwen voordat M2 er is.** Als de meting iets anders uitwijst, is elke fix hieronder de verkeerde.

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

1. **M1** en **M2** meten. Geen bouwtaak hangt zonder deze twee aan iets vast.
2. **D1** beslissen op grond van M1.
3. **Taak C**: basis op `forgejo/main` zetten.
4. **Taak D**: branch splitsen, zodat er een PR over alleen de muur overblijft.
5. **Taak A** bouwen volgens de keuze uit D1, met de schemamigratie als harde poort.
6. **Taak B** bouwen, maar alleen als M2 de hypothese bevestigt.
7. Uitrollen via git naar ArgoCD, in deze volgorde: eerst `5747a3133` (de foutpagina, die nu nog ontbreekt in de container), dan `e51b5ad25`. Verifieer na uitrol in de logs van `rig-prd-mpfm-w3h` dat de startregel `Could not load file error.html` weg is en dat er geen 403 meer op `/bediening.css` staat.

## 9. Verificatie van het geheel

Het eindproduct is pas af als dit alle vier waar is:

- Geen 403 meer op een stylesheet, script of favicon in de logs van `rig-prd-mpfm-w3h` over een venster van minstens een uur met echte gebruikers.
- Geen volledige herlogins meer rond de vijfminutengrens: de `AuthSuccess`-regels horen zeldzaam te worden, want een stille refresh logt niets.
- `inlogmuur.js` kan uit `fbs-demo-console` verdwijnen zonder dat de pagina stukgaat. Dat is de echte toets: dat script bestond als workaround voor ons gedrag, en zolang het nodig blijft is dit niet opgelost. Afstemmen met de eigenaar van die applicatie.
- `features/authorization-wall.md` beschrijft de eindtoestand, inclusief wat er met `banner` is gebeurd.
