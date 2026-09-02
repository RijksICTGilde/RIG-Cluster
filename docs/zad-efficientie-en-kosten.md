# ZAD: het bestelbusje

*Peildatum 31 augustus 2026. Bron: de 48 projectbestanden in `rig-cluster-projects/projects`, de auto-tune-historie daarin, en de platformmanifesten in `infrastructure/bootstrap` en `bootstrap/rig-system`. Rekenprijs: 25 euro per GiB geheugen per maand. Er wordt afgerekend op `max(request, werkelijk gebruik)` per container.*

---

## De rekening bestaat uit vier delen

Alles in dit stuk is de uitwerking van deze tabel. De vier delen tellen op tot wat Grafana meet, en elk deel heeft zijn eigen verhaal.

| | Wat het is | Nu | Per maand |
|---|---|---:|---:|
| **A** | **Platform**: wat je sowieso nodig hebt | 6,80 GiB | 170 euro |
| **B** | **Gedeelde diensten**: database, SSO, opslag, Redis | 1,12 GiB | 28 euro |
| **C** | **Applicaties**: 207 containers van 48 projecten | 30,29 GiB | 757 euro |
| | Sidecars en wekkers | 1,13 GiB | 28 euro |
| | **Totaal** | **39,34 GiB** | **984 euro** |
| | *Grafana meet* | *39,12 GiB* | *978 euro* |

En daar hoort een vierde deel bij dat geen kostenpost is maar een besparing:

| | Wat het is | Nu | Per maand |
|---|---|---:|---:|
| **D** | **Slaapstand**: omgevingen die niemand gebruikt staan uit | 5,26 GiB niet betaald | 132 euro bespaard |

De rest van dit stuk loopt A, B, C en D langs. Per deel staat er wat het kost, wat het alternatief zou kosten, en wat dat verschil is.

### Wat de vier delen samen opleveren

| Deel | Hefboom | Bespaart nu | Per jaar |
|---|---|---:|---:|
| A | Geen. Dit is de vaste voet. | 0 | 0 |
| B | Delen in plaats van een eigen instantie per omgeving | 4.247 euro/mnd | 50.964 euro |
| C | Tuning in plaats van een ingevuld getal (uitgaande van 512 Mi) | 1.913 euro/mnd | 22.962 euro |
| D | Uitzetten wat niemand bekijkt | 132 euro/mnd | 1.578 euro |
| | **Samen** | **6.292 euro/mnd** | **75.504 euro** |

Bij een voorzichtiger aanname (een eigen instantie per *project* in plaats van per omgeving, en mensen die de prefill van 256 Mi laten staan) is het 2.567 euro per maand, oftewel 30.804 per jaar. **De bandbreedte is dus grofweg 31.000 tot 75.000 euro per jaar, bij 48 projecten.**

---

## Vooraf: waarom de reservering de rekening ís

Er wordt afgerekend op **`max(request, werkelijk gebruik)`** per container. Dat maakt twee tegengestelde instincten allebei fout:

- **Ruim vragen kost direct geld.** Vraag je 512 Mi voor een container die 68 Mi gebruikt, dan betaal je 512 Mi. Je betaalt voor de lucht.
- **Laag vragen levert niets op.** Zet je 25 Mi op een container die 500 Mi nodig heeft, dan betaal je alsnog 500 Mi, en val je bovendien om.

Er is dus precies één manier om de rekening te verlagen: de reservering strak boven het werkelijke gebruik laten liggen, en hem daar houden. Dat is geen zuinigheid, dat is nauwkeurigheid. Deel C gaat daarover.

---

# A. Het platform: wat je sowieso nodig hebt

