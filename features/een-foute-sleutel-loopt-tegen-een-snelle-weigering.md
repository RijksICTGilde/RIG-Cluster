# Een foute sleutel of een stortvloed loopt tegen een snelle weigering

De machine-API van OPI legitimeert zich met een `X-API-Key` per project en met een
admin- of mastersleutel. De vergelijking was al constant-time en het antwoord bij elke
fout al een identieke 401, dus er lekte niet welk deel fout was, maar er zat geen rem op
het AANTAL pogingen: een aanvaller kon blijven raden, en elke poging kostte OPI werk (een
projectbestand lezen, soms een subprocess). OPI is een proces per cluster, dus zo'n
stortvloed legt hem plat voor alle projecten tegelijk.

Er zitten nu twee lagen in, die elkaar niet vervangen:

| laag | wat hij tegenhoudt | waar |
|---|---|---|
| faalteller | het raden van een sleutel | `opi/api/rate_limiter.py`, aangeroepen uit `opi/api/endpoint_util.py` en uit `opi/middleware/body_limit.py` |
| grove API-grens | een stortvloed van goed gevormde verzoeken | `opi/middleware/rate_limit.py` |

Beide weigeren meteen met een **429 en een `Retry-After`**, nooit met een vertraging. Een
mislukte poging laten wachten is voor een service de verkeerde reflex: een opengehouden
verzoek kost een slot in de event-loop, dus een stortvloed van foute sleutels die elk een
paar seconden slapen is precies hoe OPI zelf onderuitgaat.

Dit is de BINNENSTE laag. Een ingress kan daarvoor een grove, volumetrische grens per IP
leggen, goedkoop en voordat het verzoek Python bereikt, maar hij kent onze authenticatie
niet en kan dus geen grens per sleutel of project leggen.

## De faalteller

Alleen falen kost een token. Een geslaagde authenticatie telt niet mee, dus een client die
elke twee seconden een taak opvraagt raakt de grens nooit.

Er zijn twee aparte tellers, zoals `opi/api/invite_routes.py` het ook doet: een per
client en een per aangeboden sleutel. Ze apart houden is nodig: zet je client en sleutel
samen in een string, dan verschuift stil waarop de tweede emmer van `IPRateLimiter` telt,
en dan weigert hij een onschuldige client die zijn header-vingerafdruk met een rader deelt
(elke `python-requests` van dezelfde versie doet dat).

| teller | emmer | grens | weigert |
|---|---|---|---|
| `AUTH_FAILURE_PER_KEY` | de vingerafdruk van de aangeboden sleutel | `AUTH_FAILURE_LIMIT_PER_MINUTE` (10/min) per sleutel | vóór `compare_digest` en vóór het lezen van het projectbestand |
| `AUTH_FAILURE_PER_CLIENT` | `ip \| vingerafdruk \| sessie` | dezelfde grens, en de tweede emmer van de limiter per IP op driemaal die grens | pas ná een mislukte vergelijking |

Dat verschil in moment is de kern van het ontwerp. De emmer per SLEUTEL mag vóór de
vergelijking weigeren, want wie de sleutel niet heeft kan hem niet leeg krijgen: alleen
falen telt af, en een foute sleutel heeft een andere vingerafdruk dan de juiste. Wie de
sleutel wel heeft kan hem wel laten weigeren, door hem op het verkeerde project aan te
bieden: dan mislukt de vergelijking op de juiste sleutel en telt zijn emmer af (gemeten:
tien keer op een ander project, daarna 429 op het eigen project tot de emmer bijvult).
Dat is geen rechtenverhoging, want met die sleutel kon de aanbieder het project toch al
wijzigen. Het is wel de vorm waarin een verkeerd geconfigureerde client zichzelf
buitensluit: de juiste sleutel met de verkeerde projectnaam in de URL.

De emmer per CLIENT telt op het IP (de tweede emmer van `IPRateLimiter` telt op niets
anders), dus wie daar vóór de vergelijking op weigert, sluit de houder van een GELDIGE
sleutel buiten zodra een rader hetzelfde IP gebruikt. Dat heeft die rader geen sleutel
voor nodig, en achter één uitgaand IP (NAT, CI-runners) gebeurt het zonder enige aanval.
Daarom weigert die emmer pas als de vergelijking al mislukt is: een geldige sleutel komt
daar nooit langs. Het werk dat de vroege weigering daar uitspaart is klein,
`get_project_store().get()` is een lookup in de cache in het geheugen.

| wat de aanvaller doet | waar hij op vastloopt |
|---|---|
| dezelfde foute sleutel herhalen | de teller per sleutel, vóór enig werk |
| elke poging een andere sleutel | de teller per client, na de mislukte vergelijking |
| alleen zijn User-Agent verwisselen | de emmer per IP, op driemaal de grens |
| ook zijn `X-Forwarded-For` verwisselen | het adres waarop geteld wordt komt niet uit die header, zie hieronder |
| een rader een geldige sleutel laten buitensluiten | lukt niet zonder die sleutel zelf; zie het verschil in moment hierboven |

