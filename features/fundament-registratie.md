# Registratie van een Fundament-installatie

## Wat het is

Een ZAD-installatie op Fundament (de installer-plugin) moet bij de installatie een eigen
domein krijgen onder de fieldlab-zone, met een wildcardcertificaat en de gegevens om via de
Keycloak van productie in te loggen. De geheimen die daarvoor nodig zijn (een TransIP-token
met schrijfrecht op de zone en het secret van een Keycloak-client) horen niet op het cluster
van een deelnemer.

Daarom is er één endpoint in OPI dat die geheimen bewaakt: de installer vraagt, het endpoint
zet de DNS, haalt het certificaat en geeft alles terug. Het is gebouwd voor een fieldlab van
drie dagen en staat **los van de rest van OPI**: alles zit in `opi/fundament_registration/`,
met één regel in `opi/server.py`. Hoe je het weghaalt staat onderaan.

Het endpoint bestaat alleen op een cluster waar `FUNDAMENT_REGISTRATION_CODE` gezet is.
Zonder die instelling is de route er niet en antwoordt het pad 404.

## Het contract

Dit is de afspraak tussen het endpoint en de installer. Beide kanten bouwen hiertegen, dus
een wijziging hierin is een wijziging aan beide kanten.

### Vraag

```
POST /api/fundament/registreer
Content-Type: application/json

{
  "code":       "ABCD",
  "team":       "team42",
  "cluster_id": "3f9c2a7e-0b1d-4c55-9e1a-2b7d8c4f6a10",
  "ip":         "194.135.48.131"
}
```

| Veld | Regel |
|---|---|
| `code` | de fieldlab-code, niet leeg |
| `team` | een DNS-label: `^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$` |
| `cluster_id` | 1 tot 128 tekens uit `[A-Za-z0-9._-]`; de installer stuurt `FUNDAMENT_CLUSTER_ID` |
| `ip` | een publiek IPv4-adres: geen privé, loopback, link-local, CGNAT, multicast of gereserveerd adres |

Onbekende velden worden genegeerd, zodat er later een veld bij kan zonder dat een oude
installer breekt.

### Antwoorden

| Status | Wanneer | Lichaam | Wat de installer doet |
|---|---|---|---|
| **200** | het certificaat is er | zie hieronder, met `Cache-Control: no-store` | klaar |
| **202** | de aanvraag loopt | `{"status": "bezig"}`, header `Retry-After: 10` | na `Retry-After` seconden **dezelfde vraag** opnieuw |
| **400** | een veld voldoet niet aan zijn regel | `detail` noemt het veld en de regel | stoppen, melding tonen |
| **401** | code fout of leeg | `{"detail": "Ongeldige code"}` | stoppen, melding tonen |
| **409** | de teamnaam hoort bij een ander `cluster_id` | `{"detail": "Deze teamnaam is al in gebruik, kies een andere"}` | stoppen, melding tonen |
| **429** | de limiet op `/api/` of op mislukte codes | met `Retry-After` | wachten en opnieuw |
| **503** | TransIP of Let's Encrypt faalde, of de functie staat niet goed ingericht | `{"detail": "..."}` met de stap, zonder geheim | wachten (30 s, oplopend) en opnieuw |

Antwoord bij 200:

```json
{
  "domain":      "team42.zadlab.nl",
  "certificate": "-----BEGIN CERTIFICATE-----\n...",
  "private_key": "-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----\n",
  "sso": {
    "discovery_url": "https://keycloak.rijksapp.nl/realms/rig-platform/.well-known/openid-configuration",
    "client_id":     "...",
    "client_secret": "..."
  },
  "vlam": {
    "login_server": "https://vonk.rijksapp.dev",
    "gateway":      "100.80.0.1:443",
    "api_host":     "vlam-api.overheid-i.nl",
    "preauth_key":  "..."
  }
}
```

`certificate` is de volledige keten (leaf plus intermediates) voor `*.<team>.<zone>`,
`private_key` de ECDSA-sleutel ervan. Samen horen ze in een Kubernetes-secret van het type
`kubernetes.io/tls`. De drie velden onder `sso` gaan één op één naar
`KEYCLOAK_MASTER_OIDC_DISCOVERY_URL`, `KEYCLOAK_MASTER_OIDC_CLIENT_ID` en
`KEYCLOAK_MASTER_OIDC_CLIENT_SECRET` van de OPI op de installatie.

`vlam` is **optioneel** en staat er alleen als `FUNDAMENT_REGISTRATION_VLAM_PREAUTH_KEY`
gezet is. Het veld geeft alleen de tunnel: een API-key van VLAM zit er niet in, want die
heeft elke gebruiker zelf. Staat de pre-auth key niet, dan ontbreekt het hele veld: geen
`null`, geen lege waarden, en de rest van het antwoord werkt gewoon. De vier waarden komen bij
elk 200-antwoord vers uit de instellingen en staan **niet** in de tabel, zodat een nieuwe
pre-auth key na een week meteen met de volgende installatie meegaat.