| Onderdeel | Request | Per maand | Heb je die ook zonder ZAD? |
|---|---:|---:|---|
| ArgoCD (controller 4 GiB, repo-server + SOPS-sidecar, server, ApplicationSet, Dex, Redis) | 5,25 GiB | 131 euro | **Ja.** Wie GitOps doet, draait een ArgoCD. |
| Operations Manager | 1,00 GiB | 25 euro | Nee, dit is ZAD zelf. |
| Prometheus + kube-state-metrics | 0,31 GiB | 8 euro | Ja, in een of andere vorm. |
| external-dns | 0,12 GiB | 3 euro | Ja, of je doet het met de hand. |
| Mailrelay | 0,12 GiB | 3 euro | Ja, of je vraagt er een aan. |
| **Totaal** | **6,80 GiB** | **170 euro** | |

**Dit deel bespaart niets en dat is prima.** Het is de vaste voet. Wat het bijzonder maakt is dat hij **niet meeschaalt**: 6,80 GiB voor 48 projecten is 3,54 euro per project per maand; bij 96 projecten is dat 1,77. Het is het enige deel van de rekening dat goedkoper wordt naarmate je het drukker gebruikt.

Van die 6,80 GiB is 5,25 GiB (77 procent) ArgoCD, en dat had je met of zonder ZAD gehad. **De prijs van ZAD zelf is de operations manager: 1,00 GiB, 25 euro per maand voor 48 projecten.** Dat is 52 cent per project.

### Eén eerlijke kanttekening

Die 4 GiB van de ArgoCD-controller is een gedeclareerde waarde uit een manifest, geen meting. De auto-tuner uit deel C is project-scoped: hij laadt een projectbestand en schrijft daarin terug. ArgoCD, de operations manager en de gedeelde diensten zijn geen project, dus **de tuner raakt deel A en B nooit aan.**

Het dichten is goedkoop: zet een Off-mode VPA op de controller en meet een week. Gebruikt hij 1,5 GiB, dan ligt daar 2,5 GiB, ongeveer 62 euro per maand. Heeft hij die 4 GiB echt nodig, dan weten we dat en staat het vast.

---

# B. Gedeelde diensten: delen versus uniek

Dit is het deel waar het meeste geld zit, en het is ook het minst zichtbaar.

Vandaag draaien **66 databases op één PostgreSQL van 512 Mi** (7,8 Mi per database) en **26 realms op één Keycloak van 256 Mi** (9,8 Mi per realm). Samen met MinIO en Redis is dat 1,12 GiB, oftewel 28 euro per maand.

De vraag "wat als iedereen zijn eigen ding opspint" heeft twee trappen.

| Dienst | Gedeeld (nu) | Uniek per project | | Uniek per omgeving | |
|---|---:|---:|---:|---:|---:|
| | | *minimaal* | *realistisch* | *minimaal* | *realistisch* |
| PostgreSQL (26 projecten, 66 omgevingen) | 0,50 | 6,3 | 26,0 | 16,1 | 66,0 |
| Keycloak (26 projecten, 66 omgevingen) | 0,25 | 13,0 | 39,0 | 33,0 | 99,0 |
| MinIO (10 projecten) | 0,25 | 1,2 | 5,0 | 1,2 | 5,0 |
| Redis (4 projecten) | 0,12 | 0,1 | 1,0 | 0,1 | 1,0 |
| **Totaal, GiB** | **1,12** | **20,7** | **71,0** | **50,5** | **171,0** |
| Per maand | 28 euro | 518 | 1.775 | 1.262 | **4.275** |
| Per jaar | 338 euro | 6.217 | 21.300 | 15.146 | **51.300** |
| Factor | 1× | 18× | 63× | 45× | **152×** |
| CPU-requests, cores | 0,5 | 4,2 | 12,4 | 10,2 | **30,4** |

"Minimaal" is de absolute ondergrens waarop het ding nog draait (PostgreSQL 250 Mi, Keycloak 512 Mi, MinIO 128 Mi, Redis 32 Mi). "Realistisch" is wat de gangbare Helm-charts en de projecten zelf aanraden.

## Wat dit deel bespaart

