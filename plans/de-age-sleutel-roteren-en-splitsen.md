# De AGE-sleutel roteren, en hem daarna splitsen

Status: plan, 22 september 2026. Niet gebouwd.

Aanleiding: de private AGE-sleutel van het platform stond hardcoded in `operations-manager/python/tests/test_age_password_decryption.py`, sinds de eerste commit van 8 oktober 2025, in een repo die publiek is op GitHub. Bevestigd door de sleutel in dat bestand te vergelijken met `security/key.txt`: dezelfde. Dit gaat een keer opnieuw gebeuren, dus roteren moet een handeling worden die je uitvoert en niet een project.

Afbakening voor deze ronde, zoals besloten: **alleen opnieuw versleutelen, geen nieuwe onderliggende wachtwoorden.** Zie "Wat dit bewust niet oplost".

## Wat die ene sleutel vandaag opent

Gemeten, niet aangenomen:

```
platformsleutel  (security/key.txt = secret `sops-age-key` = SOPS_AGE_KEY_CONTENT)
   |
   +-- 19 SOPS-bestanden in DEZE repo, en die staan op GitHub
   |      argocd-admin, keycloak-admin, keycloak-mail, postgres-admin, minio-admin,
   |      redis-admin, transip (DNS), vault-init, mail-relay, mail-db, pgadmin,
   |      prometheus-metrics-auth, backup-destination, de argocd repo-secrets
   |
   +-- de private AGE-sleutel van ELK project
          (`get_decoded_project_private_key`, opi/utils/age.py:486, ontsleutelt met
           settings.SOPS_AGE_PRIVATE_KEY)
          |
          +-- per project: Keycloak-wachtwoorden, api-key, user-env-vars,
                 en de `age:base64...`-waarden in het projectbestand
```

Dat beantwoordt de vier vragen: ja voor de eigen SOPS-bestanden, ja voor de sleutel in projecten (en dat is de zwaarste, want het is een hoofdsleutel boven elke projectsleutel), ja voor de eigen env-secrets van OPI (ook een SOPS-bestand), en ja voor de base64-vorm (die versleutelt met de publieke sleutel die de aanroeper meegeeft, doorgaans die van het project).

Er zijn twee recipients in omloop: één in 19 bestanden (platform) en één in 2 (sandbox).

Wie de sleutel nodig heeft: de OPI-pod via het `sops-age-key` secret, de sops-plugin die ArgoCD bij het renderen gebruikt (`bootstrap/rig-system/kustomize/sops-plugin.sh`), en een ontwikkelaar lokaal voor `kustomize build`.

## Waarom splitsen, en waar de scheidslijn hoort

De renderer heeft vandaag meer macht dan hij nodig heeft. De sops-plugin naast ArgoCD moet de infrastructuurmanifesten kunnen ontsleutelen, en krijgt daarmee ook de sleutel waarmee elk projectgeheim te openen is. Dat zijn twee verschillende vertrouwensniveaus in één sleutel.

Voorgestelde scheidslijn, en dit is een voorstel en geen bestaande naam:

- **infrasleutel**: de SOPS-bestanden in deze repo (`bootstrap/`, `infrastructure/`). Nodig door de sops-plugin en door ontwikkelaars lokaal.
- **projectsleutel-sleutel**: ontsleutelt de per-project private keys. Nodig door OPI en door niemand anders.

Daarmee levert een lek aan de renderkant niet langer elk project op, en kan de projectkant roteren zonder dat ArgoCD iets merkt.

## `updatekeys` is hier niet genoeg, `rotate` wel

Dit is de valkuil van deze taak, en hij is gemeten. SOPS versleutelt de inhoud met een willekeurige data key, en versleutelt vervolgens alleen die data key voor elke recipient.

- `sops updatekeys` wisselt de **recipients** en laat de data key staan. De versleutelde waarden blijven byte-voor-byte identiek: gemeten `ENC[AES256_GCM,data:r06ZMrQ=` voor en na.
- `sops rotate` maakt een **nieuwe data key** en versleutelt alle waarden opnieuw: dezelfde waarde werd `ENC[AES256_GCM,data:P/kVJgo=`.

Na een lek is `updatekeys` daarom schijnveiligheid. Wie de oude private sleutel had, heeft de data key uit een oude kopie kunnen halen, en die data key opent de "nieuwe" bestanden net zo goed, want de ciphertext van de waarden is niet veranderd. Alleen `rotate` sluit dat af.

## Wat er moet gebeuren