Twee dingen die buiten de tabel vallen:

- Een lichaam dat geen JSON-object is, of helemaal ontbreekt, geeft de 422 die FastAPI zelf
  voor een onleesbaar verzoek geeft. De installer stuurt altijd een geldig object.
- Een 5xx die niet uit dit endpoint komt (een storing elders in OPI) draagt de envelope van
  `opi/server.py` in plaats van `{"detail": ...}`. De installer behandelt elke 5xx die geen
  503 is als een 503, dus dat maakt voor hem geen verschil.

### Waarom 202 en niet synchroon

Een DNS-01-aanvraag duurt tientallen seconden tot een paar minuten, vooral door het wachten
tot het TXT-record zichtbaar is. ingress-nginx breekt een verzoek standaard na 60 seconden
af. Met 202 duurt geen enkel verzoek lang, en herhalen is voor de installer hetzelfde als
opnieuw proberen: hij heeft één lus voor 202, 429 en 503.

## Hoe het werkt

| Onderdeel | Bestand |
|---|---|
| De route, de veldregels en de code-controle | `opi/fundament_registration/router.py` |
| De beslissing per vraag, en de aanvraag erachter | `opi/fundament_registration/registration.py` |
| De twee tabellen | `opi/fundament_registration/store.py` |
| lego, de werkmap en het ACME-account | `opi/fundament_registration/certificate.py` |
| Het TXT-record van de challenge | `opi/fundament_registration/dns_hook.py` |
| De instellingen | `opi/fundament_registration/configuration.py` |
| De tabellen in de database | `opi/migrations/versions/015_add_fundament_registrations.py` |

### De beslissing per vraag

| Toestand van de teamnaam | Wat het endpoint doet |
|---|---|
| onbekend | legt de teamnaam vast bij dit `cluster_id`, start de aanvraag, antwoordt 202 |
| bezig, zelfde `cluster_id` | 202 |
| bezig of klaar, ander `cluster_id` | 409 |
| klaar, zelfde `cluster_id` | 200 met het opgeslagen certificaat. Is het IP anders, dan verhuist eerst het A-record. Geen nieuw certificaat. |
| mislukt, zelfde `cluster_id` | 503 met de stap die faalde, en de volgende vraag begint opnieuw |
| bezig, maar langer dan 10 minuten stil | geldt als mislukt: de pod is herstart tijdens de aanvraag |

Een teamnaam blijft bij het eerste `cluster_id` dat hem vroeg, ook als die aanvraag mislukte.
Een herinstallatie op hetzelfde cluster krijgt hetzelfde certificaat terug, ook met een nieuw
IP: er wordt niets opnieuw uitgegeven.

De rij in de tabel is de waarheid over een aanvraag, niet de achtergrondtaak. Een aanvraag
claimen is daarom een voorwaardelijke schrijfactie (`INSERT .. ON CONFLICT DO NOTHING`,
`UPDATE .. WHERE state`), zodat twee vragen die tegelijk aankomen één aanvraag starten.

De kolom `step` draagt wat nog gemeld moet worden: de stap waarin een aanvraag zit terwijl
hij loopt, de stap die faalde daarna, en niets meer zodra de aanroeper het te horen heeft
gekregen. Die laatste overgang is wat "de volgende vraag begint opnieuw" betekent.

### De aanvraag zelf

1. Het A-record `*.<team>` in de zone naar `ip`, TTL 300. Bestaat het al, dan wordt het eerst
   verwijderd: vervangen is bij TransIP verwijderen plus toevoegen.
2. Een wildcardcertificaat voor `*.<team>.<zone>` via DNS-01, met lego.
3. Certificaat en sleutel opslaan, toestand op klaar.

lego lost de challenge op met zijn provider `exec`: het roept een script aan dat
`opi.fundament_registration.dns_hook` start, en dat zet of verwijdert het TXT-record
`_acme-challenge.<team>` via de bestaande TransIP-connector. De eigen TransIP-provider van
lego kan niet: die vraagt een accountnaam plus private key, en voor deze zone hebben we een
access-token.

- Het token gaat via de **omgeving** van het lego-proces naar het script, nooit als argument:
  een argument is leesbaar in `ps`.
- De hook is een **eigen proces**: alleen wat in die omgeving staat bereikt hem, en dat is het
  token plus de zone. Daarom weigert hij ook alleen op die twee. De vijf andere instellingen
  van het endpoint reizen niet mee, dus een controle daarop zou elke uitgifte laten stranden.
- De werkmap van lego staat onder `SECRET_TEMP_DIR` (een tmpfs in de pod) en gaat altijd weg,
  ook na een fout. De privésleutel staat er maar even.
