# Gebeurtenissen vastleggen en melden: de oplossingsrichtingen

## Status en meetbasis

Dit is deel 2 van drie. Deel 1 is `features/futures/gebeurtenissen-inventarisatie.md` en is het feitenmateriaal waar dit stuk op leunt; deel 3 is `features/futures/gebeurtenissen-plan-van-aanpak.md` en kiest. Dit document weegt af en beveelt aan, maar besluit niet: een lezer die het oneens is met een aanbeveling moet de inventaris nog kunnen gebruiken.

Dezelfde meetbasis als deel 1: commit `83ac4b9b` van 21 augustus 2026. Alle namen die hieronder voor tabellen, kolommen, eventtypes, instellingen of endpoints worden gebruikt zijn **VOORSTEL** en als zodanig gemarkeerd. Ze staan er om over te kunnen praten, niet omdat ze zijn besloten.

---

## De begripsbotsing, en het besluit

Het woord *event* betekent in deze codebase vandaag drie dingen.

1. **`ActionEvent` en `UIEvent`** (`opi/services/services_enums.py:149` en `:184`, beschreven in `features/service-event-hooks.md`). Twee families van in-procesuitbreidingspunten waarop een dienst inhaakt: `AFTER_SYNC` en `REDEPLOY` schrijven, `PROJECT_SECTIONS`, `DEPLOYMENT_SECTIONS` en `DEPLOYMENT_STATE` lezen. Dit is een dispatchmechanisme met een eigen, uitgeschreven contract (een actiehandler commit nooit zelf; een UI-handler is synchroon en muteert niets). Het is bewust zo ontworpen en het heeft niets met geschiedenis te maken.
2. **De `events`-kolom op `async_tasks`** (`opi/core/async_task_schema.py`). Kubernetes-events die bij één taak zijn opgehaald met `kubectl get events` (`opi/connectors/kubectl.py:969`) en in JSONB zijn geplakt. Dat zijn andermans events, van één namespace, op één moment.
3. **Wat deze opdracht bedoelt:** er is iets gebeurd op het platform dat het waard is te bewaren en mogelijk te melden.

**Besluit: het derde heet een *gebeurtenis*, in code `Gebeurtenis` en in het Nederlands.** De eerste twee blijven ongemoeid. Er wordt niets hernoemd, geen enum verplaatst, geen kolom omgedoopt.

De reden om niet het omgekeerde te doen, dus het derde "event" noemen en de eerste twee hernoemen, is dat de kosten scheef liggen. Hernoemen van `ActionEvent`/`UIEvent` raakt de hele dienstencatalogus (`opi/services/catalog/`), de registry, de dispatch en `features/service-event-hooks.md`, en levert niets op behalve een woord. De `events`-kolom hernoemen kost een migratie. Een nieuw, ongebruikt woord kiezen kost niets en is bovendien preciezer: "gebeurtenis" zegt in het Nederlands wat het is, en dit is een Nederlandstalig project.

Er is één prijs, en die hoort genoemd: naar buiten toe is *event* het woord dat de standaarden gebruiken. Een CloudEvents-projectie op de rand heet dan een event terwijl hij intern een gebeurtenis is. Dat is een vertaalslag op precies één plek (de exporteur) en dat is een aanvaardbare prijs voor nul aanraking van bestaande code.

**Wat er met de eerste twee gebeurt:** niets, met één toevoeging die in deel 3 als latere fase staat. `ActionEvent` is de natuurlijke plek waar een dienst kán besluiten een gebeurtenis te schrijven, maar dat maakt de families geen geschiedenis; het maakt ze een van de bronnen. De `events`-kolom blijft wat hij is en wordt geen bron: hij bevat Kubernetes-events, niet onze eigen.

---

## Vork 1: waar leggen we vast

De vraag is niet alleen waar de gegevens komen te staan, maar of het antwoord op "sinds wanneer is dit rood" en "wie deed dit" eruit komt.

### Richting 1a: een eigen tabel in rig-db, in de lijn van `async_tasks` en `runs`

Een tabel (VOORSTEL: `gebeurtenissen`) met een Alembic-migratie als vijfde in de rij (`opi/migrations/versions/`), geschreven via SQLAlchemy zoals `AsyncTaskService` dat doet.

