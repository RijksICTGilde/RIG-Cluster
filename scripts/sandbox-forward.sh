#!/usr/bin/env bash
# Stuur lokaal 80/443 door naar de sandbox op de server, en weer terug.
#
# Alle sandboxdomeinen resolven hier al naar 127.0.0.1, dus met deze forward werken ze
# allemaal in een klap: het verkeer gaat naar de ingress op de server in plaats van naar
# een lokaal cluster. Geen /etc/hosts-regels per hostnaam bijhouden, en geen aanpassing
# als er een project bijkomt.
#
#   scripts/sandbox-forward.sh on       forward aanzetten (vraagt sudo)
#   scripts/sandbox-forward.sh off      forward uitzetten
#   scripts/sandbox-forward.sh status   kijken of hij aan staat, en of hij antwoordt
#
# Waarom sudo: 80 en 443 zijn poorten onder 1024 en die mag alleen root binden. De ssh
# draait daardoor als root, dus de identiteit wordt expliciet meegegeven; anders zou ssh
# in /var/root/.ssh gaan zoeken.
#
# LET OP: draait je LOKALE kind-sandbox, dan houdt die 80/443 al bezet en kan de forward
# niet binden. Het script zegt dat dan, in plaats van half te starten.

set -euo pipefail

SERVER="${SANDBOX_SERVER:-192.168.1.101}"

# Jouw ~/.ssh/config bepaalt gebruiker, sleutel en poort voor deze host. Onder sudo leest
# ssh /var/root/.ssh/config en niet die van jou, dus die regels vallen daar weg. Vandaar:
# eerst als jezelf de effectieve config uitlezen met `ssh -G`, en die waarden expliciet
# meegeven aan de ssh die als root draait. Anders raadt het script de gebruiker, en dat
# ging mis (het nam de lokale accountnaam terwijl de config `claude` zegt).
_ssh_config_value() {
    ssh -G "$SERVER" 2>/dev/null | awk -v k="$1" 'tolower($1)==k {print $2; exit}'
}
SSH_USER="${SANDBOX_SSH_USER:-$(_ssh_config_value user)}"
SSH_KEY="${SANDBOX_SSH_KEY:-$(_ssh_config_value identityfile)}"
SSH_KEY="${SSH_KEY/#\~/$HOME}"
SSH_PORT="${SANDBOX_SSH_PORT:-$(_ssh_config_value port)}"
PIDFILE="${SANDBOX_FORWARD_PIDFILE:-/tmp/zad-sandbox-forward.pid}"
PORTS=(80 443)

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

running_pid() {
    [ -f "$PIDFILE" ] || return 1
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
    # ps -p en niet kill -0: de forward draait als root, en een signaal sturen naar een
    # proces van root mag jij niet, waardoor kill -0 faalt en dit "staat uit" concludeerde
    # terwijl hij gewoon draaide. ps ziet processen van iedereen.
    [ -n "$pid" ] && ps -p "$pid" -o pid= >/dev/null 2>&1 || return 1
    echo "$pid"
}

port_holder() {
    # Wie luistert er op deze poort, ongeacht of wij dat zijn.
    #
    # Met sudo, want de forward zelf draait als root en een gewone lsof ziet processen van
    # root niet. Zonder sudo concludeerde dit dat de forward niet draaide terwijl hij
    # gewoon stond, en brak `on` af op zijn eigen controle.
    #
    # -n op sudo: nooit om een wachtwoord vragen puur voor een controle. Kan het niet
    # zonder prompt, dan val terug op de gewone lsof; die ziet dan alleen eigen processen,
    # wat voor de bezet-check nog steeds beter is dan niets.
    # Sluit af met `|| true`: "niets gevonden" is hier een geldig antwoord, geen fout. Zonder
    # dat geeft lsof status 1, maakt `pipefail` de hele pijplijn niet-nul, en breekt `set -e`
    # het script af op zijn eigen controle -- stil, zonder ook maar om sudo te vragen.
    {
        sudo -n lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null ||
            lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null
    } | head -1 || true
}

