# 100 dingen die je kunt zeggen over ZAD

*Ruw materiaal voor een promopraatje richting ontwikkelaars. Alles is gebaseerd op de 48 projectbestanden, de auto-tune-historie en de platformmanifesten, peildatum 31 augustus 2026. Pik eruit wat werkt; ze zijn niet bedoeld om allemaal gebruikt te worden.*

## De eerste vijf minuten

1. Je begint met één YAML-bestand en eindigt met een draaiende applicatie op een echte URL.
2. Je schrijft op **wat** je nodig hebt, niet **hoe** het moet.
3. Er is geen ticket, geen aanvraagformulier en geen wachtrij.
4. Er staan er nu 48 op. De eerste vijf minuten zijn dus 48 keer gelukt.
5. `mzs-3ik` draait negen omgevingen van één website. Bij elkaar 225 Mi en 5,50 euro per maand. Dat is wat "gewoon aanzetten" waard is.
6. Je hoeft niet te weten in welke namespace je zit. Die krijg je.
7. Je hoeft niet te weten hoe je een Deployment schrijft. Die wordt gegenereerd.
8. De wizard vult alles alvast in. Fout invullen kan bijna niet, want je krijgt toch wat je nodig hebt.
9. Je eerste deployment is niet anders dan je vijftigste. Zelfde bestand, zelfde route.
10. Als je het projectbestand liever met de hand schrijft, mag dat ook. Het is gewoon YAML in git.

## Wat je niet hoeft te weten

11. Je hoeft geen Kubernetes te kennen.
12. Je hoeft niet te weten wat een StatefulSet is, of wanneer je er een nodig hebt.
13. Je hoeft geen Helm-chart te schrijven, en geen `values.yaml` te doorgronden.
14. Je hoeft niets van cert-manager te weten, en niets van ACME-uitdagingen.
15. Je hoeft geen NetworkPolicy te kunnen schrijven om er toch een te hebben.
16. Je hoeft SOPS en AGE niet te kennen, terwijl je geheimen wel versleuteld in git staan.
17. Je hoeft ArgoCD niet te kunnen bedienen om GitOps te doen.
18. Je hoeft niet te weten hoe een OIDC-client eruitziet om te kunnen inloggen.
19. Je hoeft geen ingressregels te schrijven om op internet te staan.
20. Je hoeft geen enkele beslissing te nemen die je toch niet kunt overzien. Dat is geen betutteling, dat is arbeidsdeling.

## Diensten van de plank

21. Vijftien diensten om uit te kiezen. Je vinkt aan wat je nodig hebt.
22. Inloggen met Rijkspas is één regel in je bestand. Normaal is dat een traject.
23. Een database is één regel. Je krijgt er een per omgeving, niet per project.
24. Objectopslag is één regel, inclusief toegangsbeleid en sleutels in een geheim.
25. Uitgaande e-mail is één regel, inclusief eigen account en dagbudget.
26. Straks is VLAM ook één regel: taalmodellen als dienst, zonder eigen sleutelbeheer.
27. Een autorisatiemuur voor je hele applicatie is één regel, en kost 32 Mi.
28. Persistente opslag is één regel, en zit meteen in het backupschema.
29. Twaalf van de 48 projecten hebben genoeg aan alleen een webadres. De andere 36 nemen er gemiddeld drie bij.
30. Een applicatie komt nooit alleen. Die "rest" is normaal het echte werk, en hier is het een vinkje.
31. `tvas-7pb` neemt alle vijftien diensten af en draait zelf op 64 Mi. Het kan dus allemaal tegelijk.
32. Elke dienst die je aanzet, weet ZAD ook weer uit te zetten. Aanzetten zonder afbouwplan is geen dienst maar een probleem.

## Preview- en PR-omgevingen

33. Een pull request is een omgeving. Eigen URL, eigen database, eigen inlog.
34. 46 van de 132 omgevingen zijn preview-omgevingen. Dat is een derde van het landschap.
35. Je preview krijgt een kloon van je data, niet een lege database.
36. `wies` draait er zeventien tegelijk. Niemand vindt dat gek.
37. Zonder eigen database per preview test je niets, want dan gaan alle PR's tegen dezelfde ontwikkeldatabase aan.
38. Je hoeft je preview niet op te ruimen. Hij gaat vanzelf slapen.
39. Slapen is `replicas: 0`. Geen pods, geen rekening. Nul.
40. Wie de URL opent krijgt een pagina "applicatie wordt gestart" en is binnen een halve minuut binnen.
41. Van de twintig omgevingen van `wies` slapen er nu vijftien. Dat scheelt dat project 133 euro per maand.
42. Je hoeft niet te kiezen tussen "handig" en "netjes". De slaapstand doet allebei.
43. Geen crawler of uptime-check kan per ongeluk je omgeving wakker maken, want in de standaardstand moet iemand klikken.
44. De discussie "welke preview-omgevingen kunnen weg" hoeft niet meer gevoerd te worden.

## Databases en data

45. 66 databases draaien nu op één instantie van 512 Mi. Dat is 7,8 Mi per database.
46. Je krijgt een echte database met eigen credentials, niet een schema in andermans database.
47. De backup is er al. Je hoeft er niet om te vragen en je hoeft hem niet in te richten.
48. Herstellen is een knop, geen procedure met een handleiding erbij.
49. Verwijder je een omgeving, dan gaat de database mee. Geen wezen.
50. Heb je PostGIS nodig, superuser-rechten of een eigen versie? Dan krijg je je eigen cluster. Twee projecten doen dat vandaag.
51. Dus je zit niet vast aan gedeeld. Je begint er alleen wel.
52. Extra schema's binnen dezelfde database kan gewoon, project-breed gedeclareerd.
53. Zonder ZAD wordt "we hebben een database nodig" automatisch "we hebben een eigen PostgreSQL nodig", en dat is zelden waar.
54. Een lege PostgreSQL kost 250 Mi voordat er één rij in staat. Die ondergrens betaal je één keer in plaats van 66 keer.
55. En soms hoort er helemaal geen database te staan. Voor een enkelvoudige applicatie doet SQLite op een volume het prima, met makkelijkere backups.
56. Objectopslag idem: één MinIO bedient de buckets van tien projecten.

