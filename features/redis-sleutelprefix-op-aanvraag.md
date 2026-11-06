# Redis zonder sleutelprefix is een aanvraag

Elke deployment die de gedeelde Redis gebruikt krijgt een eigen ACL-gebruiker die alleen bij
sleutels met het voorvoegsel `{deployment}-{project}:` kan. De projectinstelling
`acl-key-prefix: false` zet die beperking uit, en dan kan de gebruiker bij elke sleutel in de
gedeelde instantie. Sinds RC-338 geldt die stand pas na goedkeuring door een
platformbeheerder.

Bij een gedeelde datastore is meer dan de eigen partitie geen zelfbediening maar een
aanvraag met een beslisser (BIO2 8.22 en 8.03).

## Hoe het werkt

1. Een projectbeheerder zet **Sleutels beperken tot dit project** uit in de Redis-sectie van
   het projectformulier, of schrijft `acl-key-prefix: false` via de API.
2. Het opslaan legt de aanvraag vast (`ensure_approval_requests`) op
   `services/[redis]/config/acl-key-prefix-approval`, met `status: requested` en de
   gevraagde `value`.
3. Zolang de status niet `approved` is, houdt `redis_manager` het projectprefix. De
   applicatie werkt dus, maar binnen haar eigen sleutels. De projectpagina en de
   lees-endpoints melden dat via de gewone goedkeuringsmeldingen.
4. Een platformbeheerder keurt de aanvraag goed of af op de beheerpagina voor
   goedkeuringen, naast de domein- en dienstaanvragen. Elk oordeel komt in de `history`.
5. Na goedkeuring en een volgende verwerking krijgt de ACL-gebruiker geen keypattern meer.

Een goedkeuring geldt voor de stand waarvoor ze gold. Gaat `acl-key-prefix` terug naar
`true`, dan vervalt het oordeel en verdwijnt het blok, dus een hernieuwde opt-out begint
weer als `requested`. Een andere waarde is eveneens een nieuwe aanvraag. Het veld
`acl-key-prefix-approval` is platform-managed: geen enkele schrijfweg buiten de
goedkeuringsroute kan er `approved` of `denied` in zetten. Dat laatste geldt ook voor het
WEGHALEN: `DELETE .../services/redis/config/project` laat het blok staan, want een oordeel
van het platform is niet aan het project om op te ruimen. Zet het project daarna opnieuw
`acl-key-prefix: false`, dan geldt datzelfde oordeel weer, over precies de waarde waarvoor
het is gegeven.

## Welke commando's een projectuser mag

De ACL-regel is `+@all -@admin -@dangerous +info +client|setname` (RC-409). Een
sleutelpatroon beperkt WELKE sleutels een datacommando raakt en niet welke commando's er
zijn, dus met alleen `+@all` kon een projectuser op de gedeelde instantie `ACL LIST` (de
wachtwoordhashes van elk ander project), `ACL SETUSER`, `FLUSHALL`, `FLUSHDB`, `CONFIG`,
`DEBUG`, `KEYS`, `MONITOR` en `SHUTDOWN`. Gemeten met `ACL CAT admin` en `ACL CAT
dangerous` tegen Redis 7.4.11: die twee categorieen dekken dat samen.

`INFO` en `CLIENT SETNAME` zitten in `@dangerous` en gaan expliciet terug, want veel
clients sturen ze bij het verbinden. `PING` en `ECHO` staan in geen van beide categorieen
en hoeven dus niet terug. Wat @dangerous nog meeneemt en niet terugkomt: `KEYS` (gebruik
`SCAN`), `SORT`, `RESTORE`, `MIGRATE` en `SWAPDB`.

De regel wordt bij ELKE verwerking van de deployment opnieuw gezet, met hetzelfde
wachtwoord: `ACL SETUSER` is idempotent, en zo komt een user die onder een oudere regel is
aangemaakt mee zonder dat iemand hem hoeft te vinden. Een vergelijking op de commandoset
staat er daarom bewust niet.

## Er is geen weg zonder aanvraag

Kan je applicatie haar sleutels niet voorvoegen, dan is de aanvraag de enige weg. Een eigen
Redis per project bestaat nog niet.

## Bestaande projecten

De projecten die de opt-out al droegen zijn de reden dat de instelling bestaat. Schemaversie
2.10 (`approve_existing_acl_key_prefix_optout` in `opi/services/schema_migration.py`) neemt
hun stand over als `approved`, met `by: schema-migration` in de history. Een migratie draait
een keer per bestand, dus een opt-out die daarna wordt gezet doorloopt de gewone aanvraag.

## Waar het staat

| Wat | Waar |
|---|---|
| De goedkeuring | `opi/services/catalog/redis/__init__.py` (`APPROVAL`) |
| De vorm | `config_value_approval` in `opi/services/catalog/approval.py` |
| Het configmodel | `opi/services/catalog/redis/config_model.py` |
| De poort | `opi/manager/redis_manager.py`, via `is_acl_key_prefix_approved` |
| De migratie | `opi/services/schema_migration.py` (v2.9 -> 2.10) |

Zie ook `docs/development/services.md`, sectie Approvals, en
`features/impliciete-dienstselectie.md`.
