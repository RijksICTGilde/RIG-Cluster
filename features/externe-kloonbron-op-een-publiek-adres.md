# Externe kloonbron op een publiek adres

## Wat het is

Een project kan een database of bucket van buiten naar binnen klonen, met zijn API-key of
met `clone-from: remote-source` in zijn projectbestand. De verbinding legt de OPI-pod zelf,
dus met een zelfgekozen bronhost kon een project OPI naar een adres binnen het cluster
laten verbinden. Dat kan niet meer: de host die onze pod aanspreekt moet naar een publiek
adres resolven.

Daarnaast staan de wachtwoorden uit zo'n verzoek versleuteld in de takentabel, en zegt een
mislukte verbinding naar buiten niet meer welke van de drie dingen misging.

## Wat er wordt getoetst

| Wat | Getoetst |
|---|---|
| `sourceHost` (direct, zonder tunnel) | ja |
| `tunnel.serverUrl` / `chisel.server-url` | ja |
| `tunnel.remoteHost` en de `host` van een service achter chisel | nee: die resolven aan de kant van de chisel-server |

Een host is toegestaan als elk adres waarnaar hij resolvet globaal is. Een host die niet
resolvet wordt geweigerd: anders verklapt het verschil welke interne namen bestaan. De
toets zelf staat in `opi/utils/egress_guard.py` en is dezelfde die de skopeo-connector
voor een registry gebruikt.

De weigering komt op twee plekken:

- in de routes (v1 en v2), vóór de taak wordt aangemaakt: HTTP 422 met één vaste tekst;
- in `clone_database_from_external_source` en `clone_bucket_from_external_source`, zodat
  ook de weg via `remote-sources` in het projectbestand hem haalt, en een DNS-wijziging
  tussen route en taak ook. Daar is het een fout van de taak.

## Een privaat bereik toestaan

Een cluster dat echt een privaat bereik nodig heeft, zet dat in `CLUSTER_CONFIG`
(`opi/core/cluster_config.py`) met de reden erbij:

```python
"odcn-production": {
    "external_clone_allowed_private_networks": ["10.20.0.0/16"],
}
```

Ontbreekt de sleutel, dan is de lijst leeg, en dat is de stand op elk cluster. Dit is een
platformbesluit in git; een project kan het niet zetten.

## Wachtwoorden in de takentabel

De routes versleutelen `sourcePassword`, `sourceSecretKey` en `tunnel.password` met de
AGE-sleutel van het cluster (`settings.SOPS_AGE_PUBLIC_KEY`) voordat de payload naar
`create_async_task` gaat. De taakhandler ontsleutelt met `decrypt_password_smart_auto`
vlak voor gebruik, en leest een oudere payload in klare tekst nog gewoon.

Payloads van vóór deze wijziging kunnen nog in klare tekst in `async_tasks` staan, en in
een nachtelijke dump van de platformdatabase, tot hun bewaartijd verstrijkt.

## Eén melding bij een mislukte verbinding

Onbereikbaar, een onbekende database of bucket en een fout wachtwoord leveren naar buiten
dezelfde tekst: "de bron is niet bereikbaar of weigerde de verbinding". Het onderscheid,
met host en poort, staat in de log van de operations-manager.

## Afhankelijkheden

- `opi/utils/egress_guard.py`: de toets, gedeeld met `opi/connectors/skopeo.py`
- `opi/core/cluster_config.py`: `get_external_clone_allowed_private_networks`
- `opi/utils/age.py`: `encrypt_age_content` en `decrypt_password_smart_auto`