*Kost:* een migratie, een service, een schrijfweg per bron, een opruimlus, en een index-ontwerp dat per project, per deployment en per tijdvak snel is. Groei: bij een ruwe schatting van enkele honderden gebeurtenissen per dag over alle projecten is dat orde 100k rijen per jaar, wat voor PostgreSQL niets is. De echte kost is discipline: elke nieuwe schrijfweg moet eraan denken.

*Levert:* bevraagbaar per project en per deployment met de autorisatie die er al is; overleeft een herstart en een pod-verplaatsing; retentie in eigen hand; en het antwoord op "sinds wanneer" komt er direct uit met een `ORDER BY tijdstip`. Bovendien is de infrastructuur er al: `rig-db` draait, de pool staat (`opi/core/database_pools.py`), Alembic draait bij het opstarten (`opi/core/startup.py:56`, aangeroepen op `:513`), en er zijn vier migraties als voorbeeld.

*Bezwaar:* het is een negende administratie naast de acht uit deel 1. Dat bezwaar is echter minder zwaar dan het lijkt, want de acht bestaande zijn geen geschiedenissen maar toestanden met een tijdstempel eraan geplakt. `async_tasks` is een werkwachtrij die na een uur wordt geleegd, `runs` een levenscyclus van één werklastsoort, `marked_for_deletion` een voornemen, `subdomain_registry` een claim. Alleen `resources.history` in het projectbestand is echt een gebeurtenissenlog, en dat is er een van één domein zonder actor.

### Richting 1b: alleen gestructureerd loggen naar Loki

De logregels krijgen een vaste JSON-vorm met velden in plaats van proza, en de retentie is die van Loki.

*Kost:* laag, en het meeste ligt er al: `flow_id` staat al in elke regel (`opi/utils/logging_config.py:48`, `opi/core/flow_id.py`), de logger is overal, en de log watcher leest al uit Loki.

*Levert:* zoekbaarheid over alles heen, inclusief de regels die geen gebeurtenis zijn.

*Valt af als enige oplossing, om drie redenen.* De Loki-, Grafana- en Mimir-stack staat niet in deze repo maar wordt geleverd (deel 1, "meld- en exportinfrastructuur"): het spoor is `GRAFANA_URL` en `GRAFANA_DATASOURCE_UID` in `opi/core/config.py:434-437` plus `mimir-prd` in de productie-configmap, en de retentie ervan is niet te verifiëren en niet in ons beheer. Een tijdlijn per project rendert uit Loki betekent dat de portal afhankelijk wordt van een externe dienst voor een gewone pagina. En de autorisatie klopt niet: Loki kent onze projectrollen niet, dus lezen zou langs een tweede regel lopen in plaats van langs `is_user_authorized_for_project`.

*Blijft staan als aanvulling.* Gestructureerd loggen is goedkoop en maakt de log watcher beter. Het is geen vervanging van de bron van waarheid.

### Richting 1c: OTLP, met de afhankelijkheden die er al liggen

Alle negen `opentelemetry-*`-pakketten staan in `operations-manager/python/pyproject.toml:66-74` en `opi/core/tracing.py` is compleet.

*Kost bij nader inzien hoger dan het lijkt.* Drie dingen die in het startpunt niet zichtbaar waren. Ten eerste importeert `opi/core/tracing.py` uitsluitend `OTLPSpanExporter`: er is een trace-exporter en geen log- of metriekexporter, dus een gebeurtenis zou als span moeten worden weggeschreven, wat semantisch scheef is (een gebeurtenis heeft geen duur). Ten tweede staat er geen ontvanger: `OTEL_EXPORTER_OTLP_ENDPOINT` wijst naar `http://jaeger.rig-system:4317` (`opi/core/config.py:274`) en er staat geen Jaeger in `infrastructure/bootstrap/infrastructure/`. Ten derde is een tracingbackend geoptimaliseerd voor bemonsterde, kortlevende spans, niet voor een volledig, jarenlang bewaard verslag.

*Levert:* een export die aansluit op het Logboek Dataverwerkingen (zie vork 2 en de paragraaf over bewaartermijn), en verrijking van alle andere waarnemingen als tracing ooit aan gaat.

*Blijft staan als exportweg, niet als bron van waarheid.*

### Richting 1d: Kubernetes-events op de projectnamespace

OPI schrijft een `Event`-object in de namespace van het project.

*Kost:* laag qua code (de kubectl-connector kan het), maar hoog qua eigenschappen.

