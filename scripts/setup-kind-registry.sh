#!/usr/bin/env bash
#
# setup-kind-registry.sh - zet een image-registry NAAST het kind-cluster neer en sluit
# de nodes erop aan, volgens https://kind.sigs.k8s.io/docs/user/local-registry/.
#
# Waarom naast het cluster, en de containerdConfigPatches die de kind-config daarvoor
# nodig heeft: docs/sandbox-kind-registry.md. Ontbreekt die regel, dan leest containerd
# de hosts.toml niet en weigert dit script; de fout is anders stil tot de eerste deploy.
#
# Gebruik: scripts/setup-kind-registry.sh [--cluster NAAM]
#
# Omgevingsvariabelen:
#   KIND_CLUSTER_NAME    clusternaam (standaard rig-sandbox, --cluster gaat voor)
#   KIND_REGISTRY_NAME   containernaam (standaard kind-registry)
#   KIND_REGISTRY_PORT   hostpoort op 127.0.0.1 (standaard 5001, 5000 is bezet op de dev-server)
#   KIND_REGISTRY_IMAGE  registry-image (standaard registry:2)
#
# Exitcodes: 0 = klaar, 2 = fout gebruik, 3 = het cluster heeft geen nodes,
# 4 = de kind-config mist containerdConfigPatches. Anders: de code van het mislukte commando
# (docker exec op een gestopte node geeft zelf 1, vandaar geen 1 voor de weigering).

set -euo pipefail

CLUSTER="${KIND_CLUSTER_NAME:-rig-sandbox}"
REG_NAME="${KIND_REGISTRY_NAME:-kind-registry}"
REG_PORT="${KIND_REGISTRY_PORT:-5001}"
REG_IMAGE="${KIND_REGISTRY_IMAGE:-registry:2}"

while [ $# -gt 0 ]; do
    case "$1" in
        --cluster)
            [ $# -ge 2 ] || { echo "[kind-registry] --cluster mist een naam" >&2; exit 2; }
            CLUSTER="$2"
            shift 2
            ;;
        --cluster=*)
            CLUSTER="${1#*=}"
            shift
            ;;
        *)
            echo "[kind-registry] onbekende optie: $1" >&2
            exit 2
            ;;
    esac
done

# kind get nodes geeft ook exit 0 voor een cluster dat niet bestaat.
nodes="$(kind get nodes --name "$CLUSTER")"
if [ -z "$nodes" ]; then
    echo "[kind-registry] cluster $CLUSTER heeft geen nodes, bestaat het wel?" >&2
    exit 3
fi

# 1. Bestaat de container maar staat hij stil, dan starten we hem: een `docker run` met
# dezelfde naam zou daarop stuklopen.
reg_state="$(docker inspect -f '{{.State.Running}}' "$REG_NAME" 2>/dev/null || true)"
if [ "$reg_state" = "true" ]; then
    echo "[kind-registry] registry $REG_NAME draait al"
elif [ -n "$reg_state" ]; then
    echo "[kind-registry] registry $REG_NAME bestaat maar staat stil, starten"
    docker start "$REG_NAME"
else
    echo "[kind-registry] registry $REG_NAME starten op 127.0.0.1:${REG_PORT}"
    docker run -d --restart=always -p "127.0.0.1:${REG_PORT}:5000" \
        --network bridge --name "$REG_NAME" "$REG_IMAGE"
fi

# 2. De hostnaam in hosts.toml is de containernaam op het kind-netwerk, niet localhost:
# de node zoekt de registry daar, niet op de host.
REGISTRY_DIR="/etc/containerd/certs.d/localhost:${REG_PORT}"
for node in $nodes; do
    # Eerst ophalen, dan toetsen: een gestopte node mag niet lezen als een ontbrekende patch.
    config="$(docker exec "$node" cat /etc/containerd/config.toml)"
    if ! grep -qE '^[[:space:]]*config_path[[:space:]]*=' <<<"$config"; then
        echo "[kind-registry] node $node leest geen /etc/containerd/certs.d." >&2
        echo "[kind-registry] De kind-config van dit cluster mist containerdConfigPatches." >&2
        echo "[kind-registry] Voeg ze toe (docs/sandbox-kind-registry.md) en bouw het cluster opnieuw." >&2
        exit 4
    fi
    docker exec "$node" mkdir -p "$REGISTRY_DIR"
    printf '[host."http://%s:5000"]\n' "$REG_NAME" |
        docker exec -i "$node" cp /dev/stdin "${REGISTRY_DIR}/hosts.toml"
    echo "[kind-registry] node $node wijst naar $REG_NAME"
done

# 3. De registry aan het kind-netwerk, anders kan de node hem niet bereiken.
if [ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "$REG_NAME")" = "null" ]; then
    echo "[kind-registry] registry aan het kind-netwerk hangen"
    docker network connect kind "$REG_NAME"
fi

# 4. KEP-1755: gereedschap dat een lokale registry zoekt (skaffold, tilt) leest deze
# configmap.
kubectl --context "kind-${CLUSTER}" apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:${REG_PORT}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
EOF

echo "[kind-registry] klaar: push naar localhost:${REG_PORT}/<image>, het cluster pullt hem zelf"
