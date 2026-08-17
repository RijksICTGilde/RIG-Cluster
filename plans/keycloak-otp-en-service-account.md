# Keycloak: service-account voor OPI, en OTP op realm-admins

**Eindproduct:** OPI authenticeert met een eigen client-credentials service-account in plaats van het gedeelde adminwachtwoord, en de realm-admins van projecten krijgen OTP, ook de realms die al bestaan.

Dit is afgesplitst van `plans/otp-en-verhoogde-rechten.md`, dat over OTP op de ZAD-gebruiker en verhoogde rechten gaat. Die twee hangen samen maar zijn los te bouwen, en dit deel is de voorwaarde: zolang OPI met het gedeelde adminwachtwoord inlogt, sluit elke OTP op dat account OPI buiten.

**Alle beslissingen zijn genomen op 2 augustus.** Waar de tekst een voorstel doet, is dat vastgesteld beleid tenzij het expliciet als open staat.

Alle paden zijn relatief aan `operations-manager/python/` tenzij ze met `instructions/`, `features/`, `plans/` of `opi/schemas/` beginnen.

---

## 1. Waar je op bouwt

Er ligt werkende code, veiliggesteld op 2 augustus in commit `9b1d8c75` op branch `claude/keycloak-realm-admin-otp`, nadat het daar ruim een maand ongecommit in een worktree stond. Het bevat 937 regels: `opi/utils/totp.py`, drie testbestanden, twee feature-documenten, en wijzigingen in `keycloak_setup.py`, `connectors/keycloak.py`, `core/config.py`, `core/startup.py`, `keycloak_manager.py`, het projectschema, de detailpagina en `web/router.py`.

**Die branch loopt 255 commits achter op main en moet niet gemerged worden.** Neem `opi/utils/totp.py` en de twee feature-documenten over, en pas de rest opnieuw toe op de huidige code. `connectors/keycloak.py` en `manager/keycloak_manager.py` zijn op 1 augustus gewijzigd voor de zelfbedienings-fix (`set_required_action_enabled`, `remove_default_role`, `_ensure_realm_self_service`), en daarna nog door RC-12 tot en met RC-15.

Let op wat `totp.py` wél en niet kan: het genereert een zaad, maakt er een Base32-vorm en een otpauth-URI van, en bouwt een Keycloak-credentialrepresentatie. Het **verifieert geen codes**, en dat hoeft hier ook niet, want Keycloak doet de verificatie. Voor OTP op de ZAD-gebruiker (het andere plan) is dat wel nodig.

## 2. Het ontwerp


Dit hoort bij hetzelfde traject maar gaat over Keycloak-accounts, niet over ZAD-gebruikers. Het bouwt op het werk dat op 2 augustus is veiliggesteld in `9b1d8c75` (branch `claude/keycloak-realm-admin-otp`), dat 255 commits achterloopt en dus opnieuw toegepast moet worden en niet gemerged.

### Wat OTP hier precies is

Hetzelfde als bij een gewone Keycloak-gebruiker: een TOTP-zaad. Normaal genereert Keycloak dat, toont een QR, en daarna staat het in de Keycloak-database en in de app van die ene persoon. Het bewaarde ontwerp draait dat om voor de realm-admins, en alleen omdat dat **gedeelde** accounts zijn: OPI genereert het zaad, duwt het als OTP-credential in Keycloak (`build_credential_representation`), en bewaart het AGE-versleuteld in het projectbestand naast het adminwachtwoord. Zonder die omkering zou de eerste die de QR scant de enige zijn die kan inloggen.

### Waar welk geheim hoort

| Identiteit | Zaad | Toelichting |
|---|---|---|
| OPI zelf | geen OTP | Machine-identiteit met client-credentials; een tweede factor is zinloos voor een proces. |
| Realm-admin per project (gedeeld) | Projectbestand, AGE met de projectsleutel | Gedeeld account, dus het zaad moet deelbaar zijn. |
| Gedeeld master-`admin` (break-glass) | Zelfde weg als het wachtwoord: cluster-secret, AGE-versleuteld in git | Consistent met hoe alle wachtwoorden vandaag bewaard worden. |
| Eigen master-account per beheerder | Niets op te slaan | Keycloak genereert, de beheerder scant de QR; het zaad staat alleen in Keycloak en op zijn telefoon. |

**De twee OTP's in dit plan hebben elk hun eigen doel, en dat hoort in de feature-documentatie zodat niemand ze verwisselt.**

De OTP *in Keycloak* beschermt de Keycloak-console: hij maakt een uitgelekt of doorgestuurd adminwachtwoord waardeloos en blokkeert brute force op het inlogscherm. Dat is het doel, en daarvoor is het genoeg dat het zaad op dezelfde manier bewaard wordt als de andere geheimen. Het is uitdrukkelijk niet bedoeld als bescherming tegen iemand die al bij de secret-opslag kan; dat is een ander dreigingsbeeld en daar hoort een andere maatregel bij.

De OTP *in ZAD*, op de gebruikerstabel, is de echte tweede factor. Die hangt aan een persoon en niet aan een gedeeld account, en daar hangen de verhoogde rechten uit `plans/otp-en-verhoogde-rechten.md` aan. Dat is waar "je doet dit bewust en je bent het echt" wordt afgedwongen.

Het einddoel waar ook de Keycloak-kant een persoonsgebonden factor krijgt is de laatste rij van de tabel: een eigen master-account per beheerder, waar Keycloak zelf het zaad genereert en er niets gedeeld bewaard wordt. Het service-account maakt dat mogelijk, want daarna is het gedeelde `admin`-account geen dagelijks werkpaard meer.

