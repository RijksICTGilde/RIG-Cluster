# De SOPS-sleutel vervangen, met een script dat het twee keer kan

Status: plan, 22 september 2026. Niet gebouwd. Vervangt de brede opzet in `de-age-sleutel-roteren-en-splitsen.md`, die als fase 3 blijft staan.

Aanleiding: de platform-AGE-sleutel is blootgesteld geweest. Deze taak levert de vervanging van sleutel A door sleutel B, en levert die als **gereedschap** op, niet als een reeks handmatige stappen. De volgende keer is het één commando.

**Toon.** Dit is onderhoud, geen incident. Commitberichten, branchnaam en PR-tekst beschrijven wat er gebeurt ("de sops-sleutel wordt vervangen"), niet waarom het urgent is. Geen verwijzing naar blootstelling, geen sleutelvormige strings in de tekst, ook niet als voorbeeld.

**Basis.** Deze taak bouwt op `main_github` en niet op `main`. Die branch staat op Forgejo en is identiek aan wat er nu op GitHub staat (`e999eb98a`), zodat de fix daar terecht kan komen zonder de 425 commits die nog niet gepubliceerd zijn.

## Wat de sleutel vasthoudt

Gemeten:

```
sleutel A  (security/key.txt = k8s secret `sops-age-key` = SOPS_AGE_KEY_CONTENT)
   |
   +-- 19 SOPS-bestanden in deze repo (alle echte; zie hieronder over de 2 andere)
   |
   +-- config.age-private-key van ELK project
          (age.py:486 ontsleutelt die met SOPS_AGE_PRIVATE_KEY)
          |
          +-- alles binnen dat project: Keycloak-wachtwoorden, api-key,
                user-env-vars, de age:base64-waarden
```

De twee overige `.sops.yaml`-bestanden staan in `sops-sandbox/` en horen bij een wegwerpsleutel die in diezelfde map ligt. Dat is een oefenmap met een DISCLAIMER, en een commit van 14 augustus legt vast dat hij weg mag. De echte sandboxsleutel (`security/sandbox-key.txt`) komt in geen enkel gecommit bestand voor. Het script moet dus op recipient werken en niet op "alle sops-bestanden", en `sops-sandbox/` kan beter verdwijnen dan meegenomen worden.

De sleutelwaarde zelf staat nergens in git: `bootstrap/.../deployment.yaml` verwijst met `secretKeyRef` naar het secret `sops-age-key`, en `sops-plugin.sh` leest datzelfde secret. Het is een bootstrapwaarde die alleen in Kubernetes leeft. De drie treffers op `AGE-SECRET-KEY` in de Taskfile en de plugin zijn vormcontroles, geen waarden.

## De volgorde, en waarom hij zo moet

De projectbestanden zijn de harde afhankelijkheid. Trek je A in voordat elk `config.age-private-key` is omgezet, dan kan OPI geen enkel project meer openen. Daarom:

```
1. B aanmaken
2. B TOEVOEGEN aan de 19 sops-bestanden   (A blijft geldig)
3. B uitdelen aan de drie consumenten      (A blijft geldig)
4. elk projectbestand: age-private-key van A naar B
5. A VERWIJDEREN uit de 19 sops-bestanden
6. A uit het cluster en van schijf
```

Tot stap 5 werkt alles met beide sleutels, dus er is geen moment waarop iets stukgaat. De winst komt pas bij stap 5, en dat is geen reden om stap 4 over te slaan.

## `updatekeys` is hier niet genoeg

Gemeten op een echt bestand. SOPS versleutelt de inhoud met een data key en versleutelt alleen die data key per recipient.

- `sops updatekeys` wisselt de recipients en laat de data key staan: de waarde bleef byte-voor-byte `ENC[AES256_GCM,data:r06ZMrQ=`.
- `sops rotate` maakt een nieuwe data key: dezelfde waarde werd `ENC[AES256_GCM,data:P/kVJgo=`.