- Er is **één ACME-account** voor alle aanvragen: de accountbestanden van lego gaan
  versleuteld in de tabel `fundament_acme_accounts` en worden rond elke run teruggezet. Let's
  Encrypt staat maar tien nieuwe accounts per IP per drie uur toe.
- Het hele lego-proces heeft een harde timeout van 8 minuten, zodat een hangende aanvraag
  binnen de 10 minuten van het contract eindigt.

### Wat er versleuteld staat

De privésleutel van het certificaat gaat met de AGE-sleutel van de installatie
(`SOPS_AGE_PUBLIC_KEY`) als `base64+age:` in de tabel, net als de accountbestanden van lego.
Een back-up van de database draagt dus geen bruikbare sleutel. Het certificaat zelf staat
plat in de tabel: dat is een publiek gegeven.

Logregels noemen team, cluster-ID, IP, toestand en uitkomst. Een certificaat, sleutel, token,
client-secret of de code komt er nooit in, ook niet via de tekst van een fout van buiten:
die gaat eerst door `redact_text` en daarna langs de twee geheimen van deze aanvraag zelf.

## Instellingen

Allemaal optioneel, want zonder de eerste bestaat de functie niet.

| Instelling | Geheim | Standaard | Wat |
|---|---|---|---|
| `FUNDAMENT_REGISTRATION_CODE` | ja | leeg | de fieldlab-code; leeg betekent uit. Het enige bewijsmiddel van dit endpoint, dus lang en willekeurig: `pwgen -s 32 1` |
| `FUNDAMENT_REGISTRATION_TRANSIP_TOKEN` | ja | leeg | access-token (TransIP API v6) van het account waar de zone in zit, een ander account dan `TRANSIP_ACCOUNT_NAME` |
| `FUNDAMENT_REGISTRATION_SSO_CLIENT_ID` | nee | leeg | de fieldlab-client in de realm |
| `FUNDAMENT_REGISTRATION_SSO_CLIENT_SECRET` | ja | leeg | het secret van die client |
| `FUNDAMENT_REGISTRATION_SSO_DISCOVERY_URL` | nee | leeg | de OIDC-discovery van die realm |
| `FUNDAMENT_REGISTRATION_ZONE` | nee | `zadlab.nl` | de zone waarin de namen komen |
| `FUNDAMENT_REGISTRATION_ACME_DIRECTORY` | nee | de **staging** van Let's Encrypt | voor het fieldlab de echte |
| `FUNDAMENT_REGISTRATION_ACME_EMAIL` | nee | leeg | contactadres van het ACME-account |
| `FUNDAMENT_REGISTRATION_VLAM_PREAUTH_KEY` | ja | leeg | pre-auth key van VLAM; de schakelaar van het `vlam`-veld |
| `FUNDAMENT_REGISTRATION_VLAM_LOGIN_SERVER` | nee | `https://vonk.rijksapp.dev` | de login-server in het `vlam`-veld |
| `FUNDAMENT_REGISTRATION_VLAM_GATEWAY` | nee | `100.80.0.1:443` | de gateway in het `vlam`-veld |
| `FUNDAMENT_REGISTRATION_VLAM_API_HOST` | nee | `vlam-api.overheid-i.nl` | de API-host in het `vlam`-veld |

Staat de code aan maar ontbreekt een van de andere instellingen, dan logt OPI dat bij het
opstarten en antwoordt het endpoint 503 met de **naam** van de ontbrekende instelling, nooit
met een waarde. De vijf `VLAM`-instellingen vallen daar buiten: die zijn echt optioneel, en
zonder de twee geheimen ontbreekt alleen het `vlam`-veld in het antwoord.

De geheimen horen in het SOPS-env-secret van het cluster, de rest in de configmap van de
OPI-overlay. Deze repo zet ze niet: zolang ze niet gezet zijn, bestaat de functie niet.

Zet je de geheimen in `operations-manager-env-secrets.yaml` van een overlay, voeg hun namen
dan toe aan `PLATFORM_SECRET_NAMES` (`opi/core/subprocess_env.py`): anders erft elk git-,
kubectl- en sops-kindproces het fieldlab-token mee, en valt
`tests/test_platformsleutel_als_bestand.py` om.

### Waar de code op staat

De code is de hele toegangscontrole: wie hem heeft, krijgt een wildcardsleutel in de zone en
het client-secret van de Keycloak-client. Een foute code telt mee voor
`AUTH_FAILURE_LIMIT_PER_MINUTE` (standaard 10 per minuut per client-id, 30 per IP omdat de
tweede emmer van een limiter op drie keer de snelheid loopt, en 10 per gepresenteerde code),
wat één bron afremt maar niet een verdeelde gokker. Neem daarom een waarde met echte
entropie en niet de korte vorm uit het contractvoorbeeld hierboven; die staat er om de vorm
te laten zien.