*Valt af.* De standaardretentie van Kubernetes-events is een uur (de `--event-ttl`-standaard van de kube-apiserver; **niet geverifieerd** op ODCN, want die instelling staat niet in deze repo), wat het probleem niet oplost maar verplaatst. De events zijn zichtbaar voor iedereen met leesrechten op de namespace, wat niet dezelfde kring is als de projectleden. Platformgebeurtenissen hebben geen namespace om in te landen. En de gebruiker van ZAD kijkt niet in een namespace; hij kijkt in de portal.

*Blijft staan als bron, niet als opslag.* Kubernetes-events zeggen dingen die wij niet zelf weten (image-pull-backoff, OOMKilled, probe-kills) en worden al per taak opgehaald.

### Richting 1e: de database is de waarheid, OTLP is de export

De gebeurtenis wordt weggeschreven in de eigen tabel, en een exporteur op de rand kan hem doorzetten naar OTLP of naar een notificatieservice.

*Aanbeveling: 1e, opgebouwd uit 1a plus 1c later, met 1b als goedkope aanvulling en 1d als bron.*

De reden is de vraag zelf. "Sinds wanneer is dit rood" en "wie deed dit" zijn beide vragen aan een verslag met een sleutel en een tijdsordening, en dat is een tabel. Zodra de tabel er is, is elke exporteur een optie en geen voorwaarde: OTLP later toevoegen kost dan een exporteur, terwijl OTLP eerst kiezen betekent dat de portal een externe dienst moet bevragen voor een tijdlijn.

**Wat het kost om de export pas later te doen:** één ding, en het is echt. Als het recordformaat pas bij de export wordt bedacht, is de kans groot dat velden die de export nodig heeft er niet in staan (een stabiele `type`-naam, een `subject`, een schema-verwijzing). Dat is te ondervangen door het formaat nu goed te kiezen, en dat is vork 2.

---

## Vork 2: welk recordformaat

### Richting 2a: een eigen minimaal schema

Een tabel met de velden die de vragen uit deel 1 beantwoorden. VOORSTEL voor de kolommen, om over te praten:

| Kolom (VOORSTEL) | Waarom |
|---|---|
| `id` | UUID, primaire sleutel, zoals `async_tasks` en `runs` |
| `type` | De soort gebeurtenis, uit een gesloten enum in code, zoals `TaskType` |
| `tijdstip` | `TIMESTAMPTZ`, met zone, anders dan de logregels van vandaag |
| `cluster` | Elke instantie beheert alleen zijn eigen cluster |
| `project` / `deployment` / `component` | De niveaus uit deel 1; leeg betekent platformniveau |
| `actor` | Het e-mailadres van een mens, of de naam van een scheduler, of `API` |
| `actor_soort` | mens, agent, scheduler, cluster, buiten |
| `ernst` | Zie vork 4 |
| `samenvatting` | Eén regel, mensleesbaar, Nederlands |
| `gegevens` | JSONB, de details van deze soort |
| `flow_id` | De bestaande correlatie-identificatie uit `opi/core/flow_id.py` |
| `taak_id` | Verwijzing naar `async_tasks.id` als die er is, zonder vreemde sleutel, want die rij wordt na een uur verwijderd |

*Kost:* laag, en volledig in eigen hand.

*Levert:* precies wat de vragen vragen, en niets meer.

### Richting 2b: CloudEvents 1.0 volgens het NL GOV profiel

Het NL GOV profiel voor CloudEvents staat op de lijst van het Forum Standaardisatie als pas-toe-of-leg-uit (CloudEvents v1.0, goedgekeurd door het OBDO op 25 november 2025); de meest recente vastgestelde Logius-versie is v1.1. Het profiel eist vier attributen: `id`, `source` in URN-notatie met de `nld`-namespace (`urn:nld:oin:<OIN>:systeem:<systeemnaam>`), `specversion: "1.0"`, en `type` in reverse-DNS-notatie met een `v`-suffix voor versies. Optioneel zijn onder meer `subject` (waarop de gebeurtenis betrekking heeft, zodat een afnemer kan filteren zonder de payload te openen), `time` in RFC 3339, `datacontenttype`, `dataschema` en `dataref`.

*Kost:* het `source`-attribuut vraagt een OIN, en die staat nergens in deze repo; `features/local-cluster-federation.md` noemt `organization.number` alleen als doorgegeven Keycloak-attribuut. Er moet dus een organisatie-OIN worden vastgesteld voordat een geldige `source` te bouwen is. Verder vraagt het profiel een centraal register voor eventtypes, en de `cloudevents`-SDK is een extra afhankelijkheid.

