#!/usr/bin/env bash
#
# Genereert de geheimen van een cluster uit de blauwdrukken in TEMPLATES_DIR.
#
# Dit stond als 250 regels shell in de Taskfile onder `_generate-secrets-shared`. Het staat
# hier omdat het geen taak is maar een programma: het heeft lussen, vertakkingen en
# toestand. Een taak die een binary aanroept hoort in de Taskfile; code hoort in een bestand
# dat je kunt lezen, los kunt draaien en kunt testen.
#
# Alle invoer komt uit omgevingsvariabelen en niet uit vlaggen. Dat is geen smaak: een
# fundament-plugin krijgt zijn configuratie als FUNP_-omgevingsvariabelen op zijn
# Deployment, dus een script dat zo gevoed wordt past daar zonder tussenlaag in.
#
# ======================================================================================
# TWEE BESTEMMINGEN (BESTEMMING=git of BESTEMMING=cluster)
# ======================================================================================
#
# git     - versleutelt met SOPS naar OUTPUT_DIR/CLUSTER_FOLDER. ArgoCD past ze toe, dus
#           er is reconciliatie, een diff en een terugweg. Dit is wat elk cluster vandaag
#           doet en het blijft de standaard.
#
# cluster - past ze rechtstreeks toe met kubectl. Geen bestand, geen AGE-sleutel, geen
#           kip-en-ei waarin de sleutel moet bestaan voordat er iets versleuteld kan worden.
#           Niets reconcilieert ze daarna: wat je hier neerzet blijft staan tot iemand het
#           weghaalt.
#
# DIE KEUZE IS NIET GRATIS EN HET VERSCHIL IS PRINCIPIEEL. Met `git` is git de bron en wint
# git van het cluster: een wachtwoord dat je in het cluster wijzigt draait bij de volgende
# sync terug. Met `cluster` is het cluster de bron en is er niets dat een verdwenen geheim
# terugbrengt. Wat je in beide gevallen wilt is dat het wachtwoord in het Secret op het
# cluster HET echte wachtwoord is; welke van de twee bestemmingen daar het beste bij past
# is een openstaande vraag, uitgeschreven in
# plans/de-installatie-in-drie-fasen-keuzes-geheimen-en-een-overdracht.md.
#
# ======================================================================================
# TWEE PADEN PER GEHEIM, in allebei de bestemmingen
# ======================================================================================
#
#   1. Het geheim bestaat NIET  -> volledig aanmaken.
#   2. Het geheim bestaat WEL   -> alleen ONTBREKENDE velden aanvullen.
#
# Pad 2 bestond niet. De oude versie sloeg een bestaand bestand in zijn geheel over, met
# "Skipping (already exists)" en een vinkje. Dat beschermt tegen ongevraagde rotatie, en dat
# blijft zo, maar het betekende ook dat een veld dat later aan een blauwdruk werd toegevoegd
# op een draaiend cluster NOOIT meer landde. Dat is geen theorie: op odcn mist
# `keycloak-admin-secret` vier velden die de blauwdruk wel heeft.
#
# Wat dit script NOOIT doet: een bestaande waarde overschrijven of een veld verwijderen.
# Rotatie is een operationele handeling met een uitrol eromheen, geen bijverschijnsel van
# een generator die toevallig langsloopt.

set -euo pipefail

# --- invoer ------------------------------------------------------------------

: "${MODE:?MODE is verplicht (infrastructure of bootstrap)}"
: "${TEMPLATES_DIR:?TEMPLATES_DIR is verplicht}"
: "${CLUSTER_TYPE:?CLUSTER_TYPE is verplicht}"
: "${RIG_NAMESPACE:?RIG_NAMESPACE is verplicht}"
BESTEMMING="${BESTEMMING:-git}"
FIXED_PASSWORD="${FIXED_PASSWORD:-}"
CLUSTER_NAME="${CLUSTER_NAME:-$CLUSTER_TYPE}"