Binnen die code scheidt alleen `cluster_id` de teams: wie de code heeft en het `cluster_id`
van een team kent, krijgt het certificaat met sleutel van dat team en kan zijn A-record
verzetten. De installer stuurt daar `FUNDAMENT_CLUSTER_ID`, een UUID, dus behandel die als
een geheim van de installatie zolang het fieldlab loopt.

## Aanzetten en toetsen

```bash
# Op de sandbox, zonder TransIP of Let's Encrypt erbij: de route bestaat en weigert.
kubectl -n rig-system set env deploy/operations-manager FUNDAMENT_REGISTRATION_CODE=proef-code
kubectl -n rig-system rollout status deploy/operations-manager --timeout=180s

# 503 met de naam van de eerste instelling die nog ontbreekt.
curl -s -X POST https://zad.sandbox.rijksapp.dev/api/fundament/registreer \
  -H 'Content-Type: application/json' \
  -d '{"code":"proef-code","team":"team42","cluster_id":"proef-1","ip":"194.135.48.131"}'

# 401 bij een foute code, 400 bij een privé-adres.
curl -s -o /dev/null -w '%{http_code}\n' -X POST .../api/fundament/registreer \
  -H 'Content-Type: application/json' \
  -d '{"code":"fout","team":"team42","cluster_id":"proef-1","ip":"194.135.48.131"}'
curl -s -X POST .../api/fundament/registreer -H 'Content-Type: application/json' \
  -d '{"code":"proef-code","team":"team42","cluster_id":"proef-1","ip":"10.0.0.1"}'
```

Een echte uitgifte toets je alleen tegen de ACME-**staging** (de standaardwaarde) en met een
TransIP-token dat op die zone mag schrijven, vanaf een bron die op de whitelist van dat
account staat.

De toetsen in de suite:

| Bestand | Wat het meet |
|---|---|
| `tests/test_fundament_registratie_contract.py` | elke rij van het contract, de veldregels, de schakelaar, de limiet en het optionele `vlam`-veld |
| `tests/test_fundament_registratie_aanvraag.py` | de aanvraag: DNS, certificaat, wat er versleuteld staat, en wat niet in de log komt |
| `tests/test_fundament_lego_aanroep.py` | de lego-aanroep: argumenten, omgeving, hook, account en werkmap |
| `tests/test_fundament_dns_hook.py` | de challenge-hook, ook als echt proces in de omgeving die lego hem geeft |
| `tests/test_fundament_store.py` | de opslag van het ACME-account: versleuteld, vervangbaar, per directory |
| `tests/test_transip_access_token_en_delete.py` | de twee toevoegingen aan de connector |
| `tests/test_migration_015_fundament_registrations.py` | de migratie, heen en terug |

## Grenzen

- **Geen vernieuwing.** Het fieldlab duurt drie dagen, een certificaat negentig. Een
  installatie die later een nieuw certificaat nodig heeft, moet het zelf regelen.
- **Eén client voor alle teams.** Geen Keycloak-client per team.
- **VLAM is alleen doorgeven.** Het endpoint deelt de pre-auth key uit, het maakt er geen
  aan en het praat zelf niet met VLAM.
- **Eén replica.** De achtergrondtaak draait in het OPI-proces. Met meerdere replica's blijft
  het veilig (de claim is een voorwaardelijke schrijfactie), maar dat is niet gemeten.
- De route bestaat alleen waar de code gezet is, en daardoor zien de repo-brede
  routegrendels (`tests/test_elke_route_heeft_een_herkenning.py`,
  `check_route_actions`) hem niet: in een testrun is de instelling leeg. Wat die grendels
  voor de andere routes doen, doen de toetsen hierboven voor deze: de code-controle, de
  limiet en de 404 zonder instelling staan daar.

## Weghalen na het fieldlab

1. `operations-manager/python/opi/fundament_registration/` weg.
2. De regel `register_fundament_registration(app)` en zijn import uit `opi/server.py`.
3. De dertien `FUNDAMENT_REGISTRATION_*`-instellingen uit `opi/core/config.py`, uit het
   env-secret en uit de configmap van staging en productie.
4. lego uit `operations-manager/Dockerfile` (de `ARG LEGO_*`-regels en de `RUN`-laag) en de
   regel `LEGO` uit `scripts/renovate_checksums.py`.
5. Een migratie die `fundament_registrations` en `fundament_acme_accounts` dropt.
6. De A-records van de teams in de zone, en de Keycloak-client van het fieldlab.
7. De zeven toetsbestanden uit de tabel hierboven, en dit document.

De twee toevoegingen aan `opi/connectors/transip.py` (inloggen met een access-token en
`delete_dns_entry`) mogen blijven: die staan op zichzelf.
