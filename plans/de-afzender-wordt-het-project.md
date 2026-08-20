# De afzender wordt het project: From en envelope op noreply-rijksapp+project, met de naam uit de projectconfiguratie

De afspraak met het mailteam om hun mailserver te mogen gebruiken is dat een bericht herkenbaar van een project komt: `Vrije Naam <noreply-rijksapp+ai1-uit@rijksoverheid.nl>`. De relay doet nu iets anders. Elk project verstuurt vanaf hetzelfde kale adres, en de weergavenaam is die van de applicatie in plaats van die van het project. Deze taak legt de hele `From:` in handen van het platform.

## Wat er nu gebeurt, gemeten op 20 augustus 2026 op de sandbox

Een testmail van `ai1-uit` kwam zo aan in de Mailpit-sink:

```
From          e2e-allservices <noreply-rijksapp@rijksoverheid.nl>
To            <robbert.uittenbroek@rijksoverheid.nl>
Return-Path   <noreply-rijksapp+project-ai1-uit@rijksoverheid.nl>
```

Drie dingen kloppen daar niet aan de afspraak:

1. Het adres in de `From:` draagt het project niet. Het is voor elk project hetzelfde.
2. De weergavenaam is die van de applicatie: `images/e2e-allservices/mail.go:61` zet letterlijk `"From: e2e-allservices <" + from + ">"`, en sieveregel 2 in `mail/controller/base/configmap.yaml` laat die naam expres staan.
3. Het projectbestand van `ai1-uit` zegt `from-name: Robbert Uittenbroek`, en die waarde wordt door niemand gelezen. Er is geen `SMTP_FROM_NAME`, het relay-account draagt de naam niet, en er is geen enkele andere lezer. Ik heb ernaar gezocht in `opi/`, `manifests/` en de images.

De envelope draagt het project wel, maar via de accountnaam (`project-ai1-uit`) in plaats van de projectnaam.

## De nieuwe afspraak

```
From:         <from-name uit de projectconfiguratie> <noreply-rijksapp+ai1-uit@rijksoverheid.nl>
Return-Path:  <noreply-rijksapp+ai1-uit@rijksoverheid.nl>
```

- Het plusdeel is de PROJECTNAAM (`ai1-uit`), niet de accountnaam (`project-ai1-uit`).
- De weergavenaam komt uit `services/[send-email]/config/from-name`. Wat de applicatie in haar eigen `From:` zet, wordt weggegooid, naam en adres allebei.
- `Reply-To:` blijft van de applicatie en wordt niet aangeraakt. Dat is de scheiding: de `From:` is identiteit en ligt vast omdat wij op andermans mailserver zitten, de `Reply-To:` is alleen waar een antwoord heen moet.
- Envelope en `From:` worden hetzelfde adres. Nu verschillen ze een voorvoegsel, en dat verschil dient niets: SPF-uitlijning kijkt naar het domein, en dat verandert niet.

Dit VERVANGT de afspraak van 18 augustus ("een vast adres voor alle projecten, project alleen in het plusdeel van de envelope"), die op vier plekken staat opgeschreven. Die teksten moeten mee, anders staat de reden van gisteren morgen nog als regel in de documentatie.

## De ene onbekende: hoe weet de relay de From van een account?

Het sieve-script kent alleen `${env.authenticated_as}`, en dat is de ACCOUNTNAAM. Daar volgt de projectnaam niet uit zonder het voorvoegsel weg te knippen, en de weergavenaam volgt er helemaal niet uit. De relay heeft dus per account een waarde nodig die hij nu niet heeft.

**Meet dit eerst, want de rest van het ontwerp hangt eraan.** De relay draait op de sandbox (`rig-ron`, Stalwart Community v0.11.8) en de management-API is van binnenuit bereikbaar. Drie kandidaten, in de volgorde waarin ik ze zou proberen:

1. **De lookup-store.** `storage.lookup = "db"` staat al aan en de ratelimiter sleutelt al op `authenticated_as`. Vraag: kan een sieve-script een sleutel lezen (`key_get` of de query-uitbreiding), en kan OPI zo'n sleutel schrijven via de management-API? Als allebei ja, is dit het antwoord: OPI schrijft één regel per account met de volledige `From:`, het script leest hem, en er is één schrijver.
2. **Het `description`-veld van de principal.** OPI zet dat al (`connectors/mail.py`, "ZAD send-email account voor ..."). Vraag: kan sieve de principal van de geauthenticeerde afzender uitlezen?
3. **Afleiden plus een gegenereerde tabel.** Adres uit `authenticated_as` met het voorvoegsel eraf, naam uit een tabel die OPI in de relay-configuratie schrijft. Dit is de uitweg als 1 en 2 niet kunnen, en de minst mooie: het koppelt GitOps-configuratie aan projectdata en vraagt een herlaadmoment.

