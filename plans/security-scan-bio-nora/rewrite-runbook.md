# Draaiboek: de gelekte AGE-sleutel uit de GitHub-historie verwijderen

Status: voorbereid op 2026-09-26, nog niet uitgevoerd. Dit document is intern en hoort niet op GitHub.

Aanleiding: bevinding K-1 uit `bevindingen.md`. De platform-AGE-sleutel heeft van 4 juli 2025 tot 22 september 2026 in `operations-manager/python/tests/test_age_password_decryption.py` gestaan, in een repository die sinds 8 oktober 2025 publiek is.

## Wat een history rewrite wel en niet oplost

Wel: de sleutel is daarna niet meer te vinden door iemand die de repo kloont of doorzoekt, en scanners van derden vinden hem niet opnieuw. Dat is de eis die hier gesteld is.

Niet: het maakt de blootstelling van het afgelopen jaar niet ongedaan. Wie de sleutel al heeft, houdt hem. De enige maatregel die het risico echt wegneemt is het vervangen van de secrets die eronder lagen. Die rotatie loopt apart en is urgenter dan deze opruiming.

## De drie plekken waar de sleutel staat

| Plek | Bereikbaar via | Weg te krijgen met |
|---|---|---|
| Tekstbestanden in de historie van `main` en de branches | gewone clone | de rewrite hieronder |
| Twee `.pyc`-bestanden in `tests/__pycache__/` | uitsluitend `refs/pull/5/head`, `refs/pull/6/head`, `refs/pull/7/head` | alleen GitHub Support |
| De objecten van vóór de force-push | de oude commit-SHA op github.com, en de fork | alleen GitHub Support, na verwijdering van de fork |

De tweede en derde rij zijn de reden dat een force-push op zichzelf niet genoeg is. `refs/pull/*` is van GitHub; je kunt die refs niet pushen en niet verwijderen.

Meetgegevens uit de dry run: de `.pyc`-bestanden zijn gecompileerde bytecode van het testbestand en bevatten de sleutel als stringconstante. `git filter-repo` slaat ze bewust over, want het laat binaire blobs met rust (een blob met een NUL-byte in de eerste 8 KB wordt niet bewerkt, zie `git_filter_repo.py` regel 3820). Ze staan niet in `main` en ook niet in de huidige boom; alleen in die drie pull-refs.

## Vindbaarheid, gemeten op 26 september 2026

Dit is waarom de stappen staan zoals ze staan. Alles hieronder is getest, niet aangenomen.

- `git ls-remote origin 'refs/pull/*'` levert 92 refs op. GitHub adverteert ze dus aan elke anonieme client; ze zijn niet obscuur en een `git clone --mirror` pakt ze vanzelf mee.
- Een commit ophalen met alleen de SHA werkt: `git fetch --depth 1 origin <sha>` slaagde voor een commit die enkel via `refs/pull/5/head` bereikbaar is, en `https://github.com/.../commit/<sha>` gaf HTTP 200. Een force-push verplaatst dus alleen de branch; het object blijft opvraagbaar tot GitHub opruimt.
- Software Heritage, dat publieke repo's permanent archiveert, kent deze origin niet. Er is daar dus geen onomkeerbare kopie.
- De fork `GliderGeek/RIG-Cluster` is voor het laatst bijgewerkt op 4 maart 2026, midden in de periode dat de sleutel in de historie stond.

Overwogen en verworpen: de repository tijdelijk op private zetten. Dat zou alle anonieme toegang in één keer afsnijden, maar het is hier geen begaanbare weg.

## Volgorde

De volgorde is niet vrij. Zolang de fork bestaat, deelt hij de objectopslag met het origineel en blijft elke oude commit daar opvraagbaar, ook na de force-push.

1. **Laat de fork verwijderen.** `GliderGeek/RIG-Cluster`. De eigenaar moet dat zelf doen; een organisatiebeheerder kan het niet. Lukt dat niet, vraag GitHub Support de fork uit het netwerk te halen. Dit is de blokkerende stap voor al het volgende.
2. **Voer de rewrite uit en push geforceerd.** Commando's hieronder. Push-rechten zijn gecontroleerd: `admin: true`, `allow_force_pushes: true`, `enforce_admins: false`, geen push-restricties, en `uittenbroekrobbert` staat in de bypass-lijst. De force-push wordt dus geaccepteerd.
3. **Dien het ticket in bij GitHub Support.** Tekst hieronder. Doe dit pas na de push, want Support vraagt om bevestiging dat de nieuwe historie er al staat.