*Levert:* een record dat een externe afnemer zonder uitleg begrijpt, en een pas-toe-of-leg-uit-verplichting die vervuld is in plaats van uitgelegd.

*Bron en voorbehoud:* het bovenstaande komt uit de skill `standaarden:ls-notif` (plugin `standaarden` 0.3.9), die zichzelf als concept aanmerkt en niet de normatieve tekst is. De publicaties op forumstandaardisatie.nl en gitdocumentatie.logius.nl zijn leidend; wie het profiel daadwerkelijk gaat implementeren, leest die eerst.

*Belangrijk detail dat de richting kleurt:* het profiel schrijft voor dat er **geen persoonsgegevens in de context-attributen** staan, omdat die door tussenliggende systemen worden gelogd. Bij ons is de actor een e-mailadres van een medewerker, en dat is een persoonsgegeven. Een naïeve projectie die de actor in `subject` zet, schendt het profiel. Dat is oplosbaar (actor in `data`, `subject` op de projectnaam) maar het moet expliciet.

### Aanbeveling: intern 2a, met een CloudEvents-projectie op de rand, en het formaat nu al zo kiezen dat die projectie triviaal is

De afweging is niet religieus, en het beslissende argument is dat de twee dezelfde vorm hebben zodra je de kolommen goed kiest. `type` wordt intern een gesloten enum met reverse-DNS-waarden erin (VOORSTEL: `nl.rig.zad.deployment.geheugen-verhoogd.v1`), zodat de projectie een hernoeming is en geen vertaling. `tijdstip` is `TIMESTAMPTZ` en dus RFC 3339. `id` is al een UUID. `project` wordt `subject`. `source` is het enige dat pas bij de export ontstaat, en dat is één constante per cluster.

**Wat het kost om CloudEvents pas later te doen, als je nu deze twee dingen doet:** vrijwel niets, één exporteurfunctie. **Wat het kost als je ze niet doet:** een migratie over de hele tabel om `type` te hernoemen, plus een tweede naam voor elke gebeurtenissoort die dan permanent naast de eerste blijft bestaan. Het verschil tussen die twee is de hele reden dat dit nu een beslissing is en niet later.

---

## Vork 3: hoe melden

Vijf kanalen, elk aan een publiek gekoppeld. De publieken komen uit deel 1.

| Kanaal | Publiek | Push of pull | Bestaat al | Kost | Levert |
|---|---|---|---|---|---|
| Tijdlijn in de portal, per project en per deployment | gebruiker van een project | pull | De portal wel, de tijdlijn niet | Een pagina, een query, een autorisatiecontrole die er al is | Altijd beschikbaar, geen nieuwe infrastructuur, geen abonnement, geen afmeldpad, geen bezorgprobleem |
| Mail via de eigen relay | gebruiker van een project | push | Relay en platformaccount wel (`opi/manager/mail_manager.py:248`); een verzendweg vanuit OPI niet | Een SMTP-client in OPI, sjablonen, abonnementen, een afmeldpad, en een oplossing voor de bounce (deel 1, punt 3) | Bereikt iemand die niet kijkt |
| ntfy | platformteam | push | Ja (`opi/services/log_watcher.py:327`) | Niets | Werkt vandaag al, maar is één topic voor één publiek |
| Webhook per project | agent of eigen tooling | push | Nee | Een bezorger met herhaalbeleid, een geheim per abonnement, uitgaand netwerkbeleid | Het enige kanaal dat een agent echt kan gebruiken; sluit aan op het Abonneren-model uit de Logius-standaard, waar push het aanbevolen model is |
| Prometheus met Alertmanager | platformteam | push | Prometheus wel, Alertmanager niet en alerteringsregels niet (deel 1) | Alertmanager uitrollen, regels schrijven, een route naar ntfy of mail | Het juiste gereedschap voor wat eigenlijk een metriek is (te veel mislukte taken per uur, wachtrij loopt op) |

### De volgorde die hieruit volgt

**Eerst de tijdlijn.** Hij vraagt geen nieuwe infrastructuur, geen abonnement, geen afmeldpad, en hij kan niet mislukken bij de bezorging. Hij is bovendien de enige manier om te controleren of de gebeurtenissen die je vastlegt de goede zijn, voordat je ze naar iemands postvak stuurt. Een meldkanaal bouwen op gebeurtenissen die je nog niet hebt bekeken is de snelste weg naar een kanaal dat wordt uitgezet.