Schrijf in de PR wat je gemeten hebt en welke weg het is geworden, ook als het de eerste was. Dit is het soort vraag dat over een half jaar terugkomt.

## Wat er moet gebeuren

1. **Het adres per project.** `MailManager._addresses()` (`opi/manager/mail_manager.py:447`) rekent nu één vast adres uit en zegt in zijn docstring dat het voor elk project gelijk is en niet instelbaar. Dat wordt `noreply-rijksapp+<projectnaam>@rijksoverheid.nl`, voor zowel de `From:` als de envelope. `get_mail_from_address()` en `mail_from_address` in `cluster_config.py` houden het BASISadres (het kale, zonder plusdeel); het samenstellen hoort op één plek te gebeuren en dat is de manager.
2. **De relay zet de From vast.** Sieveregel 2 in `infrastructure/bootstrap/infrastructure/mail/controller/base/configmap.yaml` verwijdert de `From:` en zet de waarde van dit account neer, naam en adres samen. Het onderscheid tussen "met naam" en "kaal" verdwijnt, want er wordt niets meer uit de aangeleverde header overgenomen. Kijk meteen of regel 2a (precies een adres in de `From:`, anders 550) nog een taak heeft: die bestaat om de overschrijving eenduidig te maken zolang de weergavenaam behouden bleef, en dat is straks niet meer zo. Valt hij weg, schrijf dan op waarom, in dezelfde geest als de regel die er nu staat.
3. **De envelope volgt.** De herschrijving in `[session.mail]` bouwt het adres nu uit `authenticated_as`. Die moet hetzelfde adres opleveren als de `From:`, uit dezelfde bron.
4. **De terugval is het kale adres.** Levert de opzoeking niets (een account zonder regel, een relay-database die net leeg is), dan verstuurt de relay als `noreply-rijksapp@rijksoverheid.nl` zonder naam. Dat is een bewuste keuze: de post gaat weg, het domein klopt en geen project kan zich als een ander voordoen. Dichtklappen met een 550 zou het versturen platleggen door een configuratiehik. Log het wel op WARNING, want het is een toestand die niet hoort te bestaan.
5. **`from-name` krijgt validatie.** Het veld gaat straks rechtstreeks een mailheader in en heeft vandaag geen enkele controle (`opi/services/catalog/send_email/editables.py:24`, alleen `remove_when_none`). Nodig: geen regeleindes of andere stuurtekens (header-injectie), geen `@` en geen punthaken of aanhalingstekens (anders leest "beveiliging@bank.nl" als naam bij menig ontvanger als het afzenderadres), en een lengtegrens; 64 tekens lijkt me ruim zat. Dat laatste sluit het gat dat het configcommentaar zelf al benoemt en tot nu toe afdekt met "elk project komt langs een goedkeuring". Zet er een test op met een naam met een `\r\n` erin en een naam die op een adres lijkt.
6. **Wat de applicatie ziet.** `SMTP_FROM` blijft bestaan en wordt het projectadres, zodat een ontwikkelaar kan zien wat de ontvanger krijgt. Er komt GEEN `SMTP_FROM_NAME`: de applicatie hoeft de naam niet te weten, want ze kan er niets aan veranderen.
7. **De toets en de teksten.** `scripts/mail_identity_check.py` pint het vaste adres (`VAST_ADRES`, regel 41 en 152); die wordt de toets op de nieuwe vorm, inclusief het geval "applicatie zet een eigen naam en die wordt genegeerd". En de vier documentatieplekken die de oude afspraak beschrijven: `plans/mailrelay.md:47` en `:49`, `features/send-email.md:86` en verder, `opi/services/catalog/send_email/help.md:26`, en de mailparagraaf in `TODO_NEXT_RELEASE.md`.

## Valkuilen

**Het platformaccount is geen project.** OPI heeft zelf een account op de relay (`settings.MAIL_PLATFORM_ACCOUNT`, `mail_manager.py:247`). Beslis expliciet wat dat account als afzender krijgt en schrijf het op. Het kale adres zonder plusdeel ligt voor de hand, want er is geen project om naar te wijzen, en dan valt het samen met de terugval uit punt 4.

