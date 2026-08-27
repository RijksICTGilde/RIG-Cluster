#!/usr/bin/env bash
#
# Genereert de geheimen van een cluster uit de blauwdrukken in TEMPLATES_DIR.
#
# Dit stond als 257 regels shell in de Taskfile onder `_generate-secrets-shared`. Het staat
# hier omdat het geen taak is maar een programma: het heeft lussen, vertakkingen en
# toestand, en dat laatste is er net bijgekomen (zie "aanvullen" hieronder). Een taak die
# een binary aanroept hoort in de Taskfile; code hoort in een bestand dat je kunt lezen,
# los kunt draaien en kunt testen.
#
# Alle invoer komt uit omgevingsvariabelen en niet uit vlaggen. Dat is geen smaak: een
# fundament-plugin krijgt zijn configuratie als FUNP_-omgevingsvariabelen op zijn
# Deployment, dus een script dat zo gevoed wordt past daar zonder tussenlaag in.
#
# TWEE PADEN, en het verschil ertussen is de hele bedoeling:
#
#   1. Het uitvoerbestand bestaat NIET  -> hele bestand genereren.
#   2. Het uitvoerbestand bestaat WEL   -> alleen ONTBREKENDE velden aanvullen.
#
# Pad 2 bestond niet. De oude versie sloeg een bestaand bestand in zijn geheel over, met
# "Skipping (already exists)" en een vinkje. Dat beschermt tegen ongevraagde rotatie, en
# dat blijft zo, maar het betekende ook dat een veld dat later aan een blauwdruk werd
# toegevoegd op een draaiend cluster NOOIT meer landde. Dat is geen theorie: op odcn mist
# `keycloak-admin-secret` vier velden die de blauwdruk wel heeft
# (KEYCLOAK_ADMIN_CLIENT_SECRET en de drie KEYCLOAK_OTP_ADMIN_*), en OPI leest er drie van
# met `optional: true`, dus ze ontbreken daar stil.
#
# Wat dit script NOOIT doet: een bestaande waarde overschrijven of een veld verwijderen.
# Rotatie is een operationele handeling met een uitrol eromheen, geen bijverschijnsel van
# een generator die toevallig langsloopt. Een veld dat in het geheim staat maar niet meer
# in de blauwdruk wordt gemeld en blijft staan.

set -euo pipefail

# --- invoer ------------------------------------------------------------------

: "${MODE:?MODE is verplicht (infrastructure of bootstrap)}"
: "${TEMPLATES_DIR:?TEMPLATES_DIR is verplicht}"
: "${OUTPUT_DIR:?OUTPUT_DIR is verplicht}"
: "${CLUSTER_TYPE:?CLUSTER_TYPE is verplicht}"
: "${CLUSTER_FOLDER:?CLUSTER_FOLDER is verplicht}"
: "${KEY_FILE:?KEY_FILE is verplicht}"
: "${RIG_NAMESPACE:?RIG_NAMESPACE is verplicht}"
FIXED_PASSWORD="${FIXED_PASSWORD:-}"
CLUSTER_NAME="${CLUSTER_NAME:-$CLUSTER_TYPE}"

if [ ! -d "$TEMPLATES_DIR" ]; then
  echo "FOUT: TEMPLATES_DIR bestaat niet: $TEMPLATES_DIR" >&2
  exit 1
fi
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
# nodig; versleutelen van een nieuw bestand heeft genoeg aan de publieke. Alleen exporteren
# als hij er is, zodat een omgeving die de sleutel via een andere weg aanlevert (een
# keyservice, een gemounte sleutel) niet overschreven wordt.
if [ -z "${SOPS_AGE_KEY:-}" ]; then
  SOPS_AGE_KEY="$(sed -n '3p' "$KEY_FILE")"
  export SOPS_AGE_KEY
fi

mkdir -p "$DOELMAP"

echo "Geheimen genereren voor cluster: $CLUSTER_NAME ($MODE)"
echo "  blauwdrukken: $TEMPLATES_DIR"
echo "  doelmap     : $DOELMAP"
echo ""

# Het overzicht wordt in een tijdelijk bestand opgebouwd en pas aan het eind weggeschreven,
# en alleen als er echt iets in staat. Hier stond ooit een `>` op het echte bestand boven de
# lus: dat kapte het af, waarna elk geheim werd overgeslagen omdat het al bestond en er dus
# niets meer bijkwam. Een tweede run meldde dan "Skipping (already exists)" voor alles,
# eindigde met een vinkje, en had ondertussen alle wachtwoorden gewist. Onherstelbaar, want
# in git staan alleen bcrypt-hashes en AGE-blokken.
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