cmd_on() {
    local pid
    if pid=$(running_pid); then
        echo "Staat al aan (pid ${pid}). Uitzetten met: $0 off"
        exit 0
    fi

    local port holder
    for port in "${PORTS[@]}"; do
        holder=$(port_holder "$port")
        if [ -n "$holder" ]; then
            echo "Poort ${port} is al bezet door pid ${holder} ($(ps -p "$holder" -o comm= 2>/dev/null))." >&2
            echo "Draait je lokale kind-sandbox? Stop die eerst, anders kan de forward niet binden." >&2
            exit 1
        fi
    done

    [ -n "$SSH_USER" ] || { echo "Geen ssh-gebruiker gevonden voor ${SERVER}; zet SANDBOX_SSH_USER." >&2; exit 1; }
    [ -r "$SSH_KEY" ] || { echo "Geen leesbare ssh-sleutel op '${SSH_KEY}'; zet SANDBOX_SSH_KEY." >&2; exit 1; }

    echo "Forward aanzetten naar ${SSH_USER}@${SERVER}:${SSH_PORT} (sudo nodig voor poort 80/443)..."
    # -N: geen commando, alleen forwarden. -f: naar de achtergrond zodra de verbinding staat.
    # ExitOnForwardFailure zorgt dat een mislukte bind ook echt als fout terugkomt in plaats
    # van een ssh die vrolijk zonder forward doorleeft.
    # UserKnownHostsFile expliciet: als root heeft ssh een eigen (lege) known_hosts, en met
    # BatchMode weigert hij dan op een onbekende hostsleutel in plaats van te vragen.
    # Doel is het LAN-adres van de server en niet zijn 127.0.0.1: de ingress is daar wel
    # bereikbaar en op de loopback van de server niet (getest: curl naar 127.0.0.1 geeft daar
    # geen verbinding, naar het LAN-adres wel).
    sudo ssh -f -N \
        -i "$SSH_KEY" \
        -o BatchMode=yes \
        -o UserKnownHostsFile="$HOME/.ssh/known_hosts" \
        -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=30 \
        -p "$SSH_PORT" \
        -L "127.0.0.1:80:${SERVER}:80" \
        -L "127.0.0.1:443:${SERVER}:443" \
        "${SSH_USER}@${SERVER}"

    # ssh -f daemoniseert zelf, dus de pid komt van de poort en niet van $!.
    local listener
    listener=$(port_holder 443)
    if [ -z "$listener" ]; then
        echo "De forward lijkt niet te draaien; controleer de ssh-verbinding naar ${SERVER}." >&2
        exit 1
    fi
    echo "$listener" | sudo tee "$PIDFILE" >/dev/null
    sudo chmod 644 "$PIDFILE"

    echo "Aan. Alle sandboxdomeinen die naar 127.0.0.1 wijzen gaan nu naar ${SERVER}."
    cmd_status
}

cmd_off() {
    local pid
    if ! pid=$(running_pid); then
        # Geen pidfile, maar misschien wel een wees uit een eerdere run.
        pid=$(port_holder 443)
        if [ -z "$pid" ]; then
            echo "Staat al uit."
            sudo rm -f "$PIDFILE" 2>/dev/null || true
            return
        fi
        echo "Geen pidfile, maar pid ${pid} houdt 443 nog vast; die stop ik."
    fi
    sudo kill "$pid" 2>/dev/null || true
    sudo rm -f "$PIDFILE" 2>/dev/null || true
    echo "Uit. 80 en 443 zijn weer vrij voor je lokale cluster."
}

cmd_status() {
    local pid
    if ! pid=$(running_pid); then
        echo "Uit."
        return
    fi
    echo "Aan (pid ${pid}) naar ${SERVER}."
    # Een levende poort zegt nog niet dat de ingress antwoordt, dus even echt vragen.
    #
    # Met --resolve en niet met -H "Host:". Een Host-kop komt pas ná de TLS-handshake, en
    # die handshake gebruikt de naam uit de URL als SNI. Vroeg dit dus https://127.0.0.1
    # met een Host-kop, dan was de SNI het IP, wees niemand aan de andere kant een site
    # aan en faalde de verbinding -- waarna dit "antwoordt niet" meldde terwijl de forward
    # gewoon stond en alle domeinen het deden.
    local host="argo.sandbox.rijksapp.dev"
    local code
    code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
        --resolve "${host}:443:127.0.0.1" "https://${host}/" 2>/dev/null) || code=""
    case "$code" in
        "" | 000) echo "  ${host} antwoordt niet; staat de forward echt en draait de ingress?" ;;
        *) echo "  ${host} -> ${code}" ;;
    esac
}

case "${1:-}" in
    on) cmd_on ;;
    off) cmd_off ;;
    status) cmd_status ;;
    -h | --help | "") usage 0 ;;
    *) echo "Onbekend commando: $1" >&2; usage 1 ;;
esac
