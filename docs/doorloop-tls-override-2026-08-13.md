# Doorloop: de TLS-override per deployment-component

Datum: 13 augustus 2026. Taak RC-96, tak `de-tls-override-per-deployment-component-doorlopen`.
Gemeten op de sandbox (Kind-cluster `rig-sandbox` op de dev-server), met de Operations
Manager op de commit van deze tak (`sandbox-deploy` controleert `/version` tegen de build).

Aanleiding: RC-78 bouwde de override en toetste hem op het model, de haak en het gerenderde
sjabloon. RC-89 liep er op de sandbox een pad doorheen (`passthrough`, gemeten aan een
annotatie op het ingress). Wat in geen van beide zat is het bewijs dat een client werkelijk
een ander certificaat aangeboden krijgt -- en bij een certificaat is dat het enige dat telt.

**Oordeel: het vermogen doet wat het belooft.** Alle zeven punten hebben een gemeten
uitkomst, twee opeenvolgende volledige runs waren groen (4m35 en 4m54). Eén punt levert een
andere uitkomst op dan het plan veronderstelde: er is voor deze laag geen API-weg (punt 6).
Dat is geen defect maar een ontbrekend vermogen, en het staat hieronder als bevinding.

## Hoe er gemeten is

De vangrail staat in `operations-manager/python/tests/e2e/test_sandbox_tls_override.py`
(3 tests, `-m "e2e and sandbox"`). Hij maakt een project met `publish-on-web` en
`attachments`, zet er een tweede deployment (`staging`) naast, en meet per punt op de plek
die het antwoord heeft:

- het **projectbestand** in Forgejo -- staat de override op de deployment-component-laag
  (`deployments[i]/components[j]/services/publish-on-web/config`) en niet op het component;
- het **ingress-object** en het **tls-secret** op het cluster (`kubectl`);
- het **certificaat op de verbinding**: een echte TLS-handshake met SNI per hostnaam
  (`openssl s_client`), en dat is het bewijs -- de rest is de bedoeling.

Het eigen certificaat is een zelfondertekend certificaat met `CN = rc96-doorloop-eigen-certificaat`,
tijdens de run gemaakt en via `POST /api/v2/projects/{p}/services/attachments/attachment` in
de projectcatalogus gezet. Het platformcertificaat van de sandbox is het echte Let's
Encrypt-wildcard voor `*.sandbox.rijksapp.dev`; de twee zijn op de verbinding dus zonder
twijfel uit elkaar te houden.

## Bevinding vooraf: meet de handshake op de poort van de ingress, niet op 443

Op de gedeelde dev-server luistert Caddy op 443 en publiceert de Kind-container de ingress
op **8843** (`docs/sandbox-on-dev-server.md` noemt 8443 als voorbeeld; de draaiende
container zegt 8843). Caddy termineert TLS zelf, met hetzelfde wildcard-certificaat. Een
meting op 443 levert daardoor voor **elke** hostnaam het platformcertificaat op -- ook voor
een deployment die aantoonbaar zijn eigen certificaat aanbiedt.

De eerste volledige run liep daar precies op vast: projectbestand, ingress én secret klopten
alle drie, en de "verbinding" zei platform. Dezelfde meting op 8843 gaf meteen het eigen
certificaat. Dat is geen fout in het product maar in de meetplek, en het is dezelfde soort
fout die deze week vaker is gemaakt: een laag meten die het antwoord niet heeft.

De test kiest de poort daarom zelf (`_TLS_KANDIDATEN` = 8843, 8443, 443, in die volgorde --
de Kind-poorten eerst, want een proxy ervoor zou het antwoord vervangen), met
`E2E_TLS_ENDPOINT` te overrulen.

## De zeven punten

### 1. Leeg laten verandert niets -- geslaagd

Zonder override staat er niets in het projectbestand op de deployment-component-laag, en
beide deployments krijgen het platformcertificaat:

```
web-productie-<p>.sandbox.rijksapp.dev: CN = *.sandbox.rijksapp.dev, Let's Encrypt E8
web-staging-<p>.sandbox.rijksapp.dev  : CN = *.sandbox.rijksapp.dev, Let's Encrypt E8
```

Op het scherm is het ook te zien: de eerste keuze in de TLS-lijst van de deploymentmodal is
leeg en heet *"Erven van het component: Standaard certificaat (platform regelt het)"* -- hij
noemt de modus die geërfd wordt, dus "leeg" is niet met "geen TLS" te verwarren. De test
toetst dat het geërfde deel van dat label letterlijk één van de andere keuzes is, zodat de
poort blijft werken als de bewoording verandert.

### 2. Een eigen certificaat naast het platformcertificaat -- geslaagd

Override op `staging` (`tls: provided`, `attachment: doorloop-cert`) via
`modal-edit-deployment-<n>`. Daarna, op de verbinding:

```
web-staging-<p>   (override provided): CN = rc96-doorloop-eigen-certificaat  (zelfondertekend)
web-productie-<p> (geen override)    : CN = *.sandbox.rijksapp.dev, Let's Encrypt E8
```

De keten ertussen is meegemeten: het projectbestand draagt de override alleen bij `staging`,
het ingress `staging-web` wijst naar secret `staging-web-provided-tls`, en dat secret draagt
werkelijk het aangeleverde certificaat. Die tussenstap staat er expres in: een ingress dat
naar een ontbrekend secret wijst valt stil terug op het standaardcertificaat van de
ingress-controller, en dat is van buiten niet van "de override deed niets" te onderscheiden.

### 3. `provided` uitzetten met een override -- geslaagd