**Dan de webhook.** Hij bedient het publiek dat vandaag het slechtst bediend is (deel 1, groep C), hij heeft geen persoonsgegevens nodig in het kanaal zelf, en hij is per project af te schermen. Hij is technisch het lastigst vanwege herhaalbeleid en uitgaand verkeer, maar hij vraagt geen keten buiten ons beheer.

**Daarna pas mail.** Niet omdat het onbelangrijk is, maar omdat de keten aantoonbaar niet af is: naar buiten mailen kan niet, bounces verdwijnen stil, en de MTA-STS-lookup hangt (`TODO.md` punt 26, alle drie gemeten op 21 augustus 2026). Een meldsysteem opzetten op een keten die stil faalt, is een meldsysteem dat stil faalt. De vier punten uit dat TODO-item zijn de voorwaarde, niet de context.

**ntfy blijft wat het is,** het kanaal van het platformteam, en wordt niet uitgebreid naar gebruikers: het vraagt een app en een topic, en het heeft geen autorisatie per project.

**Alertmanager als eigen spoor.** Wat een drempelwaarde over een reeks is (wachtrijlengte, foutpercentage, backups die niet liepen) hoort niet in een gebeurtenissenlog maar in een metriek met een regel eroverheen. Het is geen concurrent van de gebeurtenissen maar de andere helft.

### Waar wordt een abonnement vastgelegd

Twee kandidaten, en dit is een echte keuze met echte gevolgen.

**In het projectbestand,** waar de rest van de projectconfiguratie staat. *Voor:* het staat waar alles staat, het is via de wizard en de API te bewerken met de bestaande editables, het is versiebeheerd, en een abonnement verdwijnt vanzelf als het project verdwijnt. *Tegen:* elke wijziging is een commit in `zad-projects` met een AGE-hercodering van de geheimen erin (een webhook-geheim moet versleuteld), een GitOps-diff, en een herverwerking van het project. Een gebruiker die zijn e-mailmelding uitzet veroorzaakt daarmee een deployment-cyclus, en dat is een absurde verhouding tussen oorzaak en gevolg.

**In de database,** naast de gebeurtenissen zelf. *Voor:* geen commit, geen hercodering, geen herverwerking; een afmelding is een `UPDATE`. Het schaalt naar iets dat vaak verandert. *Tegen:* het is een tweede plek waar projectconfiguratie staat, en dat is precies wat dit project elders vermijdt; en het overleeft geen herbouw van het cluster uit git.

**Aanbeveling: in de database, met één uitzondering.** Een abonnement is geen infrastructuurwens maar een voorkeur, hij verandert vaak, en de afmelding moet goedkoop zijn (zie hieronder bij persoonsgegevens: een afmeldpad dat een deployment veroorzaakt, is een afmeldpad dat niet wordt gebruikt). Het argument dat het niet uit git te herbouwen is, telt hier minder zwaar dan elders: een verloren abonnement betekent dat iemand een melding mist, niet dat een applicatie niet draait.

De uitzondering is het geval waarin een abonnement een dienst wordt, dus met een uitgaande netwerkregel en een geheim. Zodra een webhook een NetworkPolicy nodig heeft, is hij infrastructuur en hoort het aan-uit-vinkje in het projectbestand, precies zoals `vlam` en `send-email` dat doen (`features/vlam-service.md`, `features/send-email.md`). De ontvanger-URL en het bezorgbeleid kunnen dan nog steeds in de database staan.

---

## Vork 4: ruis en drempel

Dit is geen theorie. De log watcher heeft alle drempels die hij heeft nodig gehad om bruikbaar te blijven, en ze staan er nog steeds: een ignore-lijst van 91 regels (`opi/services/log_watch_ignore_patterns.txt`), een dedup-venster van zes uur (`dedup_hours: float = 6.0`, `opi/services/log_watcher.py:74`), een begrenzing op tien regels per bericht (`MAX_BODY_LINES = 10`, `:43`), een uitsluiting van zijn eigen logregels om te voorkomen dat hij op zichzelf alarmeert (`SELF_LOG_EXCLUDE`, `:48`), en een regex die alleen echte Python-logrecords als aparte melding telt omdat Loki elke stackframe als eigen regel opslaat (`_LOG_RECORD_RE`, `:55`).

Dat is vijf lagen ruisonderdrukking op een systeem dat één ding doet. Een gebeurtenissensysteem met vijftien soorten heeft ze allemaal nodig, en het is goedkoper ze in het ontwerp te zetten dan er later omheen te bouwen.