| Alternatief | Kosten daarvan | Wat delen bespaart |
|---|---:|---:|
| Uniek per project, minimaal gesized | 518 euro/mnd | **490 euro/mnd, 5.879 euro/jaar** |
| Uniek per project, realistisch | 1.775 euro/mnd | **1.747 euro/mnd, 20.964 euro/jaar** |
| Uniek per omgeving, minimaal gesized | 1.262 euro/mnd | **1.234 euro/mnd, 14.808 euro/jaar** |
| Uniek per omgeving, realistisch | 4.275 euro/mnd | **4.247 euro/mnd, 50.964 euro/jaar** |

Drie dingen om op te merken:

**171 GiB is meer dan vier keer de complete huidige rekening (39,3 GiB).** Vier gedupliceerde diensten alleen al, nog zonder één applicatiecontainer, voor het onderdeel dat nu 28 euro per maand kost.

**Zelfs de zuinigste variant is 45 keer duurder.** Dat komt doordat de ondergrens van een instantie niet nul is: een lege PostgreSQL kost 250 Mi voordat er één rij in staat, een lege JVM kost 512 Mi voordat er één gebruiker inlogt. **Delen is niet zuiniger omdat je kleiner denkt, maar omdat je die ondergrens één keer betaalt in plaats van 66 keer.**

**66 eigen databases vragen 6,6 cores**, meer dan de complete applicatievloot van 207 containers samen (6,3 cores). De databases zouden meer plek op de nodes claimen dan alles wat ze bedienen.

## Het punt is niet dat delen moet, maar dat het kan

Delen is normaal gesproken niet moeilijk omdat mensen niet willen, maar omdat het loodgieterswerk vraagt: aparte databases met aparte credentials, connectielimieten, backup per huurder, opruimen bij verwijderen. Dat werk is de reden dat "kunnen we dat delen?" in de meeste organisaties eindigt in "doe maar een eigen instantie". **ZAD heeft dat loodgieterswerk één keer gedaan, en daarmee is delen van een theoretische optie een praktische geworden.**

En dat pakt goed uit omdat **de eisen van de meeste projecten helemaal niet spectaculair zijn.** Het bewijs staat in de tabel: die 66 databases passen samen in één instantie van 512 Mi met 20 GiB schijf. Als er ook maar één zware tussen zat, zou dat niet werken. Dit is geen vloot van datawarehouses; dit zijn portalen, formulieren, registers en documentatiesites.

Voor wie het anders nodig heeft, staat `scope: project` klaar: een eigen CNPG-cluster in een eigen infrastructuurnamespace. Twee projecten gebruiken dat vandaag, omdat ze superuser-rechten of eigen extensies nodig hebben. Dat is prima. Ze hadden een reden.

## Wat je ervoor inlevert

Dit zou een verkooppraatje zijn als de andere kant er niet bij stond:

1. **Eén instantie omlaag is 66 databases omlaag.** De blast radius van `rig-db` is het hele platform, inclusief Keycloak, Forgejo en de operations manager.
2. **Luidruchtige buren zijn echt.** In februari 2026 hield één project 75 van de 100 connectieslots bezet, over drie deployments, door een bug die per verzoek een nieuwe engine opzette. Keycloak kon geen realm meer aanmaken en de authenticatie van het hele cluster lag eruit. Reparatie: `max_connections` naar 200 en tien gereserveerde slots voor infrastructuur.
3. **Iedereen zit op dezelfde versie en dezelfde extensies.**
4. **Isolatie is logisch, niet fysiek.** Aparte databases met aparte credentials, maar hetzelfde proces en hetzelfde geheugen.

---

# C. Tuning op de pods

207 containers van 48 projecten. Voor 184 daarvan staat er een gemeten waarde in git: een VPA-target of een Prometheus-maximum, met datum en redenering erbij.