case "$BESTEMMING" in
  git|cluster) ;;
  *) echo "FOUT: BESTEMMING moet git of cluster zijn, niet '$BESTEMMING'" >&2; exit 1 ;;
esac

if [ ! -d "$TEMPLATES_DIR" ]; then
  echo "FOUT: TEMPLATES_DIR bestaat niet: $TEMPLATES_DIR" >&2
  exit 1
fi

if [ "$BESTEMMING" = "git" ]; then
  : "${OUTPUT_DIR:?OUTPUT_DIR is verplicht bij BESTEMMING=git}"
  : "${CLUSTER_FOLDER:?CLUSTER_FOLDER is verplicht bij BESTEMMING=git}"
  : "${KEY_FILE:?KEY_FILE is verplicht bij BESTEMMING=git}"
  if [ ! -f "$KEY_FILE" ]; then
    echo "FOUT: AGE-sleutelbestand niet gevonden: $KEY_FILE" >&2
    exit 1
  fi
  DOELMAP="$OUTPUT_DIR/$CLUSTER_FOLDER"
  AGE_PUBLIEKE_SLEUTEL="$(sed -n '2p' "$KEY_FILE" | sed 's/# public key: //')"
  if [ -z "$AGE_PUBLIEKE_SLEUTEL" ]; then
    echo "FOUT: geen publieke sleutel op regel 2 van $KEY_FILE" >&2
    exit 1
  fi
  # Aanvullen bewerkt een bestaand versleuteld bestand, en daar is de PRIVE-sleutel voor
  # nodig; een nieuw bestand versleutelen heeft genoeg aan de publieke. Alleen zetten als
  # hij nog niet gezet is, zodat een omgeving die de sleutel via een andere weg aanlevert
  # niet overschreven wordt.
  if [ -z "${SOPS_AGE_KEY:-}" ]; then
    SOPS_AGE_KEY="$(sed -n '3p' "$KEY_FILE")"
    export SOPS_AGE_KEY
  fi
  mkdir -p "$DOELMAP"
else
  # DE CONTEXT IS VERPLICHT EN WORDT NOOIT AFGELEID. Deze hele reeks begon met een
  # `task cluster:bootstrap` die op ODCN uitkwam omdat er kale kubectl-aanroepen in zes
  # taken stonden en `current-context` ergens anders heen wees. Een generator die
  # wachtwoorden toepast is het laatste dat je dat mag laten overkomen.
  : "${KUBE_CONTEXT:?KUBE_CONTEXT is verplicht bij BESTEMMING=cluster}"
fi

echo "Geheimen genereren voor cluster: $CLUSTER_NAME ($MODE), bestemming: $BESTEMMING"
echo "  blauwdrukken: $TEMPLATES_DIR"
if [ "$BESTEMMING" = "git" ]; then
  echo "  doelmap     : $DOELMAP"
else
  echo "  context     : $KUBE_CONTEXT"
  echo "  namespace   : $RIG_NAMESPACE"
fi
echo ""

# --- de grendel op het doelcluster -------------------------------------------

