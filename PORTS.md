# Poorten op je eigen machine

Twee dingen willen hier dezelfde poorten: het lokale kind-cluster en de forward naar de sandbox op de server. Ze kunnen niet allebei tegelijk, en dat is de hele reden dat dit document bestaat.

## Wie claimt wat

| Poort | Wie | Wanneer |
|---|---|---|
| 80 | kind-ingress **of** `sandbox-forward.sh` | zodra een van de twee draait |
| 443 | kind-ingress **of** `sandbox-forward.sh` | zodra een van de twee draait |
| 9595 | Skaffold → `operations-manager:8000` | tijdens `task sandbox:skaffold-dev` |
| 5678 | Skaffold → debugpy in de OPI-pod | tijdens `task sandbox:skaffold-debug` |
| 8000 | Operations Manager | `docker-compose.dev.yaml` |
| 5432 | PostgreSQL | `docker-compose.dev.yaml` |
| 3000 | Forgejo | alleen bij de `kubectl port-forward`-fallback in de setup |

Poort 80 en 443 komen bij kind uit `kind-config.yaml` (`extraPortMappings`), en bij de forward uit de twee `-L`-regels in het script. 9595 en 5678 staan in de drie `operations-manager/skaffold*.yaml`.

## De sandbox-forward

Alle sandboxdomeinen (`*.sandbox.rijksapp.dev`) resolven op deze machine al naar `127.0.0.1`. De forward zet een ssh-tunnel op van je lokale 80/443 naar de ingress van de sandboxserver, waarna al die domeinen in één klap tegen de server werken in plaats van tegen een lokaal cluster. Geen regels in `/etc/hosts` per hostnaam, en niets aanpassen als er een project bijkomt.

```bash
task sandbox:forward -- on       # aanzetten (vraagt sudo)
task sandbox:forward -- status   # staat hij aan, en antwoordt de ingress
task sandbox:forward -- off      # uitzetten, 80/443 weer vrij
```

Het script eronder is `scripts/sandbox-forward.sh` en accepteert dezelfde drie woorden. Zonder argument doet de task `status`.

`on` vraagt sudo omdat 80 en 443 onder 1024 liggen en alleen root die mag binden. De ssh draait daardoor als root, en dat is ook waarom het script gebruiker, sleutel en poort expliciet meegeeft: root leest `/var/root/.ssh/config` en niet die van jou.

`off` stopt het ssh-proces en ruimt het pidfile op. Staat er geen pidfile meer maar houdt er nog wel iets 443 vast (een wees uit een eerdere run), dan stopt `off` dat proces alsnog.

`status` kijkt niet alleen of de poort leeft, maar doet ook een echte `curl` naar `argo.sandbox.rijksapp.dev` via `--resolve`. Een luisterende poort betekent immers nog niet dat de ingress aan de overkant antwoordt.

## De botsing

Draait je lokale kind-sandbox, dan houdt die 80 en 443 al bezet. `on` merkt dat, noemt de pid die de poort vasthoudt, en stopt. Hij start dan niet half op.

Andersom net zo: wil je terug naar het lokale cluster terwijl de forward aan staat, dan eerst `task sandbox:forward -- off`.

Blijft er iets hangen, dan zie je met `sudo lsof -nP -iTCP:443 -sTCP:LISTEN` wie de poort heeft. Sudo is nodig, want de forward draait als root en een gewone `lsof` ziet die processen niet.

## Instellingen

Overschrijven kan met omgevingsvariabelen; de standaardwaarden komen uit je `~/.ssh/config` voor de betreffende host.

| Variabele | Standaard |
|---|---|
| `SANDBOX_SERVER` | `192.168.1.101` |
| `SANDBOX_SSH_USER` | `ssh -G <server>` → `user` |
| `SANDBOX_SSH_KEY` | `ssh -G <server>` → `identityfile` |
| `SANDBOX_SSH_PORT` | `ssh -G <server>` → `port` |
| `SANDBOX_FORWARD_PIDFILE` | `/tmp/zad-sandbox-forward.pid` |

De tunnel wijst naar het LAN-adres van de server, niet naar diens `127.0.0.1`: de ingress is daar wel bereikbaar en op de loopback van de server niet.