| | Geheugen | Per maand | Per jaar |
|---|---:|---:|---:|
| **Wat ze werkelijk gebruiken** (gemeten) | 24,14 GiB | 604 euro | 7.242 euro |
| **Wat de tuner nu heeft ingesteld** | **30,29 GiB** | **757 euro** | **9.087 euro** |
| Wat mensen zouden invullen: de prefill van 256 Mi | 57,83 GiB | 1.446 euro | 17.349 euro |
| Wat mensen zouden invullen: 512 Mi | 106,83 GiB | 2.671 euro | 32.049 euro |
| Wat mensen zouden invullen: 1 GiB | 208,49 GiB | 5.212 euro | 62.547 euro |

De onderste drie regels zijn geen fantasie. De wizard vult bij het aanmaken van een component **256 Mi en 1 CPU** voor. De mediaan van wat mensen bij aanmaak zelf invulden, over 35 componenten met een eigen opgave, is óók 256 Mi. En 512 Mi of 1 GiB is wat je invult zodra je één keer een OOM-kill hebt meegemaakt.

De mediaan van wat die containers werkelijk gebruiken is **68 Mi**. 42 van de 184 gemeten containers gebruiken minder dan 25 Mi.

## Wat dit deel bespaart

| Alternatief | Wat de tuner bespaart |
|---|---:|
| De prefill van 256 Mi blijft overal staan | **688 euro/mnd, 8.262 euro/jaar** |
| Mensen vullen zelf 512 Mi in | **1.913 euro/mnd, 22.962 euro/jaar** |
| Mensen vullen zelf 1 GiB in | **4.455 euro/mnd, 53.460 euro/jaar** |

De reservering ligt nu op **1,23 keer** het werkelijke gebruik. De 6,15 GiB daarboven, 154 euro per maand, is de volledige prijs van het feit dat je vooraf een getal moet noemen. Zet dat naast de 3,8 keer die je krijgt als de prefill blijft staan, of de 7,5 keer bij een opgave van 512 Mi.

## Wat de tuner in de praktijk doet

Op de limits, die niet gefactureerd worden maar wel bepalen wanneer een pod omvalt, ging de vloot van een piek van **78,3 GiB naar 53,5 GiB**. De grootste correcties zijn niet subtiel:

| Project / component | Hoogste stand ooit | Nu | Gemeten gebruik |
|---|---:|---:|---:|
| `mpfm-w3h` / magazijnb (2 PR-omgevingen) | 4096 Mi | 642 Mi | 363 Mi |
| `mpfpsm-lcl` / profiel (2 PR-omgevingen) | 1418 Mi | 256 Mi | 114 Mi |
| `ubbw-0i1` / typesense | 2304 Mi | 1400 Mi | 933 Mi |
| `wies` / worker (3 omgevingen) | 768 Mi | 101 tot 148 Mi | 84 Mi |
| `mpfb-8wh` / redis (3 omgevingen) | 512 Mi | 25 Mi | 13 Mi |
| `toets-hn7` / frontend | 512 Mi | 25 Mi | 2 Mi |

En omhoog werkt het net zo goed. `openp-4pw` (OpenProject) vroeg ooit 3444 Mi, staat nu op 1024 Mi en meet 2554 Mi. Er wordt dus 2554 Mi gefactureerd: het is de enige container van betekenis die boven zijn reservering uitkomt, goed voor 1,5 GiB van de rekening. **"Je krijgt wat je nodig hebt" betekent ook omhoog.**

## En de CPU-kant, die niet in euro's maar in ruimte betaalt

| | Nu |
|---|---:|
| CPU-requests, 207 applicatiecontainers | 6,3 cores |
| Mediaan per container | 32 millicores |
| Met de wizard-prefill van 1 CPU | **207 cores** |

Een CPU-request is een reservering op een node. De scheduler telt requests op en plaatst niets meer zodra de node vol is, ongeacht of die CPU echt gebruikt wordt. Met 207 cores aan requests zou deze vloot op geen enkel realistisch cluster passen, terwijl het werkelijke gebruik onder de tien procent ligt.

Dat is het handdoekjesprobleem in zijn zuiverste vorm. Niemand ligt op die ligstoel; er ligt een handdoek omdat iemand ooit "1" heeft ingevuld. Het effect is niet dat het duurder wordt, het effect is dat de volgende collega geen plek meer heeft.