### Ernst

**Aanbeveling: drie niveaus, niet vijf.** VOORSTEL: `informatie` (het is gebeurd, kijk erin als je wilt), `aandacht` (iemand moet hier iets mee, maar het kan wachten), `storing` (iets werkt niet). Vijf niveaus leiden ertoe dat niemand het verschil tussen twee middenniveaus kan uitleggen, en dan glijdt alles naar boven.

De ernst hoort bij de gebeurtenissoort, vast in code, niet per geval bepaald. Een gebeurtenis waarvan de ernst per geval verschilt, zijn eigenlijk twee soorten.

### Dedup

**Aanbeveling: de dedup-sleutel is een eigenschap van de soort, en de deduplicatie gebeurt bij het melden, niet bij het vastleggen.** Dat is het belangrijkste ontwerpbesluit in deze vork. Alles wordt vastgelegd; wat wordt gemeld is een afgeleide. De reden: als je bij het vastleggen dedupliceert, kun je achteraf niet meer vaststellen hoe vaak iets gebeurde, en "vijftig keer in een uur" is precies het signaal dat je wilt hebben. Bij het melden is dedup een `GROUP BY` over een venster.

### Samenvoeging

**Aanbeveling: één melding per project per venster, met de gebeurtenissen erin gegroepeerd,** niet één melding per gebeurtenis. Dit is dezelfde keuze die de log watcher al maakt (één ntfy-bericht met maximaal tien regels, gegroepeerd) en om dezelfde reden: het abonneernummer is het aantal berichten, niet het aantal gebeurtenissen.

### Wat er gebeurt bij een herstart

Dit is de vraag die de opdracht terecht apart stelt, want de log watcher heeft hier vandaag een bekend gat: `self._state: dict[str, str] = {}` in `opi/core/logwatcher_scheduler.py:32`, met het commentaar "it resets on an OPI restart, which at worst repeats one alert". Voor één ntfy-topic is dat aanvaardbaar. Voor mail naar gebruikers is het dat niet: een herstart tijdens een uitrol zou iedereen een dubbele mail sturen.

**Aanbeveling: geen dedup-toestand in het geheugen.** De laatst gemelde stand hoort in dezelfde database als de gebeurtenissen, als een watermerk per abonnement (VOORSTEL: `laatst_gemeld_tot`, een tijdstip). Melden is dan "alles sinds het watermerk, gegroepeerd", en het watermerk schuift pas op na een geslaagde bezorging. Een herstart midden in een melding levert dan hooguit een herhaling van één venster, en een bezorging die mislukt levert een herhaling in plaats van een gat. Een gat is erger dan een herhaling: een herhaling is irritant, een gat is een gemiste storing.

**Aanbeveling: een absolute bovengrens per abonnement per dag,** zodat een lus die duizend gebeurtenissen produceert niet duizend meldingen produceert. Bij overschrijding: één melding die zegt dat de grens is geraakt, met een verwijzing naar de tijdlijn. Dit is een noodrem, geen beleid.

---

## Wie mag welke gebeurtenis zien

Een gebeurtenis draagt projectscope, dus lezen loopt langs `is_user_authorized_for_project` in `opi/services/project_authorization.py:40`, en niet langs een eigen tweede regel. Dat is niet alleen netjes maar noodzakelijk: de rollen komen uit het projectbestand via de ProjectStore, en een tweede kopie van die logica loopt gegarandeerd uit de pas op het moment dat iemand een lid verwijdert. De trage reconcile-poll (`opi/services/project_store.py:1343`) bestaat precies omdat een intrekking binnen een begrensd venster moet doorwerken; een tweede autorisatieweg zou dat venster stilzwijgend verlengen.

Gebeurtenissen zonder project zijn platformgebeurtenissen en zijn alleen voor beheerders. Er is geen tussenvorm: een gebeurtenis die "een beetje" van een project is, is een ontwerpfout in de gebeurtenissoort.

### Wat er in een gebeurtenis terechtkomt

Dit is de scherpste rand van het hele ontwerp. Een `error_message` uit een connector kan een geheim dragen, en de weg daarheen is kort.