# Een tweede slot naast de verplichte context, en het vraagt het CLUSTER ZELF wie hij is in
# plaats van te vertrouwen op een naam in een bestand. De operations-manager draagt zijn
# CLUSTER_MANAGER in zijn ConfigMap; komt die niet overeen met CLUSTER_TYPE, dan wijst de
# context naar een ander cluster dan je denkt en stoppen we.
#
# Op een vers cluster bestaat die ConfigMap nog niet, en dan kan de grendel niets
# bevestigen. Dat is precies de toestand waarin je hem het hardst nodig hebt, dus
# doorgaan-met-een-waarschuwing is hier de verkeerde keuze: zet NIEUW_CLUSTER=ja en zeg
# daarmee expliciet dat je weet dat er nog niets staat.
bevestig_cluster() {
  local gevonden
  gevonden="$(kubectl --context "$KUBE_CONTEXT" -n "$RIG_NAMESPACE" get configmap operations-manager-config \
    -o jsonpath='{.data.\.env}' 2>/dev/null | sed -n 's/^CLUSTER_MANAGER=//p' | head -1 || true)"

  if [ -z "$gevonden" ]; then
    if [ "${NIEUW_CLUSTER:-}" = "ja" ]; then
      echo "LET OP: geen operations-manager-config in $RIG_NAMESPACE; NIEUW_CLUSTER=ja, dus doorgaan."
      echo ""
      return 0
    fi
    echo "FOUT: kan niet bevestigen op welk cluster context '$KUBE_CONTEXT' uitkomt." >&2
    echo "      Er is geen configmap operations-manager-config in namespace $RIG_NAMESPACE." >&2
    echo "      Is dit een vers cluster, zet dan NIEUW_CLUSTER=ja." >&2
    exit 1
  fi

  if [ "$gevonden" != "$CLUSTER_TYPE" ]; then
    echo "FOUT: context '$KUBE_CONTEXT' komt uit op cluster '$gevonden', niet op '$CLUSTER_TYPE'." >&2
    echo "      Er is niets gewijzigd." >&2
    exit 1
  fi

  echo "Cluster bevestigd: $gevonden"
  echo ""
}

[ "$BESTEMMING" = "cluster" ] && bevestig_cluster

# --- het overzicht -----------------------------------------------------------

# Het overzicht wordt in een tijdelijk bestand opgebouwd en pas aan het eind weggeschreven,
# en alleen als er echt iets in staat. Hier stond ooit een `>` op het echte bestand boven de
# lus: dat kapte het af, waarna elk geheim werd overgeslagen omdat het al bestond en er dus
# niets meer bijkwam. Een tweede run meldde dan "Skipping (already exists)" voor alles,
# eindigde met een vinkje, en had ondertussen alle wachtwoorden gewist.
OVERZICHT_BESTAND="secrets-overview-${MODE}-${CLUSTER_TYPE}.yaml"
OVERZICHT_TMP="$(mktemp)"
trap 'rm -f "$OVERZICHT_TMP"' EXIT

# --- hulpfuncties ------------------------------------------------------------

# Zet WACHTWOORD (wat in het geheim komt) en KLARE_TEKST (wat in het overzicht komt). Bij
# random zijn die gelijk; bij bcrypt is het geheim de hash en het overzicht het wachtwoord
# waar die hash bij hoort, want een hash kun je niet aan een mens geven.
maak_waarde() {
  local soort="$1" lengte="$2"
  case "$soort" in
    random)
      if [ -n "$FIXED_PASSWORD" ]; then
        KLARE_TEKST="$FIXED_PASSWORD"
      else
        KLARE_TEKST="$(openssl rand -base64 "$lengte" | tr -d "=+/" | cut -c1-"$lengte")"
      fi
      WACHTWOORD="$KLARE_TEKST"
      ;;
    bcrypt)
      if [ -n "$FIXED_PASSWORD" ]; then
        KLARE_TEKST="$FIXED_PASSWORD"
      else
        KLARE_TEKST="$(openssl rand -base64 16 | tr -d "=+/" | cut -c1-16)"
      fi
      # $2b -> $2y omdat niet elke lezer van deze hashes de nieuwere prefix accepteert.
      WACHTWOORD="$(htpasswd -nbBC 10 "" "$KLARE_TEKST" | tr -d ':\n' | sed 's/$2b/$2y/')"
      ;;
    *)
      echo "  FOUT: onbekend wachtwoordtype: $soort" >&2
      return 1
      ;;
  esac
}

# Echoot de annotatie van een veld in een blauwdruk, of niets als het veld er geen heeft.
annotatie_van() {
  local blauwdruk="$1" veld="$2"
  awk -v veld="$veld" '
    $0 ~ "^[[:space:]]*" veld "[[:space:]]*:" && /# @secret-gen:/ {
      sub(/.*# @secret-gen:/, "")
      print
      exit
    }
  ' "$blauwdruk"
}