**De lengte van het lokale deel.** `noreply-rijksapp+` is zeventien tekens en een lokaal deel mag er vierenzestig. Een projectnaam van meer dan zevenenveertig tekens loopt daaroverheen. `generate_mail_account_name` kapt de accountnaam al af op 63; doe hier hetzelfde soort ding en leg het in een test vast, anders ontstaat er een adres dat de upstream weigert op een lengte die niemand heeft nagerekend.

**De naam kan leeg zijn.** `from-name` is optioneel (`remove_when_none=True`), en een project zonder naam hoort gewoon `<noreply-rijksapp+project@rijksoverheid.nl>` te versturen. Dat is een geldige uitkomst en geen terugval.

**Bijwerken bij wijziging.** Verandert een project zijn `from-name`, dan moet de relay dat merken. De configuratiestap heeft al `post_save_action="process_project"`, dus de weg bestaat; zorg dat het synchroniseren van het account die waarde meeneemt en dat het herhaalbaar is (dezelfde waarde nog eens wegschrijven verandert niets).

**Niets van dit alles is uitgerold.** De relay staat op de sandbox en niet op productie, dus er zit geen bestaande post op het oude adres vast. Dat maakt dit het goedkope moment; het maakt het niet ongetoetst.

## Wat hier buiten valt

- Een filter op wat een project als naam kiest voorbij de validatie uit punt 5. De goedkeuring blijft de plek waar een beheerder een rare naam tegenhoudt.
- De upstream-koppeling en de netwerkregels. Die staan en veranderen niet.
- De e2e-image aanpassen. Die mag haar eigen `From:` blijven zetten; het hele punt is dat het niets meer uitmaakt.

## Verifieerbaar

- Een testmail van `ai1-uit` komt in de Mailpit-sink aan als `Robbert Uittenbroek <noreply-rijksapp+ai1-uit@rijksoverheid.nl>`, met dezelfde waarde als Return-Path, terwijl `images/e2e-allservices/mail.go` onveranderd `e2e-allservices` als naam meestuurt. Zet de kopregels uit de sink in de PR.
- Een `Reply-To:` die de afzender meestuurt, komt ongewijzigd aan.
- `scripts/mail_identity_check.py` toetst dit alles en slaagt op de sandbox; plak de uitvoer in de PR.
- Een account zonder opgezochte waarde verstuurt als het kale `noreply-rijksapp@rijksoverheid.nl` en logt een waarschuwing.
- `from-name` met een `\r\n` erin en `from-name` met een `@` erin worden geweigerd door het formulier en door de API.
- Een project zonder `from-name` verstuurt met een kaal projectadres en zonder naam.
- De vier documentatieplekken beschrijven de nieuwe afspraak, en nergens staat nog dat het afzenderadres voor elk project gelijk is.
- `uv run pytest tests/ -q` groen op de twee voorbestaande roden na (`test_attachment_schema`, `test_template_structure`), plus `ruff check .`, `ruff format .` en `pyright`.


History:
  2026-08-20 18:15:33  created — ship
  2026-08-20 18:16:01  dispatched — Worker session: dclaude-RIG-Cluster-rc145
  2026-08-20 18:56:27  pr_opened — PR #141

---

## Nagekomen bij de uitvoering (20 augustus 2026, RC-145)

Twee dingen liepen anders dan dit plan aannam; het gebouwde volgt de meting.

**De opzoektabel uit "de ene onbekende" bestaat niet in bruikbare vorm.** Alle drie de
kandidaten zijn gemeten en afgevallen: het `description`-veld van de principal is vanuit
sieve niet te lezen (geen enkele expressiefunctie leest een principal), een opzoektabel in
het geheugen wordt maar EEN KEER gebouwd en een reload ververst hem niet (alleen het eerste
project kreeg zijn waarde), en de opzoekopslag die wel live is heeft geen schrijfweg in de
management-API. Wat het geworden is: het ADRES leidt de relay af uit de accountnaam
(`strip_prefix` op `project-`), en de weergavenaam komt uit een klein sieve-script dat OPI
genereert en via de settings-API wegschrijft - een sieve-script wordt bij elke herbouw wel
opnieuw gecompileerd. Daarmee vervalt ook de WARNING uit punt 4: een project waarvan de naam
nog niet is weggeschreven verstuurt met het juiste adres en zonder naam, en dat is een
geldige toestand.

**De relay at zijn eigen dagbudget op.** Buiten dit plan om, maar het blokkeerde elke
meting: de gezondheidsprobes openden elke 5 en 10 seconden een SMTP-sessie en telden mee in
`queue.limiter.inbound.platform` (18 per minuut gemeten, 25.920 per dag tegen een plafond
van 20.000). De probes wijzen nu naar `/healthz` op de managementpoort.