Parallel, en urgenter dan deze hele opruiming: **roteer de secrets** die onder de oude sleutel stonden. Minimaal `KEYCLOAK_ADMIN_PASSWORD` en `SECRET_KEY`, want die twee zijn vanaf internet te misbruiken. Zie `bevindingen.md` K-1 voor de volledige lijst en de volgorde. De rewrite haalt de sleutel uit beeld, de rotatie haalt het risico weg.

Na afloop:

- **Laat iedereen opnieuw klonen.** Elke bestaande clone, worktree en CI-checkout is onbruikbaar; een `git pull` meldt een onverwante historie.
- **Replay de Forgejo-lijn** bovenop de nieuwe `main`, zoals afgesproken. Let op dat de Forgejo-server dezelfde oude historie draagt en dus dezelfde behandeling verdient; hij is wel in eigen beheer.
- **De vier open pull requests** (152, 144, 78, 47) komen uit de eigen organisatie en hun branches worden door de push meegenomen. Sluiten is niet nodig voor het verwijderen van de sleutel, maar het helpt Support bij de opruiming als er geen pull-refs meer naar oude objecten wijzen.

## De rewrite

Werk op een verse mirror-clone, nooit op een werkcheckout.

```bash
git clone --mirror git@github.com:RijksICTGilde/RIG-Cluster.git RIG-Cluster.git

cat > replacements.txt <<'EOF'
regex:AGE-SECRET-KEY-1[A-Z0-9]{50,}==>REDACTED-AGE-PRIVATE-KEY-SEE-SECURITY-NOTICE
EOF

cd RIG-Cluster.git
git filter-repo --replace-text ../replacements.txt --force
```

Het patroon staat op de *vorm* van een AGE-sleutel en niet op de waarde, zodat de echte sleutel nergens in een bestand of in een terminal-historie terechtkomt.

Bewust niet meegenomen: patronen voor PEM- en OpenSSH-sleutels. Die raakten in de eerste dry run ook nep-sleutels in tests (`FAKEKEYMATERIAL`, `BBBB`) en veranderden daarmee `test_publish_passthrough.py` en `test_argo_repository_no_hardcoded_key.py` op `main`. Het uitgangspunt is: alles blijft gelijk behalve de AGE-sleutel.

Pushen, zonder de pull-refs (GitHub weigert die):

```bash
git push --force origin 'refs/heads/*:refs/heads/*'
git push --force origin 'refs/tags/*:refs/tags/*'
```

## Controle vooraf

Draai deze drie voordat je pusht. Let op: gebruik `/usr/bin/grep -a`, want de shell-functie `grep` in deze omgeving slaat binaire invoer over en geeft dan een vals-schone uitslag.

1. Aantal commits en refs gelijk aan de oorspronkelijke mirror.
2. Geen enkele blob die vanaf `refs/heads` of `refs/tags` bereikbaar is bevat nog `AGE-SECRET-KEY-1` gevolgd door 50 of meer hoofdletters of cijfers.
3. De boom van `main` is identiek aan die van `origin/main` van vóór de rewrite. Zijn er verschillen, dan raakt het patroon meer dan bedoeld.

## Tekst voor het GitHub Support-ticket

Indienen via <https://support.github.com/contact> door iemand met adminrechten op de repository. Heeft de organisatie GitHub Enterprise, gebruik dan dat supportkanaal, want dat is sneller.

Onderwerp: `Request to purge unreachable objects and cached views after history rewrite (leaked private key)`