# Splitst een annotatie in soort en lengte. Geeft 1 terug bij `skip`.
ontleed_annotatie() {
  local annotatie="$1"
  case "$annotatie" in
    *skip*) return 1 ;;
  esac
  local soort_lengte
  soort_lengte="$(printf '%s' "$annotatie" | cut -d',' -f1)"
  SOORT="$(printf '%s' "$soort_lengte" | cut -d':' -f1)"
  LENGTE="$(printf '%s' "$soort_lengte" | cut -d':' -f2)"
  return 0
}

# Zet WACHTWOORD en KLARE_TEKST voor een veld: gegenereerd als er een annotatie op staat,
# anders de letterlijke waarde uit de blauwdruk. Geeft 1 terug bij een skip-annotatie.
waarde_voor_veld() {
  local blauwdruk="$1" veld="$2" annotatie
  annotatie="$(annotatie_van "$blauwdruk" "$veld")"
  if [ -n "$annotatie" ]; then
    ontleed_annotatie "$annotatie" || return 1
    maak_waarde "$SOORT" "$LENGTE"
  else
    # Geen annotatie: dit is configuratie die in het geheim meereist, geen wachtwoord.
    WACHTWOORD="$(yq eval ".stringData.\"$veld\"" "$blauwdruk")"
    KLARE_TEKST=""
  fi
}

# Zet een veld in een AL VERSLEUTELD bestand, zonder de rest aan te raken. De waarde moet
# JSON zijn en gaat via stdin, zodat hij niet in de procestabel belandt.
zet_veld_versleuteld() {
  printf '%s' "$3" | jq -Rs . | sops set --value-stdin "$1" "[\"stringData\"][\"$2\"]"
}

velden_van() {
  yq eval '.stringData | keys | .[]' "$1" 2>/dev/null || true
}

# De velden van een geheim ZOALS HET OP HET CLUSTER STAAT. Kubernetes bewaart alles onder
# .data (base64), ook wat als stringData is aangeboden, dus daar wordt gekeken.
velden_op_cluster() {
  kubectl --context "$KUBE_CONTEXT" -n "$RIG_NAMESPACE" get secret "$1" -o json 2>/dev/null \
    | jq -r '.data // {} | keys[]' 2>/dev/null || true
}

bestaat_op_cluster() {
  kubectl --context "$KUBE_CONTEXT" -n "$RIG_NAMESPACE" get secret "$1" >/dev/null 2>&1
}

noteer_in_overzicht() {
  local bestandsnaam="$1" veld="$2" klare_tekst="$3" soort="$4"
  [ -n "$klare_tekst" ] || return 0
  if [ "$KOP_GESCHREVEN" -eq 0 ]; then
    echo "# === $bestandsnaam ===" >> "$OVERZICHT_TMP"
    KOP_GESCHREVEN=1
  fi
  if [ "$soort" = "bcrypt" ]; then
    echo "$veld: \"$klare_tekst\"  # Origineel wachtwoord (bcrypt-hash in het geheim)" >> "$OVERZICHT_TMP"
  else
    echo "$veld: \"$klare_tekst\"" >> "$OVERZICHT_TMP"
  fi
}

# --- de lus ------------------------------------------------------------------

GEGENEREERD=0
AANGEVULD=0

