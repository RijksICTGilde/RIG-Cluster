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
   |      (age.py:486 ontsleutelt die met SOPS_AGE_PRIVATE_KEY)
   |      |
   |      +-- alles binnen dat project: Keycloak-wachtwoorden, api-key,
   |            user-env-vars, de age:base64-waarden
   |
   +-- repositories[].password van ELK project
          de GitHub-PAT, in base64+age-vorm. Gemeten: git.py:134 gebruikt
          decrypt_password_smart_auto_sync, en die leest get_global_private_key(),
          dus de PLATFORMsleutel en NIET de sleutel van het project.
```

De twee overige `.sops.yaml`-bestanden staan in `sops-sandbox/` en horen bij een wegwerpsleutel die in diezelfde map ligt. Dat is een oefenmap met een DISCLAIMER, en een commit van 14 augustus legt vast dat hij weg mag. De echte sandboxsleutel (`security/sandbox-key.txt`) komt in geen enkel gecommit bestand voor. Het script moet dus op recipient werken en niet op "alle sops-bestanden", en `sops-sandbox/` kan beter verdwijnen dan meegenomen worden.

De sleutelwaarde zelf staat nergens in git: `bootstrap/.../deployment.yaml` verwijst met `secretKeyRef` naar het secret `sops-age-key`, en `sops-plugin.sh` leest datzelfde secret. Het is een bootstrapwaarde die alleen in Kubernetes leeft. De drie treffers op `AGE-SECRET-KEY` in de Taskfile en de plugin zijn vormcontroles, geen waarden.

## De cutover kan overlappend, en dat verandert alles

Gemeten: een age-blob kan **meerdere recipients** dragen. `age -r A -r B` levert iets op dat met A en met B te openen is, beide getoetst. Daarmee hoeft er geen moment te bestaan waarop alleen de nieuwe sleutel werkt.

Even belangrijk is wie de sleutel wanneer leest:

| consument | leest | bij een wissel |
|---|---|---|
| sops-plugin naast ArgoCD | `kubectl get secret` bij **elke render** | pakt de nieuwe vanzelf, geen herstart |
| operations-manager | env-var uit datzelfde secret | **pod moet herstarten** |
| ontwikkelaar lokaal | `security/key.txt` | bestand vervangen |

Dat geeft drie blokken, waarvan alleen het middelste kort is:

```
VOORBEREIDING   (dagen van tevoren mag, niets gaat stuk, alles blijft op A werken)
  1. B aanmaken in security/new.txt          (A staat in security/old.txt)
  2. de 19 sops-bestanden: sops rotate --add-age B     -> A en B werken
  3. elk projectbestand: age-private-key opnieuw versleutelen voor A EN B
  4. beide repos committen en pushen

CUTOVER         (seconden, en terugdraaibaar)
  5. het k8s secret sops-age-key vervangen door B
  6. de operations-manager herstarten
     (de sops-plugin pakt B bij zijn volgende render vanzelf op)

NAZORG          (later, rustig, pas als alles aantoonbaar op B draait)
  7. sops rotate --rm-age A over de 19 bestanden
  8. de projectbestanden: A als recipient eraf
  9. A uit het cluster, uit security/, en van elke laptop
