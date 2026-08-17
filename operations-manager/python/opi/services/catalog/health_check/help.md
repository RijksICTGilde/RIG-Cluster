# Health check

Bepaal zelf hoe Kubernetes controleert of je component gezond is: met welk protocol, op welke poort en op welke paden.

## Let op: je component wordt altijd gecontroleerd

Anders dan bij de andere services betekent "niet aanvinken" hier niet "geen controle". Zonder deze service wordt je component op TCP-niveau gecontroleerd op zijn eerste inbound-poort. Deze service kies je om die controle ergens anders of anders te laten uitvoeren.

## Wanneer gebruik je dit?

- Je hebt een echt gezondheidspad, bijvoorbeeld /healthz, dat meer zegt dan een open poort
- Je gezondheidsendpoint zit op een andere poort dan je functionele poort
- Je functionele poort dwingt mTLS af en logt elke blinde TCP-controle als fout
- Je wilt controles uitzetten voor een component dat geen poort openzet

## Wat wordt er ingesteld?

Je kiest een **scheme** (**tcp**, **http**, **https** of **none**), de poort waarop gecontroleerd wordt, en de paden voor de liveness- en readiness-probe. Die waarden worden in de gegenereerde deployment gezet. Er wordt verder niets aangemaakt: deze service verandert alleen gedrag.