## Waarom dit niet gewoon VPA is

VPA is een goed idee, maar lost het alleen niet op:

- **VPA in `Auto`-modus herstart je pods.** ZAD draait VPA in `Off`-modus: de recommender rekent, ZAD leest de uitkomst en schrijft hem in het projectbestand.
- **De aanbeveling verdwijnt bij een herstart.** ZAD schrijft hem in git, met tijdstempel en reden: *"Request: VPA target 259Mi = 259Mi. Limit: VPA target 259Mi × 1,5 = 389Mi"*.
- **De recommender adviseert nooit onder 250 Mi.** Precies bij de 42 containers onder de 25 Mi zegt VPA dus "250 Mi". ZAD herkent dat een target op die vloer een ondergrens is en geen meting, en valt terug op Prometheus. Dat verschil alleen is ongeveer 9 GiB, 230 euro per maand.
- **VPA weet niet wat een pull request is.** ZAD tunet productie en preview onafhankelijk, laat de wortelcomponent staan zoals de ontwikkelaar hem opschreef, en laat met de hand vastgezette velden met rust, per veld. Alleen een echte OOM-kill mag daar doorheen om de limit te verhogen.

---

# D. Uitzetten wat niemand gebruikt

46 van de 132 omgevingen zijn PR- of preview-omgevingen: 77 van de 207 containers, samen 11,25 GiB oftewel 281 euro per maand. Dat is de prijs van het feit dat ZAD het te makkelijk maakt om er een aan te zetten.

De slaapstand zet een omgeving die na een deadline niet bezocht is op `replicas: 0`. Wie de URL bezoekt krijgt een pagina "applicatie wordt gestart", en de omgeving is binnen een halve minuut terug. Er is geen knop die iemand moet indrukken en geen opruimactie die iemand moet plannen.

## `wies` als maatstaf

`wies` is het beste voorbeeld omdat het de dienst het langst gebruikt: 20 omgevingen, waarvan 17 van een pull request.

| | |
|---|---:|
| Omgevingen | 20 |
| Waarvan in slaapstand | **15 (75%)** |
| Totaal gereserveerd | 6,81 GiB |
| Waarvan slapend | **5,33 GiB (78%)** |
| Wat wies zou betalen zonder slaapstand | 170 euro/mnd |
| **Wat wies werkelijk betaalt** | **37 euro/mnd** |

Drie kwart van de omgevingen slaapt, en dat is niet uitzonderlijk maar normaal: een preview wordt een dag of twee actief bekeken en staat daarna wekenlang stil.

## Wat dit deel bespaart

| | Geheugen | Per maand | Per jaar |
|---|---:|---:|---:|
| Vandaag, 18 slapende omgevingen (3 projecten) | 5,70 GiB slapend, min 0,44 GiB wekkers | **132 euro** | **1.578 euro** |
| Als alle 46 PR-omgevingen het gebruiken, bij het wies-patroon | 8,81 GiB slapend, min 1,12 GiB wekkers | **192 euro** | **2.305 euro** |

De wekkerpods kosten 25 Mi per slapende omgeving en zijn in beide regels afgetrokken.

In geld is dit het kleinste van de vier delen. **In gedrag is het misschien wel het belangrijkste**, want het maakt opruimen onnodig. De discussie "welke PR-omgevingen kunnen weg" hoeft niet gevoerd te worden, want een omgeving die niemand bekijkt kost al bijna niets. En als iemand hem na drie weken tóch nodig heeft, is hij er gewoon nog.

Het is slaapstand en geen sluimerstand: de applicatie start koud op, sessies en caches overleven het niet. Voor een preview is dat precies goed.

---

# Alles bij elkaar