Twee gemeten precedenten in deze repo. `opi/utils/api_keys.py:39` logt de volledige API-sleutel op DEBUG als `USE_UNSAFE_API_KEY` aanstaat; die vlag staat op productie uit, maar de `opi`-logger staat onvoorwaardelijk op DEBUG (`opi/utils/logging_config.py`), dus één omgevingsvariabele scheidt dat van een sleutel in de log (bevinding G in `plans/technische-review-bio-en-nora-bevindingen.md`, met BIO2 8.15.02 erbij: een logregel bevat nooit gegevens die tot het doorbreken van de beveiliging kunnen leiden). En de mailconnector, de Keycloak-connector en de postgres-connector krijgen allemaal wachtwoorden mee die in een uitzonderingsboodschap kunnen belanden.

**Aanbeveling: een gebeurtenis bouwt zijn eigen velden op, en neemt nooit een vrije `str(exception)` over.** Concreet: de gebeurtenissoort bepaalt welke velden er in `gegevens` staan, en een foutmelding komt er alleen in als de code hem expliciet heeft samengesteld. Dat is strenger dan wat `async_tasks.error_message` vandaag doet, en met opzet: die tabel wordt na een uur geleegd en een gebeurtenissenlog niet.

**Aanbeveling: één redactiefunctie op de schrijfweg, niet op de leesweg.** Redactie bij het tonen is geen redactie: de waarde staat dan al in de database, in de backup, en in elke export. Er is vandaag precies één redactiehulpmiddel, en het is te smal om hier op te leunen: `redact_sensitive_headers` in `opi/utils/logging_redact.py:25` maskeert zeven HTTP-headernamen (`authorization`, `x-api-key`, `cookie` en vier andere) en wordt op één plek aangeroepen, in `opi/connectors/argo.py`. Er is geen redactie voor waarden binnen een foutmelding. Dat hulpmiddel moet er dus komen; het bestaande is er het beginpunt van, niet de oplossing.

---

## Bewaartermijn en persoonsgegevens

Een gebeurtenissenlog met e-mailadressen erin is een verwerking van persoonsgegevens. De actor is een medewerker, het e-mailadres identificeert hem, en het verslag zegt wat hij deed en wanneer. Dat is geen randgeval.

### Geldt het Logboek Dataverwerkingen hier

**Nee, en dat is een inhoudelijk antwoord, geen ontsnapping.** De standaard (werkversie, nog geen vastgestelde versie, nog niet op de lijst van het Forum Standaardisatie) is gebouwd rond twee verplichte attributen: `dpl.core.processing_activity_id`, een verwijzing naar een verwerkingsactiviteit in het register uit AVG artikel 30, en `dpl.core.data_subject_id` met `data_subject_id_type` (`BSN`, personeelsnummer, URI), de betrokkene op wiens gegevens de verwerking betrekking heeft. Het doel is transparantie richting de burger: welke organisatie raakte wanneer mijn gegevens aan, over organisatiegrenzen heen te volgen via W3C Trace Context.

ZAD verwerkt geen burgergegevens. Een gebeurtenis in ZAD zegt "deze beheerder heeft de geheugengrens van dit component verhoogd". De betrokkene, als je die al wilt aanwijzen, is de handelende medewerker zelf, en dan valt `data_subject_id` samen met `actor`, wat de standaard niet bedoelt. Er is geen verwerkingsactiviteit uit een artikel-30-register om naar te verwijzen, want dit is geen verwerking van persoonsgegevens als doel maar als bijvangst van beheerhandelingen.

Wat wél overneembaar is, en de moeite waard: de **vorm**. Een gebeurtenis met een correlatie-identificatie die over systeemgrenzen meereist is precies wat `flow_id` (`opi/core/flow_id.py`) vandaag al doet binnen één proces, en OTLP als exportprotocol is een verstandige keuze om andere redenen dan naleving. NEN 7513 is een zorg-specifieke uitbreiding op deze standaard en is hier niet van toepassing.

*Voorbehoud:* het bovenstaande is gebaseerd op de skill `standaarden:ls-logboek` (versie 0.3.9, met een eigen conceptvoorbehoud) en niet op de normatieve tekst bij Logius. Als iemand een dwingende reden heeft om de standaard wel toe te passen, is dat een gesprek over de scope van het artikel-30-register en niet over de techniek.

### Geldt de BIO2 hier

**Ja, en er ligt al een uitspraak in dit project die het aanscherpt.** `features/bio-network-access-no-vpn-compliance.md` legt vast dat ZAD bewust geen VPN gebruikt en dat dat onder BIO2 v1.3 verdedigbaar is, mits onder meer "segmentatie, sterke authenticatie en logging/monitoring de functie van een VPN compenseren". In de tabel met compenserende maatregelen staat letterlijk: "Detectie/herleidbaarheid: Logging (8.15) + monitoring (8.16)".