## Op welk adres de rem telt

`X-Forwarded-For` is niet-geauthenticeerde tekst: iedereen op het pad mag er een waarde bij
zetten, dus de LINKSE waarde is die van de aanroeper zelf. Een rem die daarop telt, laat de
aanvaller zijn eigen emmer kiezen. Daarom is er een plek die het client-IP bepaalt,
`client_ip` in `opi/utils/client_ip.py`, en leest niemand die header meer zelf.

Die functie telt een vast aantal hops van RECHTS, zoals Envoy (`xff_num_trusted_hops`) en
wies (`TRUSTED_PROXY_HOPS`) het doen: alleen de waarde die een VERTROUWDE proxy zelf heeft
toegevoegd is iets waard. Met `TRUSTED_PROXY_HOPS=0`, de default, komt er geen header aan te
pas en telt de rem op het TCP-peer-adres, het enige gegeven in een verzoek dat niet te
kiezen valt. Elke opbouw met een proxy ervoor zet hem op 1 in zijn eigen configmap: op
odcn is dat de OpenShift-router, op de sandbox ingress-nginx. Alleen een opbouw zonder
proxy (docker-compose, pytest) houdt de default.

Twee dingen maken dat niet triviaal, en beide zijn gemeten (de metingen staan in
`features/futures/client-ip-vertrouwen-achter-de-router.md`):

- **De router stuurt twee headerregels.** HAProxy met `forwardedHeaderPolicy: Append` zet
  zijn waarde als APARTE regel achter die van de client, en `request.headers.get` geeft dan
  de EERSTE regel, dus die van de aanvaller. De keten wordt daarom met
  `request.headers.getlist` gelezen en pas daarna op komma's gesplitst.
- **`ProxyHeadersMiddleware` overschrijft `scope["client"]`.** Met `trusted_hosts=["*"]`
  zet uvicorn `always_trust` en neemt hij de linkse waarde, dus `request.client.host` is in
  elke handler al door de aanroeper te kiezen. `PeerAddressMiddleware`
  (`opi/middleware/client_ip.py`) staat daarom BUITEN die middleware en legt het echte
  peer-adres in de scope vast; `client_ip` leest dat, niet `request.client`.

Staat er op de verwachte plek geen geldig IP, of komt het verzoek met minder hops binnen
dan ingesteld, dan valt het terug op het peer-adres. Een poort achter het adres
(`1.2.3.4:56789`, `[::1]:443`) valt eraf.

## De grove API-grens

`RateLimitMiddleware` legt een token-bucket per client op alles onder `/api/` en `/auth/`.
Hij draait binnen de veiligheidsheaders (ook een 429 draagt ze) en vóór sessie,
onderhoud en autorisatie, dus een geweigerd verzoek kost geen van die stappen.

De grens staat ruim: hij hoort de piek af te vlakken, niet normaal gebruik te raken. Hij
draait zonder de nieuwe-clientgrens van `IPRateLimiter` (`MAX_NEW_CLIENTS_PER_MINUTE`, 100
per minuut). Die hoort bij een grens op een handvol routes, niet bij een grens die voor elk
verzoek geldt: wie daar client-ids verzint, sluit met honderd verzoeken iedere nieuwe
bezoeker buiten, en `/auth/login` zit erachter.

Zonder die grens is het client-id van buiten te kiezen, en dan mag de weigering zelf niets
kosten: anders bepaalt een aanvaller wat een verzoek van iedere andere client kost. Drie
remmen houden die kosten vlak, alle drie in het hete pad van `is_allowed`:

- **Een geweigerd verzoek laat geen emmer achter.** Een verse client heeft zijn volle
  burst, dus alleen de emmer per IP kan hem nog weigeren. Die wordt eerst GELEZEN, zonder
  af te tellen en zonder aan te maken. Vanaf een adres leveren verse client-ids daardoor
  niet meer emmers op dan de burst per IP, niet een per verzoek.
- **`MAX_TRACKED_CLIENTS` (10.000) is een harde bovengrens**, met verdringing op
  aanmaakvolgorde in O(1) per entry. Een scan hoeft dus niet te zorgen dat het geheugen
  begrensd blijft, ook niet als er niets oud genoeg is om op te ruimen. Verdringen geeft
  een client zijn volle burst terug; de emmer per IP blijft de backstop, en het adres
  daarin valt niet te kiezen.
- **De noodopruiming draait hoogstens een keer per `EMERGENCY_CLEANUP_INTERVAL_SECONDS`**
  (10 seconden). Boven `CLEANUP_THRESHOLD` draaide ze anders per verzoek, en dat zijn twee
  O(n)-scans onder de lock.