> Repository: RijksICTGilde/RIG-Cluster
>
> A private encryption key was accidentally committed to this repository and remained in the history of the public repository. We have rewritten the history with git-filter-repo to remove the key and force-pushed the rewritten branches and tags.
>
> We request that GitHub:
>
> 1. Garbage-collect the unreachable objects in this repository so the pre-rewrite commits are no longer retrievable by SHA.
> 2. Remove the cached commit and diff views for those objects.
> 3. Remove or purge the pull request refs `refs/pull/5/head`, `refs/pull/6/head` and `refs/pull/7/head`, which still contain the key inside committed `.pyc` files and which we cannot rewrite ourselves.
>
> The only fork of this repository has been deleted, and all open pull requests have been closed, so nothing in the network should still reference the old objects.
>
> We can supply the list of affected commit SHAs on request.

Verwacht dat Support vraagt om bevestiging dat forks weg zijn en pull requests gesloten. Houd de lijst met oude SHA's bij de hand; die is te maken met `git rev-list --all` op de mirror van vóór de rewrite.

## De Forgejo-lijn, uitgevoerd op 27 september 2026

Forgejo droeg dezelfde sleutel: de oudste commit met `tests/test_age_password_decryption.py` had hem gewoon in de boom staan. Daar is dezelfde rewrite op toegepast, met `main` als doel, want dat wordt de nieuwe basis die straks naar GitHub gaat.

Gedaan, in deze volgorde:

1. De acht wachtende commits met een gewone push naar `forgejo/main`, zodat ze in de rewrite meegaan in plaats van erna opnieuw te moeten.
2. Mirror-clone van Forgejo, plus een backup ervan als terugvaloptie.
3. Dezelfde `--replace-text`-regel, alleen het AGE-patroon.
4. Geforceerde push van uitsluitend `refs/heads/main`.
5. De werkcheckout met `git reset --hard forgejo/main` op de herschreven lijn gezet, na te hebben vastgesteld dat de boom identiek was.

Gemeten uitkomst:

| Controle | Uitkomst |
|---|---|
| Bestanden in `main` | 2929, ongewijzigd |
| Commits in `main` | 2897, ongewijzigd |
| Verschillen in de boom van `main` | 0 |
| Branches en refs | 210 en 416, ongewijzigd |
| Blobs met een volledige AGE-sleutel, over alle refs | 0 van 19025 |
| Nieuwe tip van `main` | `253760e0` |

Eén waarneming die uitleg verdient. Over alle refs samen daalde het aantal commits van 4996 naar 4941. Er is niets verwijderd: geen enkele commit is naar leeg gemapt. Van de 4997 mappingregels wijzen er 56 naar een gedeelde nieuwe SHA, dus 55 paren commits die al dubbel in de historie zaten zijn na de redactie identiek geworden en samengevallen. `main` zelf is niet geraakt, zoals de telling van 2897 laat zien.

### Wat op Forgejo nog openstaat

Alleen `main` is herschreven. De andere 209 branches dragen de oude historie nog, en dus ook de sleutel. Dat is een bewuste keuze: een geforceerde push over alle branches zou de negentien openstaande worktrees onbruikbaar maken. Zolang die branches bestaan blijft de sleutel op de Forgejo-server bereikbaar. Forgejo is intern, dus dat is een ander risiconiveau dan GitHub, maar het is niet nul en het hoort opgeruimd te worden zodra die takken zijn afgerond of vervallen.

De GitHub-lijn is hierna niet meer verwant aan de Forgejo-lijn: `origin/main` staat 2029 commits uit de pas. Dat komt doordat filter-repo ook commit-SHA's vervangt die in commitberichten staan, en die mapping verschilt per repository. De eerste afwijking zit bij commit `64d7bc49`, waarvan het bericht een SHA noemt die op GitHub niet bestaat. Vanaf daar lopen alle SHA's uiteen. Het samenbrengen van beide lijnen is bewust uitgesteld; Forgejo `main` is de nieuwe basis en de rest wordt daartegen opgeruimd.

## Collateral

- Alle commit-SHA's veranderen. Verwijzingen naar commits in issues, pull requests, changelogs en documentatie kloppen daarna niet meer.
- De 51 tags wijzen naar nieuwe commits. De CalVer-tags waar gepubliceerde images naar verwijzen, zoals `2026.09.02.2241-d81cdab4`, noemen een commit die niet meer bestaat.
- De lokale `main` loopt 615 commits voor op GitHub en `forgejo/main` is een eigen lijn. Die moeten opnieuw bovenop de herschreven historie worden gezet.