## Inloggen

57. 26 realms draaien op één Keycloak van 256 Mi. Dat is 9,8 Mi per realm.
58. Je krijgt redirect-URI's voor al je omgevingen, ook voor de preview die je morgen aanmaakt.
59. Rijkspas-koppeling zit erin. Je hoeft niet te weten wat een identity provider is.
60. Wil je een tweede client erbij, dan zet je die in je projectbestand.
61. Uitnodigingen versturen is een dienst, geen scriptje dat iemand ooit schreef.
62. Een autorisatiemuur zetten voor iets dat nog niet klaar is: één regel, meteen dicht.
63. Zonder ZAD is elke realm een gesprek met een beheerder. Met ZAD is het een commit.

## Domeinen, DNS en certificaten

64. Je vraagt een subdomein aan en iemand keurt het goed. Daarna is het van jou.
65. Wie het goedkeurde en wanneer staat in je projectbestand. Dat is meteen je verantwoording.
66. Het certificaat vernieuwt zichzelf. Je hoort er nooit meer iets van.
67. Ruim 130 hostnamen draaien zo, zonder dat iemand een kalenderherinnering heeft.
68. Een eigen domein aanhangen kan ook, met dezelfde goedkeuringsroute.
69. Zonder ZAD is elke hostnaam een aanvraag, en elk certificaat een vernieuwing die iemand moet bewaken.
70. Vergeten een certificaat te vernieuwen is een klassieker. Hier kan het niet.

## Geheugen, CPU en geld

71. Elke nacht meet ZAD wat je containers werkelijk gebruiken en stelt de reservering bij.
72. De mediaan van wat containers gebruiken is 68 Mi. De mediaan van wat ZAD reserveert is 87 Mi.
73. Dat is een dekking van 1,23. Mensen die zelf invullen zitten op 3,8 tot 15.
74. De wizard stelt 256 Mi voor. Als niemand dat ooit bijstelde, betaalde de vloot bijna het dubbele.
75. 42 van de gemeten containers gebruiken minder dan 25 Mi. Niemand vult ooit 25 Mi in.
76. Je hoeft geen getal te verzinnen waar je toch naast zit.
77. Het gaat ook omhoog. `openp-4pw` reserveert 1024 Mi, gebruikt 2554 Mi, en krijgt dat gewoon.
78. Een OOM-kill verhoogt je limit dezelfde nacht nog, ook als iemand hem met de hand had vastgezet.
79. `mpfm-w3h/magazijnb` stond ooit op 4096 Mi en draait nu op 642. Niemand heeft daar iets voor hoeven doen.
80. `mpfb-8wh/redis` stond op 512 Mi en gebruikt 13 Mi. Nu staat hij op 25.
81. Je CPU-reservering is 32 millicores in plaats van de hele core die je zou invullen.
82. De hele vloot van 207 containers vraagt 6,3 cores. Met de standaardwaarde zou dat 207 cores zijn, en dan past het nergens meer.
83. Ruim mogen pieken, krap moeten reserveren. CPU is samendrukbaar, geheugen niet.
84. Wat je met de hand vastzet, blijft vastgezet. Per veld, dus een gepinde CPU blokkeert het geheugen niet.

## Git, verantwoording en beheer

85. Alles staat in één leesbaar bestand: wie erbij mag, welke domeinen goedgekeurd zijn, wat er draait.
86. Elke aanpassing van de tuner staat in git, met tijdstempel en reden erbij: *"Request: VPA target 259Mi = 259Mi."*
87. Je hoeft geen dashboard te bevragen. Je opent een bestand.
88. De historie overleeft een clusterverhuizing, want hij staat niet in het cluster.
89. Met ZAD beheert iemand zeven dingen. Zonder ZAD zijn dat er 67.
90. Eén platform betekent één keer goed in plaats van 48 keer half.
91. Een nieuw project kost het platform bijna niets extra. De onderlaag schaalt niet mee.
92. En ja: 853 euro per maand in plaats van 4.633. Maar dat was het doel niet, dat is wat je overhoudt.

## Als het misgaat

93. Je omgeving valt niet stil omdat iemand vergat de limit te verhogen.
94. Gaat de gedeelde database om, dan gaat iedereen mee. Dat is de prijs van delen, en die noemen we hardop.
95. In februari hield één project 75 van de 100 connectieslots bezet en lag de authenticatie van het cluster eruit. Daar zitten nu limieten en gereserveerde slots op.
96. Luidruchtige buren zijn echt, en het antwoord is niet "dan maar iedereen zijn eigen instantie".
97. Een slapende omgeving start koud op. Sessies en caches overleven het niet, en voor een preview is dat prima.

## Eerlijk over wat het niet is

98. ZAD dwingt je niet om te delen. Het maakt delen mogelijk, en bij bescheiden eisen is dat gewoon de betere deal.
99. Een agent schrijft in een minuut een Helm-chart, en dat was nooit het moeilijke deel. Wat hij niet krijgt is een DNS-record in een rijkszone, een realm op Rijkspas of een goedgekeurd subdomein met een spoor van wie het goedkeurde.
100. Het projectbestand is de API. ZAD is niet wat de agent vervangt, het is wat de agent aanroept.