De volgorde is de kern: zonder overlapfase valt er iets om terwijl je bezig bent.

1. **Maak roteren een taak, niet een handleiding.** `task rotate-age-key` met een droogloop als standaard. Hij leest welke bestanden een recipient dragen, en meldt wat hij zou doen. *Verify:* de droogloop noemt exact de 19 plus de 2 bestanden en wijzigt niets.
2. **Nieuwe sleutel als TWEEDE recipient toevoegen, oude laten staan.** Met `sops rotate -i --add-age <nieuwe publieke sleutel>`, NIET met `sops updatekeys`. Dat onderscheid is de kern van deze taak, zie het kader hieronder. In deze fase kan alles nog met beide sleutels open, dus niets valt om. *Verify:* elk aangeraakt bestand is met de oude EN met de nieuwe sleutel te ontsleutelen, en de ciphertext van de waarden is veranderd.
3. **De projectbestanden apart.** De per-project private key moet opnieuw versleuteld worden met de nieuwe platformsleutel. Dat is geen `sops updatekeys` maar een lees-ontsleutel-versleutel-schrijfronde door OPI zelf, per project, via het enige gevalideerde schrijfpad (`save_and_commit_project`). *Verify:* een project dat de ronde heeft gehad is nog steeds te lezen, en een dat hem niet had ook, want de oude sleutel is nog geldig.
4. **Distribueer de nieuwe sleutel naar de drie consumenten**: het `sops-age-key` secret, de sops-plugin, en `security/key.txt` voor lokaal gebruik. Pas hierna mag de oude weg. *Verify:* OPI en ArgoCD draaien op de nieuwe sleutel, aantoonbaar door de oude uit hun omgeving te halen en te zien dat er niets breekt.
5. **Verwijder de oude sleutel als recipient** met `sops rotate -i --rm-age <oude publieke sleutel>`, en pas dan uit het cluster. Hier komt de winst: tot dit moment kan de oude sleutel alles nog openen. *Verify:* geen enkel bestand noemt de oude publieke sleutel meer, en een ontsleutelpoging met de oude sleutel faalt.
6. **Pas daarna splitsen.** De splitsing is dezelfde beweging nog een keer, maar met twee doelsleutels in plaats van één. Doe hem NIET tegelijk met deze rotatie: dan weet je bij een fout niet of het aan de rotatie of aan de splitsing lag.
7. **Een guard die dit tegenhoudt.** Een pre-commit hook plus een CI-stap die een `AGE-SECRET-KEY-` in een diff weigert. Issue #94 vraagt hier al om (gitleaks of trufflehog). *Verify:* een testcommit met een sleutelvormige string wordt geweigerd.

## Assertie

```bash
task rotate-age-key -- --dry-run     # noemt de bestanden, wijzigt niets
task rotate-age-key -- --add-new     # fase 2: sops rotate --add-age, beide sleutels geldig
SOPS_AGE_KEY="$(sed -n '3p' security/key-oud.txt)" sops --decrypt <een bestand>   # werkt nog
SOPS_AGE_KEY="$(sed -n '3p' security/key-nieuw.txt)" sops --decrypt <hetzelfde>   # werkt ook
```

Klaar als:

- er een overlapfase bestaat waarin oud en nieuw allebei werken, en die aantoonbaar is met de twee decrypts hierboven;
- elk van de drie consumenten los is bijgewerkt en gecontroleerd voordat de oude sleutel verdwijnt;
- een projectbestand na de ronde leesbaar is voor OPI, getoetst op een echt project en niet alleen op een fixture;
- de rotatie herhaalbaar is: een tweede keer draaien op een al geroteerde repo doet niets en meldt dat.

## Wat dit bewust niet oplost

De 19 SOPS-bestanden staan op GitHub in een publieke repo, versleuteld met een sleutel die daar tot vandaag naast lag. Opnieuw versleutelen met een nieuwe sleutel maakt de oude inhoud niet onbekend: wie de repo eerder kloonde heeft de wachtwoorden zelf, niet alleen de ciphertext.

Deze ronde is dus hygiëne en herhaalbaarheid, geen herstel van vertrouwelijkheid. Het roteren van de onderliggende wachtwoorden (ArgoCD admin, Keycloak admin, Postgres, Redis, MinIO, de TransIP-token, Vault, de mailrelay) is een aparte beslissing die hier bewust buiten valt. Zolang die niet is genomen, moet je ervan uitgaan dat die waarden bekend zijn.

De projectbestanden staan in een private repo en zijn daarmee niet langs deze weg gelekt.