Wie A ooit had, heeft de data key uit een oude kopie kunnen halen, en die opent een met `updatekeys` bijgewerkt bestand nog steeds. Het script gebruikt dus `sops rotate -i --add-age` en `--rm-age`.

## Wat er gebouwd moet worden

1. **`task rotate-sops-key`, droogloop als standaard.** Leest welke bestanden welke recipient dragen en meldt wat er zou gebeuren. Hij moet de sandboxsleutel met rust laten, dus hij werkt per recipient en niet op "alle sops-bestanden". *Verify:* de droogloop noemt 19 bestanden, niet 21, en wijzigt niets.
2. **Fase toevoegen: `--add-key B`.** `sops rotate -i --add-age B` over die 19. *Verify:* elk bestand is daarna met A én met B te ontsleutelen, en de ciphertext van de waarden is veranderd.
3. **De drie consumenten bijwerken.** Het secret `sops-age-key` in het cluster, de sops-plugin die ArgoCD gebruikt, en `security/key.txt` lokaal. Dit is de enige stap die het script niet alleen kan: het secret moet in het cluster komen. Lever hem als aparte taak met een controle achteraf (OPI leest een sops-bestand, ArgoCD rendert een applicatie). *Verify:* beide draaien op B terwijl A nog bestaat.
4. **`task rotate-project-keys`.** Loopt over de projectbestanden in de projects-repo, ontsleutelt `config.age-private-key` met A, versleutelt met B, en schrijft terug via het enige gevalideerde schrijfpad (`save_and_commit_project`). Idempotent: een project dat al om is, wordt overgeslagen. Per project een eigen commit, zodat een fout halverwege niet de hele repo raakt. *Verify:* een omgezet project is leesbaar met B, en een nog niet omgezet project blijft leesbaar met A.
5. **Fase verwijderen: `--remove-key A`.** `sops rotate -i --rm-age A`. Pas draaien als stap 4 over alle projecten klaar is; het script weigert als er nog projecten op A staan. *Verify:* geen bestand noemt de publieke sleutel van A meer, en ontsleutelen met A faalt.
6. **De vaste sleutels uit de tests.** Dit is niet één bestand. Een scan van de werkboom vindt er **vijf**:

   | bestand | sleutel |
   |---|---|
   | `tests/test_age_password_decryption.py` | **de productiesleutel** |
   | `tests/e2e/testserver.py` | een andere |
   | `tests/test_secret_file_keep_existing.py` | een andere |
   | `tests/test_sops_skip_unchanged.py` | twee andere |
   | `sops-sandbox/sops-key.txt` | de oefensleutel |

   Alleen de eerste is de echte, maar de andere vier zijn de reden dat hij niet opviel: een sleutel in een testbestand was hier normaal. Een scanner die op `AGE-SECRET-KEY-` alarmeert geeft in de huidige boom vier meldingen die niemand hoeft op te lossen, en wordt daarom genegeerd. **Eerst de boom schoon, dan pas de grendel**, anders bouw je een alarm waar iedereen omheen leert leven. Sleutels horen in een fixture die er ter plekke een maakt. *Verify:* een scan op de werkboom geeft nul treffers, en de tests slagen.
7. **Een grendel die dit structureel tegenhoudt.** Zie de eigen sectie hieronder; dit is meer dan een regel in een hook.

## Scanning: waarom een pre-commit hook hier niet volstaat

Er had nooit een commit met een secret gemaakt kunnen worden. Dat is de eis. De valkuil is dat de voor de hand liggende oplossing, een pre-commit hook, in dit project juist niet werkt: `--no-verify` is hier staande praktijk, omdat de pre-commit alles stasht wat unstaged is en daarmee andere sessies in dezelfde checkout omvergooit. Een grendel die met één vlag te omzeilen is, en die iedereen dagelijks omzeilt, is geen grendel.

Daarom drie lagen, waarvan alleen de tweede en derde bindend zijn:

