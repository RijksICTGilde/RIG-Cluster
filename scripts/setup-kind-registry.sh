#!/usr/bin/env bash
#
# setup-kind-registry.sh - zet een image-registry NAAST het kind-cluster neer en sluit
# de nodes erop aan.
#
# Waarom naast en niet in het cluster: de registry moet een clusterherbouw overleven.
# De in-cluster rig-registry bewaart zijn lagen op een PVC, dus na een reset is alles
# weg en kost de eerste deploy weer het volledige image. Deze container leeft naast het
# cluster en houdt zijn lagen, ook als het cluster verdwijnt.
#
# Dit is de officiele kind-recipe (https://kind.sigs.k8s.io/docs/user/local-registry/):
# registry-container op 127.0.0.1:5000, een hosts.toml per node, de registry aan het
# kind-netwerk, en de local-registry-hosting configmap in kube-public.
#
# VEREIST in de kind-config van het cluster:
#
#   containerdConfigPatches:
#   - |-
#     [plugins."io.containerd.grpc.v1.cri".registry]
#       config_path = "/etc/containerd/certs.d"
#
# Zonder die regel leest containerd de hosts.toml niet en pullt de node alsnog niets.
# Dit script controleert het en weigert als het ontbreekt: de fout is anders stil en
# komt pas boven bij de eerste deploy, en een kind-config geldt alleen bij `create`,
# dus repareren kost dan een clusterherbouw.
#
# Gebruik: scripts/setup-kind-registry.sh [--cluster NAAM]
#
# Omgevingsvariabelen:
#   KIND_CLUSTER_NAME    clusternaam (standaard rig-sandbox, --cluster gaat voor)
#   KIND_REGISTRY_NAME   containernaam (standaard kind-registry)
#   KIND_REGISTRY_PORT   hostpoort op 127.0.0.1 (standaard 5000)
#   KIND_REGISTRY_IMAGE  registry-image (standaard registry:2)
#
# Exitcodes: 0 = klaar, 1 = de kind-config mist containerdConfigPatches, 2 = fout gebruik.

set -euo pipefail

CLUSTER="${KIND_CLUSTER_NAME:-rig-sandbox}"
REG_NAME="${KIND_REGISTRY_NAME:-kind-registry}"
REG_PORT="${KIND_REGISTRY_PORT:-5000}"
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

# 1. De registry-container. Bestaat hij maar staat hij stil, dan starten we hem: een
# `docker run` met dezelfde naam zou daarop stuklopen.
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

# 2. Elke node naar de registry wijzen. De hostnaam in hosts.toml is de containernaam op
# het kind-netwerk, niet localhost: de node zoekt hem daar, niet op de host.
REGISTRY_DIR="/etc/containerd/certs.d/localhost:${REG_PORT}"
for node in $(kind get nodes --name "$CLUSTER"); do
    if ! docker exec "$node" grep -qE '^[[:space:]]*config_path[[:space:]]*=' /etc/containerd/config.toml; then
        echo "[kind-registry] node $node leest geen /etc/containerd/certs.d." >&2
        echo "[kind-registry] De kind-config van dit cluster mist containerdConfigPatches;" >&2
        echo "[kind-registry] zonder die regel wordt hosts.toml genegeerd. Zie de kop van dit" >&2
        echo "[kind-registry] script en sandboxed-local/kind-config.yaml, en bouw het cluster opnieuw." >&2
        exit 1
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

# 4. Vastleggen waar de registry zit, zoals KEP-1755 voorschrijft. Gereedschap dat een
# lokale registry zoekt (skaffold, tilt) leest deze configmap.
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