| Deel | Nu | Alternatief | Verschil per maand | Per jaar |
|---|---:|---:|---:|---:|
| A · Platform | 170 euro | 170 euro | 0 | 0 |
| B · Gedeelde diensten | 28 euro | 4.275 euro | **4.247 euro** | 50.964 euro |
| C · Applicaties | 757 euro | 2.671 euro | **1.913 euro** | 22.962 euro |
| D · Slaapstand | 0 | 132 euro | **132 euro** | 1.578 euro |
| | | | **6.292 euro** | **75.504 euro** |

Bij de voorzichtigste aannames (uniek per project in plaats van per omgeving, en de prefill van 256 Mi in plaats van 512) is het 490 + 688 + 132 = **1.310 euro per maand, 15.720 per jaar.** Bij de realistische aannames hierboven is het 75.504 per jaar.

**De bandbreedte is dus 16.000 tot 75.000 euro per jaar, bij 48 projecten.** En het schaalt mee: B en C groeien beide lineair met de vloot, terwijl A vlak blijft.

## En dan de rest, die niet in geheugen te vangen is

Alle cijfers hierboven gaan over geheugen. Wat er zonder ZAD nog bij komt, per project:

- **DNS en certificaten.** 48 projecten met ruim 130 hostnamen. Nu een goedgekeurd subdomein in het projectbestand; zonder ZAD per hostnaam een aanvraag en per certificaat een vernieuwing die iemand bewaakt.
- **Backups.** Nu één voorziening met één retentiebeleid; zonder ZAD 26 databases met elk een eigen schema waarvan niemand weet of het ooit teruggezet is.
- **Toegang en rollen.** 26 realms met SSO; zonder ZAD 26 keer een OIDC-koppeling laten configureren.
- **Netwerkbeleid, geheimenbeheer, uitrolpijplijn, monitoring.** Nu één keer goed, zonder ZAD 48 keer half.
- **Wie mag wat.** Het projectbestand legt vast wie beheerder is, welke domeinen goedgekeurd zijn en door wie. Dat is de verantwoording, geen bijproduct.

Schat je dat op twee uur per project per maand, dan is dat 96 uur per maand. **In geld meer dan de hele geheugenrekening.**

---

# Waar het om gaat

Het bestelbusje heeft een vast volume, en de vraag is niet of jouw doos erin past. De vraag is of alle dozen erin passen.

Een handdoek op een ligstoel is niet duur. Hij kost je niets. Hij kost alleen de volgende persoon een ligstoel. En omdat niemand de eerste wil zijn die zijn handdoek weghaalt, liggen er vijftig handdoeken op vijftig lege stoelen en staat iedereen. Alleen: bij `max(request, gebruik)` kost die handdoek je wél iets, namelijk 25 euro per maand per gigabyte lucht. Dit hoeft dus geen moreel verhaal te zijn.

ZAD lost het op zonder verbod en zonder quotum, langs de vier lijnen hierboven:

- **A.** De vaste voet wordt één keer betaald in plaats van 48 keer.
- **B.** Delen wordt een echte optie in plaats van een theoretische. Niet moet, maar kan. Wie bescheiden eisen heeft, en dat zijn de meesten, krijgt kant en klaar wat hij anders zelf had moeten optuigen. Wie meer nodig heeft, vraagt zijn eigen instantie aan en krijgt hem.
- **C.** De vraag verandert van "hoeveel wil je" naar "hoeveel gebruik je". Elke nacht gemeten, vastgelegd in git, met ruimte omhoog zodra je die echt nodig hebt.
- **D.** Wat niemand bekijkt, staat uit. Zonder dat iemand daar iets voor hoeft te doen.

Wat dat oplevert, in die volgorde:

- **Voor jou.** Je hoeft geen getal te verzinnen waar je toch naast zit. Je omgeving valt niet om, want een OOM-kill verhoogt de limit dezelfde nacht nog. Je krijgt twintig preview-omgevingen in plaats van twee, en je hoeft ze niet op te ruimen.
- **Voor iedereen.** 6,3 cores gereserveerd in plaats van 207. Er kan wél wat bij.
- **Voor de rekening.** 984 euro per maand, tegenover 16.000 tot 75.000 euro per jaar aan verschil met de alternatieven.
- **Voor de planeet.** Minder ijzer, minder stroom, minder koeling. Een beetje groen.