De opruiming bewaakt beide soorten emmers, die per client en die per IP, want een grens die
voor elk verzoek geldt maakt ze allebei aan. Gemeten met 30.000 verschillende adressen:
precies 10.000 emmers van elke soort. De drie remmen staan als toets in
`tests/test_rate_limiter.py` (`TestDeWeigeringBlijftGoedkoop`), met de getallen van voor de
reparatie als vergelijking.

Op de loginpaden komt er geen rem bovenop. Het raden van een wachtwoord wordt al bij de
bron afgeknepen, met `bruteForceProtected` op de Keycloak-realms
(`opi/configs/keycloak/bootstrap*.yaml`, `sso-support.yaml`, `sso-only.yaml`).

## Instellingen

Alle grenzen staan in `opi/core/config.py` en zijn per omgeving te zetten:

| instelling | default | wat het is |
|---|---|---|
| `AUTH_FAILURE_LIMIT_PER_MINUTE` | 10 | mislukte authenticatiepogingen per minuut, apart geteld per client en per aangeboden sleutel |
| `API_RATE_LIMIT_PER_MINUTE` | 600 | de grove grens op `/api` en `/auth` |
| `API_RATE_LIMIT_BURST` | 120 | hoeveel verzoeken daarvan tegelijk mogen |
| `RATE_LIMIT_RETRY_AFTER_MAX_SECONDS` | 60 | plafond op de `Retry-After` die meegaat |
| `TRUSTED_PROXY_HOPS` | 0 (odcn en sandbox: 1) | aantal vertrouwde proxy-hops in `X-Forwarded-For`, geteld van rechts |

## Grenzen van deze oplossing

- **De tellers staan in het geheugen van dit proces.** Dat klopt zolang OPI een proces per
  cluster is. Gaat OPI schalen, dan hoort er een teller achter Redis (`REDIS_HOST` is al
  geconfigureerd) die een restart en meerdere replica's overleeft.
- **Met een vertrouwde hop telt de rem op wat de ROUTER toevoegt, niet op wat de client
  stuurt.** Een gespoofte `X-Forwarded-For` levert geen verse emmer meer op; dat is gemeten
  in `tests/test_client_ip.py` (500 pogingen met een wisselend verzonnen adres vanaf
  hetzelfde echte adres lopen vast op een 429, en een geldige sleutel vanaf een ander echt
  adres blijft werken).
- **Wat `TRUSTED_PROXY_HOPS=1` wel aanneemt, is dat het verzoek via de router kwam.** Een
  aanroeper die OPI rechtstreeks op TCP/8000 bereikt en ZELF precies een
  `X-Forwarded-For`-waarde stuurt, staat op de plek waar de router zou staan en kiest dus
  zijn emmer. Wie dat kan, staat in het inkomende netwerkbeleid per opbouw:
  `odcn-production/network-policy.yaml` laat sinds RC-335 de router, Prometheus en elke
  namespace met `created-by: operations-manager` binnen, dus de tenant-namespaces houden
  dat pad, want de waker-pod van sleep-mode belt `ZAD_API_URL`. `odcn-staging` staat nog op
  `from: []`, en de sandbox-overlay heeft geen netwerkbeleid.
- **Voor de grove grens is dat pad een omweg om de rem heen, en dat is gemeten.** Met
  `TRUSTED_PROXY_HOPS=1` en een eigen `X-Forwarded-For` per verzoek kwamen 59 van 59
  verzoeken door een emmer die zonder die rotatie na 3 verzoeken een 429 gaf:
  `API_FLOOD_LIMITER` draait zonder de nieuwe-clientgrens, dus elk verzonnen adres is een
  verse emmer. De faaltellers op de sleutelpaden houden wel: dezelfde rotatie liep bij
  300 pogingen 200 keer vast op die grens (100 nieuwe client-ids per minuut), en de teller
  per sleutel telt helemaal niet op een adres. Het raden van een sleutel blijft dus
  afgeknepen; de stortvloed van een werklast IN het cluster niet.
- **De twee lagen die dat sluiten** staan in
  `features/futures/client-ip-vertrouwen-achter-de-router.md`: de
  `haproxy.router.openshift.io/set-forwarded-headers: "replace"`-annotatie op de Ingress,
  en het versmallen van het netwerkbeleid. Met die annotatie gooit de router de header van
  de client weg en is de rechtse waarde onvoorwaardelijk de zijne; zonder haar leunt hij op
  de gemeten vorm (twee regels, de router achteraan) plus op het netwerkpad. Versmallen tot
  alleen de ingress-namespace kan niet zolang de waker-pod OPI belt, dus laag 2 is op
  productie zo smal als ze wordt en de annotatie van laag 1 is daar de dekking. Een
  aanroeper die GEEN header stuurt, komt in alle gevallen op zijn peer-adres uit.
