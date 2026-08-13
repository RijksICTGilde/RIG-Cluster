# Doorloop: de TLS-override per deployment-component

Datum: 13 augustus 2026. Taak RC-96, tak `de-tls-override-per-deployment-component-doorlopen`.
Gemeten op de sandbox (Kind-cluster `rig-sandbox` op de dev-server), met de Operations
Manager op de commit van deze tak (`/version` gecontroleerd door `sandbox-deploy`).

Aanleiding: RC-78 bouwde de override en toetste hem op het model, de haak en het
gerenderde sjabloon. RC-89 liep er op de sandbox een pad doorheen (`passthrough`, gemeten
aan een annotatie). Wat in geen van beide zat is het bewijs dat een client werkelijk een
ander certificaat aangeboden krijgt -- en bij een certificaat is dat het enige dat telt.

## Wat er gemeten is, en waarmee

De vangrail staat in `operations-manager/python/tests/e2e/test_sandbox_tls_override.py`
(3 tests, `-m "e2e and sandbox"`). Hij maakt een project met `publish-on-web` en
`attachments`, zet er een tweede deployment (`staging`) naast, en meet per punt op de
plek die het antwoord heeft:

- het **projectbestand** in Forgejo (staat de override op de deployment-component-laag);
- het **ingress-object** en het **secret** op het cluster (`kubectl`);
- het **certificaat op de verbinding**: een echte TLS-handshake met SNI per hostnaam
  (`openssl s_client`), en dat is het bewijs -- de rest is de bedoeling.

## De bevinding die de meting zelf betrof

**Meet de handshake op de poort van de ingress, niet op 443.** Op de gedeelde dev-server
luistert Caddy op 443 en staat Kind op **8843** (`docs/sandbox-on-dev-server.md` noemt
8443; de container publiceert 8843). Caddy termineert TLS zelf met hetzelfde
Let's Encrypt-wildcard. Een meting op 443 levert daardoor voor **elke** hostnaam het
platformcertificaat op -- ook voor een deployment die aantoonbaar zijn eigen certificaat
aanbiedt. De eerste run van deze doorloop liep daar precies op vast: bestand, ingress en
secret klopten alle drie, en de "verbinding" zei platform.

Dat is geen fout in het product maar in de meting, en het is dezelfde soort fout als die
deze week drie keer is gemaakt: een laag meten die het antwoord niet heeft. De test kiest
de poort daarom zelf (`_TLS_KANDIDATEN`: 8843, 8443, 443, in die volgorde, met
`E2E_TLS_ENDPOINT` te overrulen).

## De zeven punten

| # | Punt | Uitkomst |
|---|---|---|
| 1 | Leeg laten verandert niets | (in te vullen) |
| 2 | Eigen certificaat naast platformcertificaat | (in te vullen) |
| 3 | `provided` uitzetten met een override | (in te vullen) |
| 4 | `provided` zonder attachment | (in te vullen) |
| 5 | De bijlage is projectbreed | (in te vullen) |
| 6 | Via de UI en via de API | (in te vullen) |
| 7 | Herverwerken | (in te vullen) |

## Oordeel

(in te vullen)