Een beetje groen, een beetje geld, en vooral: leefruimte in plaats van claimruimte.

---

## Verantwoording, en één openstaande vraag

**Bronnen.** De projectcijfers komen uit de 48 projectbestanden in `rig-cluster-projects/projects` op 31 augustus 2026 en uit de `resources.history`-blokken daarin, die de door de tuner gemeten waarden bevatten ("VPA target 259Mi", "max 6Mi"). De platformcijfers komen uit `overlays/odcn-production/argocd-deployment.yaml`, uit de gerenderde kustomize-build van `infrastructure/bootstrap/clusters/odcn`, uit de odcn-overlay van de operations manager, en uit `sleep_mode/manifests.py` en `sidecar-authorization-wall.yaml.jinja`. Deel A en B zijn dus gedeclareerde waarden, geen metingen: de tuner meet ze niet.

**Openstaande vraag: het slaapstand-gat.** De 30,29 GiB in deel C telt alle 207 containers, inclusief de 36 in slapende deployments. Die staan op `replicas: 0` en zouden nul moeten kosten. Trek je ze eraf, dan is de rekening 33,64 GiB (841 euro per maand) in plaats van 39,34 (984 euro). Grafana meet 39,12 GiB. Eén van beide klopt niet, en dit moet met één query beslecht worden voordat de cijfers de zaal in gaan:

```promql
sum by (namespace) (kube_pod_container_resource_requests{resource="memory", namespace=~"rig-.*"})
```

Komt daar 39 GiB uit terwijl 18 deployments slapen, dan staan die pods niet werkelijk op nul, en dat is een bug die meer waard is dan deze hele analyse. Komt er 33 tot 34 GiB uit, dan werkt de slaapstand zoals bedoeld en zit er nog ongeveer 5 GiB platform in dat niet in deze repository staat: cert-manager, de CNPG-operator, de VPA-operator, de ArgoCD-operator en backupjobs. **Tot dat moment is de veilige lezing 33,6 tot 39,3 GiB.** Elke conclusie in dit stuk staat over die hele bandbreedte overeind.

Terzijde: `wies/pr-632` staat op `awake` met `expires-at: 2026-08-29T11:10`, twee dagen geleden. Die had allang moeten slapen.

**Aannames.** De prijs van 25 euro per GiB per maand en de afrekenregel `max(request, werkelijk gebruik)` zijn meegegeven en niet zelf geverifieerd; alle bedragen schalen lineair mee. De alternatieven in deel B en C zijn modellen, geen metingen, maar de applicatiekant is wel op gemeten verbruik gebaseerd: per container wordt `max(alternatief-request, gemeten gebruik)` genomen, met de huidige reservering als proxy voor de 23 containers zonder meting. De sizing per voorziening komt van de defaults van gangbare Helm-charts en de aanbevelingen van de projecten zelf.

**Testprojecten.** Vijf van de 48 projecten zijn van onszelf: `tvas-7pb` ("Test van Alle Services"), `tva-d62`, `tr-odc`, `hwmaw-ovh` en `cot-zaq`. Samen vijf containers en 0,20 GiB, oftewel vijf euro per maand. Ze tellen mee in alle bedragen omdat ze echt draaien, maar ze worden nergens gebruikt als bewijs van wat een project nodig heeft. Gevolg: `send-email` en `vlam` hebben allebei precies één gebruiker in de dienstentelling, en dat is dat testproject. **Nul echte projecten gebruiken ze vandaag.**

**Overig.** Drie deployments hebben uitgeschakelde componenten, en twee deployments rollen via Helmfile uit en hebben daarom geen componentspecificatie in het projectbestand. Die tellen mee in de 132 omgevingen, maar niet in de 207 containers.
