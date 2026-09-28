# Enterprise-sleutelbeheer voor de platformsleutels

Status: **plan, geen oplossing.** Opgetekend 28-09-2026 naar aanleiding van de vraag hoe dit
er uitziet als het niet "een tekstbestand op de machine van één developer" is. Niets hiervan
is gebouwd; het rotatiewerk in `plans/wachtwoorden-roteren-*.md` en `docs/wachtwoorden-roteren.md`
gebruikt vandaag de keyring (`scripts/age_keyring.py`) en verandert daar niet door.

## De zwakte van nu

Een AGE-sleutel is een identiteit zonder poortwachter: wie de string heeft, gebruikt hem,
ongezien en onintrekkelijk. `age_keyring.py` maakt de keuze welke sleutel bij welk bestand
hoort betrouwbaar en zichtbaar -- maar maakt misbruik niet moeilijker.

## Waar een organisatie dit naartoe zet

Envelope-encryptie met een centrale poortwachter: de master-sleutel leeft in een HSM/KMS of
HashiCorp Vault (Transit-engine) en verlaat die nooit; versleutelen en ontsleutelen zijn
geauthenticeerde, geauditeerde API-calls; intrekken is een policy-wijziging; roteren is een
nieuwe sleutelversie plus rewrap van de bestaande bestanden.

Verdeling naar lagen, omdat "alles achter hardware" niet kan:

| laag | wat | vorm |
|---|---|---|
| T0, boot/break-glass | de sleutel die de poortwachter zelf ontgrendelt, noodgeval-pad | Shamir m-van-n over officers, of YubiKey(s) in beheer; documenteerde ceremonie |
| T1, pipeline/platform | wat CMP/CI onbeheerd gebruikt | Vault Transit of KMS: nooit een bruikbare string; scoped tokens; audit |
| T2, persoonlijk | dev-toegang | eigen sleutel per persoon, herleidbaar en intrekbaar, eventueel op YubiKey |

Hardware (age-plugin-yubikey werkt gewoon met SOPS) hoort bij de menselijke laag: een
controller die om drie uur 's nachts rendert kan geen pin+touch doen.

## Relatie met dit cluster

- `infrastructure` kent al een vault-component (nu met bootstrap unseal keys — het klassieke
  kip-ei waar T0 voor is).
- KSOPS (de CMP-generator die onze `decrypt-sops.yaml` draait) kan vanaf versie 4 met Vault
  Transit praten: dezelfde SOPS-bestanden, maar ontsleutelen gebeurt als API-call met een
  scoped token in plaats van met een sleutel uit `sops-age-key`.
- Vault-OIDC kan aan de eigen Keycloak hangen, zodat toegang hetzelfde SSO-pad volgt als de
  rest van het platform.
- Open: of ODC-Noord een beheerde KMS aanbiedt is een vraag aan Quattro; zo ja, dan is dat
  simpeler dan zelf Vault beheren.

## Migratie-vorm als het zover komt

De keyring blijft de bron-laag voor break-glass en overgangen; Vault/KMS wordt er een bron
bij (of vervanger voor T1). De schrijfwaarborgen uit `secret_edit.py` (recipient-set behouden,
roundtrip-bewijs) en de drift-metingen uit de rotatieflow blijven onder elk beheer kloppen.
Roteerbaarheid blijft bestaan: Transit-rotatie maakt een nieuwe versie zonder de sleutel te
tonen; de rewrap daarna is precies de ronde die `rotate-sops-key.py` nu al rijdt.