Eerst het component zelf op `provided` gezet (via de component-config-route van de API).
Productie volgt dat en biedt vanaf dan het eigen certificaat aan. Daarna op `staging` de
override op `standard` gezet, op de draaiende deployment:

```
web-staging-<p>   (override standard): CN = *.sandbox.rijksapp.dev, Let's Encrypt E8
web-productie-<p> (volgt component)  : CN = rc96-doorloop-eigen-certificaat
```

Een override zet `provided` dus werkelijk uit, en raakt de andere deployment niet. Dit
bevestigt op het cluster wat RC-78 in de cascade had gemeten: de override vervángt het
configblok van het niveau eronder, hij vult het niet aan.

### 4. `provided` zonder attachment -- geslaagd

`PUT /api/v2/projects/{p}/services/publish-on-web/config/component/web` met `{"tls": "provided"}`:

```
422 {"detail":[{"type":"value_error","loc":["body"],
     "msg":"Value error, tls 'provided' requires an 'attachment' naming the certificate", ...}]}
```

De melding noemt wat er ontbreekt en waarvoor het dient, en wijst niet naar "het
projectbestand is ongeldig". De test bewaakt beide kanten: het woord `attachment` (of
`certificaat`) moet erin staan, het woord `projectbestand` niet.

Wel Engelstalig, terwijl de weigering van punt 5 hieronder Nederlands is. Kleine
inconsistentie, geen defect; genoteerd, niet stilzwijgend veranderd.

### 5. De bijlage is projectbreed -- geslaagd

Met alleen de override van `staging` op het certificaat:

```
DELETE .../attachments/attachment/doorloop-cert
409 {"detail":"Bijlage 'doorloop-cert' wordt als certificaat gebruikt door: web (staging).
     Wijzig eerst de TLS-modus daar; een certificaat kan niet zomaar worden losgekoppeld.",
     "used_by":[{"component":"web","deployment":"staging","kind":"certificate","label":"web (staging)"}]}
```

Met `confirm_in_use=true` erbij: **eveneens 409, met dezelfde melding.** Dat is de bedoeling
-- een site van zijn certificaat halen is een besluit en geen bijwerking van het opruimen
van een bestand.

De override telt dus mee in de verwijdercontrole, inclusief de deployment waar hij staat.
Wanneer er twee plekken naar hetzelfde certificaat wijzen (het component én de override van
één deployment) noemt de weigering ze allebei, met `deployment: null` voor de component-laag
en `deployment: staging` voor de override. Dat is dezelfde walk die
`validate_attachment_references` gebruikt, zodat de controle en de validatie niet uit elkaar
kunnen lopen; de twee-plekken-vorm staat ook als eenheidstest vast
(`test_component_and_override_pointing_at_one_certificate_are_both_reported`).

### 6. Via de UI en via de API -- bevinding: de API-weg bestaat niet

De UI-weg werkt: `modal-edit-deployment-<n>` biedt per component een TLS-keuze en, zodra
`provided` gekozen is, een certificaatveld; beide punten hierboven zijn langs die weg gezet.

De API-weg is er niet. De generieke config-routes worden per laag gegenereerd uit
`_CONFIG_WRITE_LAYERS` (`opi/api/v2/router.py`), en dat zijn project, component en
deployment -- de deployment-component-laag zit er niet in. In de OpenAPI van de draaiende
sandbox bestaat er dan ook geen pad voor. De API zegt dat overigens zelf, en dat is de kant
die wél goed is: `GET /api/v2/services/publish-on-web` meldt voor die laag
`config_endpoint: null` met `has_form: true`. Een klant die op de zelfbeschrijving afgaat
wordt correct voorgelicht.

Het plan ging ervan uit dat "de API-weg dezelfde configuratie is". Dat klopt voor de
component- en de deployment-laag, niet voor deze. Of die route er moet komen is een besluit
(de zad-cli kan de override nu niet zetten), en dus is er hier niets stilzwijgend
gerepareerd. De test legt beide kanten vast, zodat hij gaat rammelen zodra de route er komt.

### 7. Herverwerken -- geslaagd

`POST /api/v2/projects/{p}/:refresh` (het hele projectbestand opnieuw verwerken):

```
web-productie-<p> (component provided) : CN = rc96-doorloop-eigen-certificaat
web-staging-<p>   (override standard)  : CN = *.sandbox.rijksapp.dev, Let's Encrypt E8
```

Het ingress van productie wijst na het herverwerken nog steeds naar
`productie-web-provided-tls`. Beide kanten zijn gemeten: het herverwerken levert het eigen
certificaat opnieuw op, én het valt niet terug op het component daar waar de override het
juist uitzet.

## Sluitstukken uit de toets

- Het projectbestand valideert na afloop (`validate_project_schema`), en
  `validate_attachment_references` geeft niets terug: er staat nergens een verwijzing naar
  een bijlage die niet bestaat. Dit staat als laatste stap in de test zelf.
- Alle metingen komen van de verbinding, het cluster of het projectbestand -- niet uit wat
  het formulier zei.

## Wat een volgende doorloop moet weten

1. **De meetpoort.** 8843 op de dev-server, niet 443. Zie de bevinding hierboven; dit is de
   valkuil die de eerste run kostte.
2. **Het certificaatveld op de modal her-rendert niet.** De TLS-keuze wel (het
   certificaatveld hangt ervan af), het certificaatveld zelf niet -- dat is het einde van de
   keten. `select_with_rerender` loopt daar in een timeout; gewoon `select_option`.
3. **De doorloop past ruim in een lease.** Vier tot vijf minuten voor alle zeven punten,
   inclusief het aanmaken en opruimen van het project. De uitrol per wijziging duurt op de
   sandbox tientallen seconden, niet minuten.