Dat is een claim die vandaag zwakker staat dan het document suggereert. De logs zijn niet in ons beheer, de enige tabel met een actor wordt na een uur geleegd, en `plans/technische-review-bio-en-nora-bevindingen.md` stelt bij bevinding E vast dat BIO2 8.15.01 een logregel voorschrijft met minimaal actie, object, resultaat, oorsprong, **actor** en tijdstempel, en dat precies de actor ontbreekt. Ook 8.15.04 komt daar aan bod: de bewaartermijn moet risicogericht worden bepaald, rekening houdend met aanvallers die langdurig binnen zijn, en een uur is dat niet.

Met andere woorden: het gebeurtenissenwerk is niet alleen een gebruikerswens. Het is de compenserende maatregel die in een bestaande risicoafweging al is opgeschreven maar nog niet is waargemaakt.

*Voorbehoud:* de skill `bio` was in deze sessie niet geïnstalleerd (alleen de plugin `standaarden` is aanwezig, en die bevat geen BIO-skill). De BIO2-uitspraken hierboven komen daarom uit twee documenten in deze repo die zelf verbatim controlteksten citeren, niet uit de normatieve bron. Ze zijn daarmee **niet onafhankelijk geverifieerd**.

### Aanbeveling voor de bewaartermijn

**Twee termijnen, niet één.**

**De gebeurtenis zelf: 90 dagen, met een pseudonimisering daarna in plaats van verwijdering.** Negentig dagen omdat die termijn hier al een keer is gekozen en verdedigd, namelijk voor de Keycloak-auditevents (`eventsExpiration: 7776000`, in zes realm-configuraties); dezelfde termijn twee keer gebruiken is makkelijker uit te leggen dan een nieuw getal verzinnen. Na 90 dagen wordt `actor` leeggemaakt of vervangen door een niet-herleidbare aanduiding, en blijft de rest staan. Dat behoudt "wat is er met dit project gebeurd", wat een beheergeschiedenis is en geen persoonsgegeven, en verwijdert "wie deed het", wat het persoonsgegeven is.

**Beveiligingsgebeurtenissen: langer, en dat is een aparte beslissing.** Een geweigerde API-sleutel of een afgewezen inlog is precies het spoor dat je bij een incident maanden later terug wilt lezen; BIO2 8.15.04 noemt langdurig aanwezige aanvallers met zoveel woorden. Wat "langer" is, is een beslissing voor een mens (deel 3), en 90 dagen is daarvoor waarschijnlijk aan de korte kant.

**Opruimen gebeurt door een lus die er al staat.** `cleanup_old_tasks` (`opi/core/async_task_service.py:670`) is het patroon: een `DELETE` op een cutoff, aangeroepen vanuit de takenlus. Er komt geen nieuwe scheduler bij.

**Het afmeldpad hoort goedkoop te zijn.** Iemand die geen mail meer wil, moet dat kunnen zonder dat er een deployment op gang komt. Dat is het praktische argument achter de keuze in vork 3 om abonnementen in de database te zetten, en het is tegelijk een AVG-argument: een bezwaarrecht dat een commit veroorzaakt, is een bezwaarrecht met een drempel.

---

## Wat er nog aan open randen ligt

- Of er een organisatie-OIN is voor RIG. Zonder die is een geldige CloudEvents `source` niet te bouwen. **Niet geverifieerd**, en niet in deze repo te vinden.
- Of de tijdlijn per deployment de `deviations` en `errors` uit `opi/services/deployment_diagnostics.py` moet gaan vastleggen als overgangen, of dat die berekening blijft wat hij is en er alleen een gebeurtenis wordt geschreven bij een verandering. Dat laatste is goedkoper maar vraagt een vergelijking met de vorige stand, en die stand is er vandaag niet.
- Of de wizard, die nog via de in-memory `task_manager` loopt, gebeurtenissen kan schrijven voordat `features/futures/migrate-task-progress-to-database.md` is afgerond. De `PersistentTaskProgressManager` bestaat inmiddels (`opi/core/persistent_task_progress.py`) en wordt door de TaskWorker gebruikt, dus dat toekomstdocument beschrijft werk dat gedeeltelijk gedaan is; hoeveel precies is **niet geverifieerd**.