### Het service-account is de voorwaarde

OPI logt vandaag in met het gedeelde `admin`-wachtwoord (`create_keycloak_connector`, `connectors/keycloak.py:3976`, 27 aanroepplekken). Zolang dat zo is kun je op dat account geen OTP afdwingen zonder OPI buiten te sluiten. Het bewaarde ontwerp lost dat op met een confidential client in de master-realm met de master `admin`-rol, waarmee OPI via client-credentials op het token-endpoint authenticeert.

De zelfbootstrap voorkomt een kip-ei op een vers cluster: is `KEYCLOAK_ADMIN_CLIENT_SECRET` leeg, dan verandert er niets; werkt client-credentials al, dan gebruikt hij dat; anders gebruikt hij het adminwachtwoord één keer om de client te maken en zichzelf de rol te geven.

Dat het één factory is, is hier de winst: de authenticatie verandert op één plek en de 27 aanroepers merken er niets van.

### Bestaande realms

Het bewaarde werk heeft hier al een antwoord voor: `_ensure_admin_otp(...)`, omschreven als idempotente retrofit, aangeroepen in de tak voor een realm die al bestaat, naast `_ensure_realm_clients` en `_create_additional_clients`. Dat is dezelfde plek waar op 1 augustus `_ensure_realm_self_service` is neergezet, om precies hetzelfde probleem: `create_realm()` draait alleen bij een nieuwe realm, en daarom heeft de identity-field-lock nooit een bestaande realm geraakt. Die twee komen dus naast elkaar te staan.

### Volgorde, en waarom die zo is

1. **Service-account.** Los en niet-brekend zolang het secret leeg is. Voorwaarde voor al het andere, want zonder gescheiden machine-identiteit sluit OTP op een menselijk account OPI buiten.
2. **OTP op de realm-admins van projecten**, inclusief de retrofit voor bestaande realms.
3. **De master-realm:** eigen accounts per beheerder met OTP via de normale Keycloak-weg, en het gedeelde `admin`-account terug naar break-glass.

### De bootstrap

Dit is het lastigste stuk en het moet in één keer kloppen. De bootstrap genereert vandaag het adminwachtwoord (`infrastructure/bootstrap/infrastructure/secrets/templates/keycloak-admin-secret.yaml`, met `@secret-gen:random:16`) en OPI gebruikt het meteen. Na deze wijziging moet de bootstrap ook het clientsecret aanmaken en de volgorde bewaken: eerst de secrets, dan OPI die zichzelf één keer bootstrapt, en daarna nooit meer het wachtwoord gebruiken.

Wat daarbij niet vergeten mag worden: de bootstrap moet expliciet maken wat er daarna met dat adminwachtwoord gebeurt. Laat je dat impliciet, dan blijft het gewoon in de omgeving van OPI staan en is er niets gewonnen.

### Wat opnieuw toegepast moet worden

`connectors/keycloak.py` en `manager/keycloak_manager.py` zijn op 1 augustus gewijzigd voor de zelfbedienings-fix (`set_required_action_enabled`, `remove_default_role`, `_ensure_realm_self_service`). Het bewaarde werk raakt dezelfde bestanden. Neem `opi/utils/totp.py` en de feature-documenten over, en pas de rest opnieuw toe op de huidige code in plaats van de branch te mergen.


## 3. Guardrails

Na elke stap, en nog een keer voordat de PR opengaat:

```bash
cd operations-manager/python
uv run pytest tests/test_service_providers.py tests/test_service_config_schema.py \
              tests/test_golden_manifests.py tests/test_flow_registry_snapshot.py -q
uv run ruff check . --fix && uv run ruff format . && uv run pyright
uv run pytest tests/ -q
```

De volledige suite eindigt op nul failures en nul errors; die stond op 5029 groen toen dit plan geschreven werd.

Daarnaast de audit die in deze codebase alles beslecht: alle productiebestanden in `~/IdeaProjects/rig-cluster-test-git-repositories/rig-cluster-projects-github/projects` inlezen, `migrate_to_latest()` in memory, dan `validate_project_schema` en `validate_service_configs`. **Alleen lezen; die repo is productiedata.** Nul fouten na migratie, en de rauwe validatie moet op 22 blijven staan (dat zijn bestaande, niet door jou veroorzaakte fouten).

Sluit af met `instructions/service-review-checklist.md`, met bijzondere aandacht voor sectie 10 over logging: elke toestandswijziging een INFO-regel met wie, wat en voor welk project of realm, en een idempotente no-op logt niets. Nooit een zaad, een code of een wachtwoord in een logregel.

## 4. Wat succes is

- OPI draait op client-credentials, en het adminwachtwoord is niet meer nodig voor de dagelijkse werking.
- Een bestaande projectrealm krijgt bij een gewone reprocess zijn OTP-credential, zonder dat de realm opnieuw aangemaakt wordt.
- Het zaad is in het portaal zichtbaar als otpauth-URI, Base32 en QR, voor wie de rol daarvoor heeft.
- Met `KEYCLOAK_ENFORCE_ADMIN_OTP` uit verandert er niets, en dat is aantoonbaar met een test.
- De bootstrap regelt de volgorde in één keer, en maakt expliciet wat er daarna met het adminwachtwoord gebeurt.
- `features/keycloak-realm-admin-otp.md` en `features/opi-keycloak-service-account.md` staan bijgewerkt in de repo, inclusief de scheiding tussen de twee doelen uit sectie 2.