**Laag 1, lokaal en vriendelijk.** Een pre-commit hook die scant en weigert. Vangt het eerlijke ongeluk en kost niets. Blijft omzeilbaar, en dat is geaccepteerd zolang laag 2 er is.

**Laag 2, CI, bindend.** Een scan op elke push en elke PR, die de bouw rood maakt. Dit is de laag die telt, want hij kent geen `--no-verify`. Hij scant niet alleen de diff maar de volledige boom van de branch, zodat iets dat via een omweg binnenkomt alsnog opvalt.

**Laag 3, op de server.** GitHub push protection op de publieke repo. Die weigert een push met een herkend token voordat het ooit publiek staat. Let op de grens: de standaardpatronen dekken bekende tokenvormen zoals een GitHub-PAT, maar een `AGE-SECRET-KEY-` zit daar niet bij. Dat vraagt een eigen patroon, en of dat beschikbaar is hangt af van het abonnement. Zoek dat uit voordat je erop vertrouwt.

**Wat gescand wordt.** Niet alleen `AGE-SECRET-KEY-`. Minimaal ook: GitHub-PAT's (`ghp_`, `github_pat_`), de `age:`-base64-vorm die ZAD zelf schrijft, privésleutels in PEM-vorm, en kubeconfig-tokens. Het patroon voor de eigen vormen moeten we zelf leveren, want geen enkele standaardscanner kent ze.

**De historie een keer uitkammen.** Een scanner op de huidige boom zegt niets over wat er in oude commits zit. Eén volledige scan over de historie van deze repo en van de projects-repo, met een lijst van wat er gevonden is. Dat is een meting en geen opruiming: wat eruit komt bepaalt of er nog meer geroteerd moet worden.

**Een tegenproef in de test.** Een test die een commit met een sleutelvormige string aanbiedt en verwacht dat hij geweigerd wordt. Zonder die test verslapt de grendel stil bij de eerstvolgende configuratiewijziging.

Issue #94 vraagt al om CI secret-scanning (gitleaks of trufflehog). Dat ticket is hiermee niet langer optioneel.

## Assertie

```bash
task rotate-sops-key -- --dry-run          # 19 bestanden, geen wijziging
task rotate-sops-key -- --add-key <B.pub>
SOPS_AGE_KEY="$(sed -n '3p' security/key-A.txt)" sops --decrypt <bestand>   # werkt
SOPS_AGE_KEY="$(sed -n '3p' security/key-B.txt)" sops --decrypt <bestand>   # werkt ook
task rotate-project-keys -- --dry-run      # noemt de projecten die nog op A staan
```

Klaar als:

- er een overlapfase bestaat waarin A en B allebei werken, aantoonbaar met de twee decrypts;
- het script weigert A te verwijderen zolang er projecten op A staan;
- een tweede keer draaien niets doet en dat meldt;
- de sandboxsleutel onaangeroerd is;
- geen enkele test nog een vaste sleutel bevat.

## Wat hierna komt, en nu bewust niet meegaat

- **De GitHub-PAT vervangen**, in de eigen configmap en in elk projectbestand. Die tokens zijn per project met de projectsleutel versleuteld, dus dat is een eigen ronde met een eigen script, ná deze.
- **De onderliggende wachtwoorden roteren** van de 19 secrets. Her-versleutelen maakt niet onbekend wat gelezen kon worden.
- **De sleutel splitsen** in een infradeel en een projectdeel, zodat de renderer naast ArgoCD niet langer elk projectgeheim kan openen. Zie `de-age-sleutel-roteren-en-splitsen.md`.
- **De uitkomst van de historie-scan.** Wat die oplevert bepaalt of er meer geroteerd moet worden dan nu voorzien.
- **De git-historie zelf.** Zolang die bestaat is elke oude versie met een oude sleutel te openen. Dat geldt voor deze repo en voor de projects-repo. Een sleutelwissel verandert dat niet, en dat hoort een bewuste beslissing te zijn en geen aanname.