for blauwdruk in "$TEMPLATES_DIR"/*.yaml; do
  [ -f "$blauwdruk" ] || continue
  bestandsnaam="$(basename "$blauwdruk")"

  # Alleen bestanden die onmiskenbaar geheimen zijn.
  case "$bestandsnaam" in
    *-secret.yaml|*.secret.yaml) ;;
    *) echo "Overslaan (geen geheim): $bestandsnaam"; continue ;;
  esac

  secret_naam="$(yq eval '.metadata.name' "$blauwdruk")"
  KOP_GESCHREVEN=0

  # Bestaat het geheim al? Voor git is dat een bestand, voor cluster een Secret.
  if [ "$BESTEMMING" = "git" ]; then
    uitvoer="$DOELMAP/${bestandsnaam}.sops.yaml"
    [ -f "$uitvoer" ] && bestaat=ja || bestaat=nee
    bestaande_velden="$([ "$bestaat" = ja ] && velden_van "$uitvoer" || true)"
  else
    bestaat_op_cluster "$secret_naam" && bestaat=ja || bestaat=nee
    bestaande_velden="$([ "$bestaat" = ja ] && velden_op_cluster "$secret_naam" || true)"
  fi

  # ---------------------------------------------------------------- pad 2: aanvullen
  if [ "$bestaat" = ja ]; then
    ontbrekend=""
    while IFS= read -r veld; do
      [ -n "$veld" ] || continue
      printf '%s\n' "$bestaande_velden" | grep -qxF "$veld" || ontbrekend="$ontbrekend $veld"
    done <<EOF
$(velden_van "$blauwdruk")
EOF

    # Andersom melden we alleen. Een veld dat uit de blauwdruk is gehaald maar nog in het
    # geheim staat wordt niet verwijderd: er kan nog iets van lezen, en dat weet dit script
    # niet.
    while IFS= read -r veld; do
      [ -n "$veld" ] || continue
      velden_van "$blauwdruk" | grep -qxF "$veld" \
        || echo "  LET OP: $secret_naam heeft veld '$veld' dat niet (meer) in de blauwdruk staat; blijft staan"
    done <<EOF
$bestaande_velden
EOF

    if [ -z "$ontbrekend" ]; then
      echo "Ongewijzigd: $bestandsnaam"
      continue
    fi

    echo "Aanvullen: $bestandsnaam"
    patch_json="{}"
    for veld in $ontbrekend; do
      if ! waarde_voor_veld "$blauwdruk" "$veld"; then
        echo "  Overslaan (skip-annotatie): $veld"
        continue
      fi
      if [ "$BESTEMMING" = "git" ]; then
        zet_veld_versleuteld "$uitvoer" "$veld" "$WACHTWOORD"
      else
        patch_json="$(printf '%s' "$WACHTWOORD" | jq -Rs --argjson p "$patch_json" --arg k "$veld" '$p + {($k): .}')"
      fi
      echo "  Toegevoegd: $veld"
      AANGEVULD=$((AANGEVULD + 1))
      noteer_in_overzicht "$bestandsnaam" "$veld" "$KLARE_TEKST" "${SOORT:-random}"
    done

    if [ "$BESTEMMING" = "cluster" ] && [ "$patch_json" != "{}" ]; then
      # --type=merge en niet strategic: dit voegt sleutels toe onder stringData en raakt
      # niets anders aan. De apiserver zet stringData zelf om naar data.
      printf '%s' "$patch_json" | jq '{stringData: .}' \
        | kubectl --context "$KUBE_CONTEXT" -n "$RIG_NAMESPACE" patch secret "$secret_naam" \
            --type=merge --patch-file /dev/stdin >/dev/null
    fi

    [ "$KOP_GESCHREVEN" -eq 1 ] && echo "" >> "$OVERZICHT_TMP"
    continue
  fi

  # ------------------------------------------------------- pad 1: volledig aanmaken
  echo "Aanmaken: $bestandsnaam"
  tijdelijk="$(mktemp)"
  cp "$blauwdruk" "$tijdelijk"

  while IFS= read -r veld; do
    [ -n "$veld" ] || continue
    annotatie="$(annotatie_van "$blauwdruk" "$veld")"
    [ -n "$annotatie" ] || continue
    if ! ontleed_annotatie "$annotatie"; then
      echo "  Overslaan (skip-annotatie): $veld"
      continue
    fi
    maak_waarde "$SOORT" "$LENGTE"
    yq eval ".stringData.\"$veld\" = \"$WACHTWOORD\"" -i "$tijdelijk"
    echo "  $SOORT-wachtwoord voor veld: $veld"
    noteer_in_overzicht "$bestandsnaam" "$veld" "$KLARE_TEKST" "$SOORT"
  done <<EOF
$(velden_van "$blauwdruk")
EOF

  [ "$KOP_GESCHREVEN" -eq 1 ] && echo "" >> "$OVERZICHT_TMP"

  yq eval ".metadata.namespace = \"$RIG_NAMESPACE\"" -i "$tijdelijk"

  if [ "$BESTEMMING" = "git" ]; then
    cp "$tijdelijk" "$uitvoer"
    sops --encrypt --output-type yaml --age "$AGE_PUBLIEKE_SLEUTEL" --in-place "$uitvoer"
    echo "  Versleuteld: $uitvoer"
  else
    kubectl --context "$KUBE_CONTEXT" -n "$RIG_NAMESPACE" apply -f "$tijdelijk" >/dev/null
    echo "  Toegepast op het cluster: secret/$secret_naam"
  fi
  rm -f "$tijdelijk"
  GEGENEREERD=$((GEGENEREERD + 1))
done

# --- de twee begeleidende bestanden, alleen bij BESTEMMING=git ----------------

if [ "$BESTEMMING" = "git" ]; then
  DECRYPT_BESTAND="$DOELMAP/decrypt-sops.yaml"
  if [ ! -f "$DECRYPT_BESTAND" ]; then
    cat > "$DECRYPT_BESTAND" << 'EOF'
apiVersion: viaduct.ai/v1
kind: ksops
metadata:
  name: secret-generator
  annotations:
    config.kubernetes.io/function: "exec:\n  path: ksops\n"
files:
EOF
    echo "decrypt-sops.yaml aangemaakt"
  fi

  for sops_bestand in "$DOELMAP"/*.sops.yaml; do
    [ -f "$sops_bestand" ] || continue
    naam="$(basename "$sops_bestand")"
    if ! grep -qF "$naam" "$DECRYPT_BESTAND"; then
      echo "  - $naam" >> "$DECRYPT_BESTAND"
      echo "decrypt-sops.yaml: $naam toegevoegd"
    fi
  done

  KUSTOMIZATION_BESTAND="$DOELMAP/kustomization.yaml"
  if [ ! -f "$KUSTOMIZATION_BESTAND" ]; then
    cat > "$KUSTOMIZATION_BESTAND" << 'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

generators:
- decrypt-sops.yaml
EOF
    echo "kustomization.yaml aangemaakt"
  fi
fi

# --- het overzicht -----------------------------------------------------------

if [ -s "$OVERZICHT_TMP" ]; then
  {
    echo "# Overzicht van de $MODE-geheimen - $CLUSTER_TYPE (bestemming: $BESTEMMING)"
    echo "# Gegenereerd op $(date)"
    echo "# LET OP: dit bestand bevat wachtwoorden in platte tekst."
    echo "# Het staat in .gitignore. Neem de wachtwoorden over en verwijder het daarna zelf."
    echo ""
    cat "$OVERZICHT_TMP"
  } > "$OVERZICHT_BESTAND"
  OVERZICHT_MELDING="Overzicht: $OVERZICHT_BESTAND"
else
  OVERZICHT_MELDING="Niets nieuws, dus $OVERZICHT_BESTAND is ongemoeid gelaten."
fi

echo ""
echo "Klaar. $GEGENEREERD geheim(en) aangemaakt, $AANGEVULD veld(en) aangevuld."
echo "$OVERZICHT_MELDING"
if [ "$BESTEMMING" = "git" ]; then
  echo "Versleutelde geheimen: $DOELMAP/"
else
  echo "Toegepast in $RIG_NAMESPACE op $KUBE_CONTEXT. Niets reconcilieert deze geheimen."
fi