# Zet een veld in een AL VERSLEUTELD bestand, zonder de rest aan te raken. De waarde moet
# JSON zijn en gaat via stdin, zodat hij niet in de procestabel belandt.
zet_veld_versleuteld() {
  local bestand="$1" veld="$2" waarde="$3"
  printf '%s' "$waarde" | jq -Rs . | sops set --value-stdin "$bestand" "[\"stringData\"][\"$veld\"]"
}

velden_van() {
  yq eval '.stringData | keys | .[]' "$1" 2>/dev/null || true
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

  uitvoer="$DOELMAP/${bestandsnaam}.sops.yaml"

  # ---------------------------------------------------------------- pad 2: aanvullen
  if [ -f "$uitvoer" ]; then
    ontbrekend=""
    bestaande_velden="$(velden_van "$uitvoer")"
    while IFS= read -r veld; do
      [ -n "$veld" ] || continue
      if ! printf '%s\n' "$bestaande_velden" | grep -qxF "$veld"; then
        ontbrekend="$ontbrekend $veld"
      fi
    done <<EOF
$(velden_van "$blauwdruk")
EOF

    # Andersom melden we alleen. Een veld dat uit de blauwdruk is gehaald maar nog in het
    # geheim staat wordt niet verwijderd: er kan nog iets van lezen, en dat weet dit script
    # niet.
    while IFS= read -r veld; do
      [ -n "$veld" ] || continue
      if ! velden_van "$blauwdruk" | grep -qxF "$veld"; then
        echo "  LET OP: $bestandsnaam heeft veld '$veld' dat niet (meer) in de blauwdruk staat; blijft staan"
      fi
    done <<EOF
$bestaande_velden
EOF

    if [ -z "$ontbrekend" ]; then
      echo "Ongewijzigd: $bestandsnaam"
      continue
    fi

    echo "Aanvullen: $bestandsnaam"
    kop_geschreven=0
    for veld in $ontbrekend; do
      annotatie="$(annotatie_van "$blauwdruk" "$veld")"
      if [ -n "$annotatie" ]; then
        if ! ontleed_annotatie "$annotatie"; then
          echo "  Overslaan (skip-annotatie): $veld"
          continue
        fi
        maak_waarde "$SOORT" "$LENGTE"
      else
        # Geen annotatie: dit is configuratie die in het geheim meereist, geen wachtwoord.
        # De waarde uit de blauwdruk is dan de bedoelde waarde.
        WACHTWOORD="$(yq eval ".stringData.\"$veld\"" "$blauwdruk")"
        KLARE_TEKST=""
      fi

      zet_veld_versleuteld "$uitvoer" "$veld" "$WACHTWOORD"
      echo "  Toegevoegd: $veld"
      AANGEVULD=$((AANGEVULD + 1))

      if [ -n "$KLARE_TEKST" ]; then
        if [ "$kop_geschreven" -eq 0 ]; then
          echo "# === $bestandsnaam (aangevuld) ===" >> "$OVERZICHT_TMP"
          kop_geschreven=1
        fi
        echo "$veld: \"$KLARE_TEKST\"" >> "$OVERZICHT_TMP"
      fi
    done
    [ "$kop_geschreven" -eq 1 ] && echo "" >> "$OVERZICHT_TMP"
    continue
  fi

  # ------------------------------------------------------- pad 1: hele bestand maken
  echo "Genereren: $bestandsnaam"
  tijdelijk="$(mktemp)"
  cp "$blauwdruk" "$tijdelijk"
  echo "# === $bestandsnaam ===" >> "$OVERZICHT_TMP"

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
    if [ "$SOORT" = "bcrypt" ]; then
      echo "$veld: \"$KLARE_TEKST\"  # Origineel wachtwoord (bcrypt-hash in het geheim)" >> "$OVERZICHT_TMP"
    else
      echo "$veld: \"$WACHTWOORD\"" >> "$OVERZICHT_TMP"
    fi
  done <<EOF
$(velden_van "$blauwdruk")
EOF

  echo "" >> "$OVERZICHT_TMP"

  yq eval ".metadata.namespace = \"$RIG_NAMESPACE\"" -i "$tijdelijk"
  cp "$tijdelijk" "$uitvoer"
  rm -f "$tijdelijk"
  sops --encrypt --output-type yaml --age "$AGE_PUBLIEKE_SLEUTEL" --in-place "$uitvoer"
  echo "  Versleuteld: $uitvoer"
  GEGENEREERD=$((GEGENEREERD + 1))
done

# --- de twee begeleidende bestanden -----------------------------------------

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

# --- het overzicht -----------------------------------------------------------

if [ -s "$OVERZICHT_TMP" ]; then
  {
    echo "# Overzicht van de $MODE-geheimen - $CLUSTER_TYPE"
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
echo "Klaar. $GEGENEREERD bestand(en) gegenereerd, $AANGEVULD veld(en) aangevuld."
echo "$OVERZICHT_MELDING"
echo "Versleutelde geheimen: $DOELMAP/"
