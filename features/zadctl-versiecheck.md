# zadctl-versiecheck

Een oude zadctl die niet meer met de API overweg kan, faalt nu op een manier die niets uitlegt: een 401, een 422 of een veld dat ontbreekt. Met deze check kan het platform zeggen dat de versie te oud is, en welke versie wel werkt.

## Wat het doet

zadctl stuurt `User-Agent: zadctl/X.Y.Z`. Op elk pad onder `/api/`:

- versie lager dan `ZADCTL_MIN_VERSION`: **426 Upgrade Required**, met header `X-ZADCTL-Minimum` en body `{"detail", "client", "version", "minimum", "upgrade"}`. Dit gebeurt vóór sessie en autorisatie.
- versie lager dan `ZADCTL_RECOMMENDED_VERSION`: het verzoek gaat door, het antwoord krijgt header `X-ZADCTL-Recommended`.
- geen `zadctl/X.Y.Z` in de User-Agent: niets. Andere clients en zadctl-versies van voor deze afspraak (die `python-httpx/...` sturen) blijven werken.

`GET /version` noemt beide grenzen onder `clients.zadctl` (`minimum`, `recommended`, `null` als niet gezet), zodat zadctl ze kan lezen zonder sleutel, bijvoorbeeld vóór het inloggen.

## Configuratie

| Setting | Default | Betekenis |
|---|---|---|
| `ZADCTL_MIN_VERSION` | leeg (uit) | Lager wordt geweigerd met 426 |
| `ZADCTL_RECOMMENDED_VERSION` | leeg (uit) | Lager krijgt een waarschuwing |

Beide als `X.Y.Z`; een andere vorm faalt bij de opstart. Ze staan per omgeving in de configmap van `bootstrap/rig-system/kustomize/operations-manager/overlays/*`.

Hoog het minimum op bij een API-wijziging waar oudere zadctl-versies aantoonbaar stuk op gaan, en noem de versie die de wijziging volgt. De aanbevolen versie is voor alles daaronder: wat werkt, maar iets mist.

## Code

- `opi/core/zadctl_version.py`: User-Agent en settings lezen.
- `opi/middleware/zadctl_version.py`: de 426 en de header.
- `opi/server.py`: de middleware, en de 426 op elke `/api/`-operatie in het OpenAPI-document.