```

Tot en met stap 6 is elke stap terug te draaien door het secret terug te zetten: alles is immers nog met A te openen. De onomkeerbaarheid begint pas bij 7, en daar is geen haast bij. Dat is precies de eigenschap die "het moet in één keer goed" wegneemt.

## Drie vormen, drie wegen, een script

Niet alles wat aan de platformsleutel hangt is een SOPS-bestand. `sops rotate` raakt alleen de eerste rij:

| vorm | waar | hoe om te zetten |
|---|---|---|
| SOPS-bestand | de 19 in `bootstrap/` en `infrastructure/` | `sops rotate -i --add-age` |
| `base64+age:` in een env-regel | `configmap.yaml` van odcn-production en local: `GIT_PROJECTS_SERVER_PASSWORD` en `GIT_ARGO_APPLICATIONS_PASSWORD` | ontsleutel, versleutel opnieuw, base64, terugschrijven |
| `base64+age:` in een projectbestand | per project `config.age-private-key` en `repositories[].password` | idem, via het gevalideerde schrijfpad |
| `base64+age:` in een env-bestand in git | `operations-manager/python/.env`: dezelfde twee GIT-wachtwoorden | idem; dit bestand wordt bij lokaal ontwikkelen echt geladen, dus zonder omzetting werkt dat niet meer zodra A eruit gaat |

De tweede rij is het gat dat je noemde. Een age-blok is meerregelig en past niet in een `KEY=value`-env-regel, vandaar de base64-omweg. Er is tooling om zo'n waarde te MAKEN (`task encrypt-value-age-base64`), maar niet om hem om te zetten. Dat recrypt-stuk moet er dus bij, en het is dezelfde lees-ontsleutel-versleutel-schrijf-lus als rij drie: bouw hem één keer en laat beide wegen hem gebruiken.

Let op het verschil tussen omzetten en opnieuw genereren. `task generate-env-secrets-for-operations-manager` leest `security/key.txt` en bouwt het SOPS-secret uit `operations-manager/python/.env.<cluster>.secrets`, een plaintext bron die lokaal staat en niet in git. Voor dat ene bestand is hernoemen van `newkey.txt` naar `key.txt` plus die taak draaien inderdaad genoeg, precies zoals je zei. Voor de configmap en de projectbestanden gaat dat niet op: daar is geen plaintext bron, alleen de ciphertext zelf, en die moet je dus echt recrypten.

## `updatekeys` is hier niet genoeg

Gemeten op een echt bestand. SOPS versleutelt de inhoud met een data key en versleutelt alleen die data key per recipient.

- `sops updatekeys` wisselt de recipients en laat de data key staan: de waarde bleef byte-voor-byte `ENC[AES256_GCM,data:r06ZMrQ=`.
- `sops rotate` maakt een nieuwe data key: dezelfde waarde werd `ENC[AES256_GCM,data:P/kVJgo=`.

Wie A ooit had, heeft de data key uit een oude kopie kunnen halen, en die opent een met `updatekeys` bijgewerkt bestand nog steeds. Het script gebruikt dus `sops rotate -i --add-age` en `--rm-age`.

## Wat er gebouwd moet worden

1. **Sleutels op schijf, in `security/`.** Die map is untracked en is al de plek waar `key.txt` en `sandbox-key.txt` staan. Het script vraagt naar de paden met een default (`security/old_key.txt` en `security/key.txt`) en neemt nooit een sleutel als argument, zodat er geen sleutel in shellgeschiedenis, procestabel of een logregel belandt. Ontbreekt er een, dan stopt het met het pad in de melding. Zie "De vorm van het gereedschap". *Verify:* een droogloop met een ontbrekend bestand weigert en noemt het pad.
2. **`task rotate-sops-key`, droogloop als standaard.** Zet de 19 SOPS-bestanden om EN elke `base64+age:`-waarde die buiten een SOPS-bestand staat: de twee configmaps en `operations-manager/python/.env`. Die worden anders vergeten, want `sops rotate` ziet ze niet. Leest welke bestanden welke recipient dragen en meldt wat er zou gebeuren. Hij moet de sandboxsleutel met rust laten, dus hij werkt per recipient en niet op "alle sops-bestanden". *Verify:* de droogloop noemt 19 bestanden, niet 21, en wijzigt niets.
3. **Fase toevoegen: `--add-key B`.** `sops rotate -i --add-age B` over die 19. *Verify:* elk bestand is daarna met A én met B te ontsleutelen, en de ciphertext van de waarden is veranderd.
4. **`task set-sops-key-secret`.** Zet de inhoud van `security/new.txt` in het secret `sops-age-key` van de doelnamespace, en herstart daarna de operations-manager zodat die zijn env-var opnieuw leest. De sops-plugin heeft geen herstart nodig. De taak vraagt om bevestiging met de clusternaam erin, want dit is de enige onomkeerbare handeling van de cutover. *Verify:* OPI leest na de herstart een sops-bestand, en ArgoCD rendert een applicatie zonder fout.
5. **`task rotate-project-keys`: de projectbestanden omzetten.** Dit is de grootste ronde: 45 bestanden in de projects-repo. Per bestand **twee** velden, niet één: `config.age-private-key` en `repositories[].password`. Allebei hangen ze aan de platformsleutel, en een project waarvan alleen het eerste is omgezet kan zijn eigen repository niet meer benaderen. In de voorbereidingsfase versleutelt hij voor A **en** B tegelijk, zodat oud en nieuw allebei werken. Schrijft terug via het enige gevalideerde schrijfpad (`save_and_commit_project`), idempotent, met een commit per project. *Verify:* een omgezet project is leesbaar met A en met B, en beide velden zijn meegegaan.
6. **Fase verwijderen: `--remove-key A`.** `sops rotate -i --rm-age A`. Pas draaien als stap 5 over alle projecten klaar is; het script weigert als er nog projecten op A staan. *Verify:* geen bestand noemt de publieke sleutel van A meer, en ontsleutelen met A faalt.
7. **De vaste sleutels uit de tests, en pas NA stap 6.** De volgorde is hier een besluit en geen detail. Zolang A nog geldig is, is het testbestand de enige plek die verraadt dat er iets te halen valt, en een losse commit die precies die regel weghaalt zet daar een pijl naar. De sleutel staat er al bijna een jaar, dus een paar dagen extra verandert niets aan de blootstelling; hem waardeloos maken wel. Dus: eerst roteren, A intrekken, en pas daarna opruimen, als onderdeel van een ronde die alle vijf de bestanden raakt en dus over "dezelfde sleutel voor alle tests" gaat in plaats van over één regel.

   Dit is niet één bestand. Een scan van de werkboom vindt er **vijf**:

   | bestand | sleutel |
   |---|---|
   | `tests/test_age_password_decryption.py` | **de productiesleutel** |
   | `tests/e2e/testserver.py` | een andere |
   | `tests/test_secret_file_keep_existing.py` | een andere |
   | `tests/test_sops_skip_unchanged.py` | twee andere |
   | `sops-sandbox/sops-key.txt` | de oefensleutel |

   Alleen de eerste is de echte, en die is op dat moment al vervangen. De andere vier zijn de reden dat hij niet opviel: een sleutel in een testbestand was hier normaal. Een scanner die op `AGE-SECRET-KEY-` alarmeert geeft in de huidige boom vier meldingen die niemand hoeft op te lossen, en wordt daarom genegeerd. **Eerst de boom schoon, dan pas de grendel**, anders bouw je een alarm waar iedereen omheen leert leven. Sleutels horen in een fixture die er ter plekke een maakt. *Verify:* een scan op de werkboom geeft nul treffers, en de tests slagen.
8. **De PAT-vervanging is dezelfde lus met een andere ingang.** Er is precies één verschil tussen de twee handelingen:

   ```
   lees veld -> ontsleutel met A -> [waarde behouden OF vervangen] -> versleutel voor A+B -> schrijf
                                              ^                ^
                                          recrypt          nieuwe PAT
   ```

   Bouw die lus één keer, met een optionele nieuwe waarde. Zonder waarde is het een recrypt, met waarde een vervanging. Dat levert `task rotate-project-keys` en `task replace-git-pat` op als twee ingangen op dezelfde motor, en het betekent dat je elk projectbestand **één keer** hoeft aan te raken in plaats van twee keer. Minder commits, minder gelegenheid om iets te laten vallen.

   **Randvoorwaarde, en die is hard: de nieuwe PAT moet al geldig zijn op GitHub voordat het eerste bestand wordt geschreven.** Anders verliest een project zijn repositorytoegang op het moment dat zijn bestand is omgezet, en de rest nog niet. Dus dezelfde overlap als bij de sleutel: maak de nieuwe PAT aan, laat beide geldig zijn, zet alle bestanden om, en trek de oude pas daarna in.

   **De afweging om ze samen te draaien.** Voordeel is één ronde over de projectbestanden. Nadeel is dat een fout in de PAT-vervanging ook de sleutelrotatie meesleept, en dat je ze niet los kunt terugdraaien. Mijn voorstel: bouw ze als één motor met twee ingangen, maar laat de eerste echte ronde alleen de sleutel doen. Is die aantoonbaar goed gegaan, dan is de PAT-ronde een herhaling van iets dat al gewerkt heeft. *Verify:* een project kan na afloop zijn repository benaderen met de nieuwe PAT, en de oude waarde komt in geen enkel bestand meer voor.
9. **Een grendel die dit structureel tegenhoudt.** Zie de eigen sectie hieronder; dit is meer dan een regel in een hook.

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

## De vorm van het gereedschap

**Geen Taskfile-taak.** Taskfile werkt slecht met parameters en dit heeft er vier. Het wordt een script dat je aanroept en dat vraagt wat het nodig heeft, met een default op elke vraag zodat doorklikken de gewone weg is:

```
oude sleutel   [security/old_key.txt]
nieuwe sleutel [security/key.txt]
nieuwe PAT     [leeg = huidige waarde hergebruiken]
```

**De naamgeving is de migratie.** `security/key.txt` is altijd "dit is de sleutel, of dit wordt hij". Gemeten: **84 verwijzingen naar `security/key.txt`** in de repo, in de Taskfile, in CLAUDE.md en in de installatiedocumentatie. Zou de nieuwe sleutel anders gaan heten, dan moeten die allemaal mee. Dus de oude schuift naar `old_key.txt` en de nieuwe neemt de vaste naam over. Het script doet die hernoeming zelf aan het eind, na bevestiging, zodat er geen los handwerk overblijft dat iemand vergeet.

**Python, niet shell, en dat is tegen de voorkeur in.** De sops-kant zou prima in shell kunnen: `sops rotate` doet daar het werk. De projectbestanden niet. Gemeten op de 45 testbestanden:

- **alle 45** dragen de sleutel als meerregelige YAML block scalar (`age-private-key: |-` met een BEGIN/END-blok eronder), waar de inspringing en de chomping-indicator exact moeten kloppen;
- in datzelfde bestand staat `repositories[].password` juist als `base64+age:` op **één** regel, dus twee vormen door elkaar;
- **3 van de 45** hebben commentaar, dat een naïeve YAML-ronde weggooit.

Dat met sed en awk doen kan, maar het is precies het soort bewerking waarbij een fout niet crasht maar stil een geldig bestand met verkeerde inhoud oplevert. Python heeft `ruamel.yaml`, dat commentaar en volgorde behoudt, en OPI heeft de ontsleutel- en versleutelfuncties plus `save_and_commit_project` al klaarstaan.

Een shell-schil om een Python-kern is een optie, maar dan onderhoud je twee talen voor één gereedschap. Mijn voorstel is daarom één Python-script. Als de voorkeur voor shell zwaarder weegt dan dit argument, dan is de grens: shell voor de SOPS-bestanden en de env-regels, Python voor de projectbestanden, en nooit de projectbestanden met tekstvervanging.

## Waar de bouwer sleutel A vindt

Het script moet tegen echte data getest worden, en dat kan niet zonder A. Beide helften zitten al in de repo, dus het plan verwijst ernaar in plaats van ze te kopieren. Dat scheelt een extra vindplaats op het moment dat we er juist een aan het opruimen zijn.

- **De private sleutel**: `operations-manager/python/tests/test_age_password_decryption.py`, regel 21. Dat die daar staat is de aanleiding van deze taak. Zet hem lokaal in `security/old_key.txt`; die map is untracked.
- **De publieke sleutel van het PLATFORM**: staat als `recipient` in elk van de 19 SOPS-bestanden. Geen geheim, en niet nieuw. Let op: dit is NIET de `age-public-key` uit een projectbestand. Elk project heeft zijn eigen sleutelpaar, gemeten: 44 unieke waarden over 45 bestanden, en geen enkele gelijk aan die van het platform. Het platform staat erboven, want het ontsleutelt de PRIVATE sleutel van elk project.
- **De testbestanden**: `https://git.claude.robbertuittenbroek.nl/robbert/rig-cluster-projects` onder `projects/`, 45 stuks, allemaal met beide velden.

Daarmee kan de bouwer de volledige ronde draaien en verifieren zonder dat er iets buiten de repo nodig is, en zonder dat er iets bij komt.

**Niet committen.** De nieuwe sleutel, `security/old_key.txt` en de vingerafdrukbestanden blijven op schijf. `security/` is untracked en dat moet zo blijven; controleer dat aan het eind van elke ronde.

## De verificatie is het echte product

De eis is dat je achteraf kunt aantonen dat er niets is veranderd behalve de sleutel. Dat is sterker dan "het script gaf geen fout", en het is de reden dat dit Python wordt en geen shell.

De methode is een **vingerafdruk van de platte inhoud, voor en na**:

```
VOOR   per versleuteld veld: ontsleutel met A  ->  sha256 van de PLATTE waarde  ->  vingerafdruk.json
                                                   (de waarde zelf wordt nooit opgeslagen)
NA     per versleuteld veld: ontsleutel met B  ->  sha256  ->  vergelijk met de vingerafdruk
```

Dat werkt omdat de ciphertext bij elke omzetting verandert en de plaintext niet. Een vergelijking van de versleutelde vorm zegt dus niets; een vergelijking van de hash van de inhoud zegt alles. Er staat nooit een geheim in het bestand: alleen een pad, een veldnaam en een hash.

Wat de verificatie afdekt:

- **Elk veld is nog leesbaar**, nu met de nieuwe sleutel. Een veld dat niet meer opengaat is een harde fout.
- **Elke waarde is ongewijzigd.** Een afwijkende hash betekent dat er inhoud is verschoven, en dat is precies de ramp die je niet wilt.
- **Er is niets kwijt.** Het aantal velden in de vingerafdruk voor en na moet gelijk zijn. Een veld dat stil verdween tijdens een YAML-ronde valt hier door de mand, en dat is de fout die tekstvervanging maakt zonder te crashen.
- **De PAT is de enige bewuste uitzondering.** Bij een vervanging hoort die hash juist te verschillen, en precies bij die velden. Het script noemt ze vooraf, zodat een afwijking elders meteen opvalt.

Draai de verificatie als losse stand: `--verify` moet werken zonder dat er iets omgezet wordt, zodat je hem ook maanden later nog kunt draaien om te zien of alles nog klopt.

**Wat het niet garandeert.** Dat de waarden zelf nog geldig zijn bij de tegenpartij, bijvoorbeeld of GitHub die PAT nog accepteert, valt hier niet mee te toetsen. Dit bewijst dat de omzetting niets heeft beschadigd, niet dat de inhoud doet wat hij moet doen. Daarvoor is de rooktest na de cutover: OPI leest een sops-bestand, ArgoCD rendert een applicatie, en een project haalt zijn repository op.

## Testen: tegen echte projectbestanden, niet tegen fixtures

Er staat een kopie van oudere projectbestanden op `https://git.claude.robbertuittenbroek.nl/robbert/rig-cluster-projects` onder `projects/`. Gemeten: **45 bestanden, alle 45 met `age-private-key` en `age-public-key`**, en 34 met een `password:` in age-vorm. Dat is de testset, en die is representatiever dan welke fixture dan ook.

De toets die telt is niet "het script draait zonder fout" maar **"er is niets veranderd wat niet veranderd mocht worden"**:

- **Ontsleutel elk bestand voor en na, en vergelijk de PLATTE inhoud.** Die moet identiek zijn, op de twee omgezette velden na. Een vergelijking van de ciphertext zegt niets, want die verandert altijd.
- **Tel de velden.** Evenveel deployments, componenten, services en env-vars voor als na. Een YAML-ronde door een parser kan stil dingen laten vallen: commentaar, ankers, lege waarden, de volgorde van sleutels.
- **Toets op beide sleutels.** Na de voorbereidingsfase moet elk omgezet bestand met A én met B te openen zijn.
- **Draai hem twee keer.** De tweede keer hoort niets te doen en dat te melden.
- **Laat er een kapot bestand tussen zitten.** Een project met een onleesbare waarde moet worden overgeslagen met een melding, niet de hele ronde afbreken en niet half weggeschreven worden.

Pas als dat op alle 45 goed gaat, mag hetzelfde script de echte repo aanraken.

## Assertie

```bash
scripts/rotate-sops-key.py --dry-run     # vraagt de paden, noemt wat het zou doen, wijzigt niets
scripts/rotate-sops-key.py               # zelfde vragen, voert uit

SOPS_AGE_KEY="$(sed -n '3p' security/old_key.txt)" sops --decrypt <bestand>   # werkt nog
SOPS_AGE_KEY="$(sed -n '3p' security/key.txt)"     sops --decrypt <bestand>   # werkt ook
```

Klaar als:

- er een overlapfase bestaat waarin A en B allebei werken, aantoonbaar met de twee decrypts;
- het script weigert A te verwijderen zolang er projecten op A staan;
- een tweede keer draaien niets doet en dat meldt;
- de sandboxsleutel onaangeroerd is;
- geen enkele test nog een vaste sleutel bevat.

## Wat hierna komt, en nu bewust niet meegaat

- **De onderliggende wachtwoorden roteren** van de 19 secrets. Her-versleutelen maakt niet onbekend wat gelezen kon worden.
- **De sleutel splitsen** in een infradeel en een projectdeel, zodat de renderer naast ArgoCD niet langer elk projectgeheim kan openen. Zie `de-age-sleutel-roteren-en-splitsen.md`.
- **De uitkomst van de historie-scan.** Wat die oplevert bepaalt of er meer geroteerd moet worden dan nu voorzien.
- **De git-historie zelf.** Zolang die bestaat is elke oude versie met een oude sleutel te openen. Dat geldt voor deze repo en voor de projects-repo. Een sleutelwissel verandert dat niet, en dat hoort een bewuste beslissing te zijn en geen aanname.
