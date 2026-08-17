Jullie waarneming klopt: op `*.regelrecht.rijks.app` komt ALPN niet tot een afspraak en valt de verbinding terug op HTTP/1.1. Dat ligt niet aan jullie routes of certificaten, HTTP/2 staat uit op de ingress-controller.

Wij zijn dat aan het uitzoeken met het platformteam. Het is een instelling op de ingress-controller die alle RIG-routes bedient, dus niet iets dat per route of per project aan te zetten is. Voordat dat gebeurt willen we zeker weten dat er geen onverwachte of vervelende neveneffecten zijn, onder andere voor applicaties met WebSockets en voor backends die plots meer gelijktijdige requests krijgen.

Het voordeel voor jullie is reëel: nu opent een browser maximaal zes verbindingen per host en staan de 23 assets van de editor daarachter in de rij. Met HTTP/2 gaan ze gemultiplext over één verbinding en vervalt die wachtrij.

Tegelijk hebben we even meegekeken naar wat de editor over de lijn stuurt, en daar is ook een grote winst te behalen, in dingen die jullie zelf in de hand hebben.

**Compressie staat uit.** De webserver stuurt niets gecomprimeerd, ongeacht wat de browser aanbiedt:

```
Accept-Encoding: gzip                      -> content-length: 187102
Accept-Encoding: br                        -> content-length: 187102
Accept-Encoding: gzip, deflate, br, zstd   -> content-length: 187102
```

De 23 assets uit de initiële HTML zijn samen 2626 KiB. Met gewone gzip is dat 723 KiB, dus 72% minder. De grootste posten:

```
index-CueQL6N2.js        1135540 ruw  ->  257104 gzip
OverviewView-0adsTM_C.js  549953 ruw  ->  184492 gzip
dist-CAai7vqM.js          313838 ruw  ->   99655 gzip
index-BLTm_raQ.css        187102 ruw  ->   18699 gzip   (90% eraf)
```

**Er zitten geen cache-headers op de assets.** De bestandsnamen zijn content-gehasht, dus die bestanden zijn per definitie onveranderlijk en kunnen `Cache-Control: public, max-age=31536000, immutable` krijgen op `/assets/*`, met `no-cache` op de `index.html` zelf. Dan hoeft een terugkerende bezoeker die 2,6 MB helemaal niet meer op te halen, ook geen revalidatie per bestand.

Eén ding om te weten als je zelf gaat meten: de `cache-control: private` die je nu in de responses ziet komt niet van jullie webserver maar van onze router, die er een sticky-session-cookie in zet. Die blokkeert alleen gedeelde caches en niet de browsercache zelf, dus zodra jullie eigen `max-age` erop zit werkt het gewoon.

**En even waard om te noemen:** in de HTML staan 20 `<link rel="modulepreload">`, waaronder 557 KiB aan routeviews (`OverviewView`, `DataTable`, `TrajectCreateForm`, `DetailPanel`) die pas nodig zijn als iemand die pagina opent. Die verdwijnen daar vanzelf uit als ze via `component: () => import('./views/OverviewView.vue')` in de router komen in plaats van als statische import. Die ene `OverviewView` van 550 KB is trouwens op zichzelf de moeite van het bekijken waard, dat ruikt naar een zware dependency die in die chunk beland is.

We houden jullie op de hoogte van het HTTP/2-verzoek.
