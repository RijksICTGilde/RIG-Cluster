#!/bin/bash

set -e  # Exit on any error
set -o pipefail

SOPS_KEY_SECRET="${ARGOCD_ENV_SOPS_KEY_SECRET:-sops-age-key}"
KUSTOMIZE_FOLDERS="${ARGOCD_ENV_KUSTOMIZE_FOLDERS:-current}"

# Check if target namespace exists
if ! kubectl get namespace "${ARGOCD_APP_NAMESPACE}" >/dev/null 2>&1; then
  echo "ERROR: Namespace '${ARGOCD_APP_NAMESPACE}' does not exist" >&2
  exit 1
fi

# Check if SOPS secret exists
if ! kubectl get secret "${SOPS_KEY_SECRET}" -n "${ARGOCD_APP_NAMESPACE}" >/dev/null 2>&1; then
  echo "ERROR: SOPS secret '${SOPS_KEY_SECRET}' not found in namespace '${ARGOCD_APP_NAMESPACE}'" >&2
  exit 1
fi

# Extract SOPS age key
echo "Extracting SOPS age key from secret '${SOPS_KEY_SECRET}'" >&2
SOPS_KEY_B64=$(kubectl get secret ${SOPS_KEY_SECRET} -n ${ARGOCD_APP_NAMESPACE} -o jsonpath='{.data.key}')
if [ -z "$SOPS_KEY_B64" ]; then
  echo "ERROR: Could not extract key data from secret '${SOPS_KEY_SECRET}'. Check if the secret has a 'key' field." >&2
  exit 1
fi

SOPS_KEY_RAW=$(echo "$SOPS_KEY_B64" | base64 -d)
if [ $? -ne 0 ]; then
  echo "ERROR: Failed to base64 decode the key data from secret '${SOPS_KEY_SECRET}'" >&2
  exit 1
fi

SOPS_AGE_KEY=$(echo "$SOPS_KEY_RAW" | grep '^AGE-SECRET-KEY-')
if [ -z "$SOPS_AGE_KEY" ]; then
  echo "ERROR: No valid AGE-SECRET-KEY found in secret '${SOPS_KEY_SECRET}'. Key must start with 'AGE-SECRET-KEY-'" >&2
  exit 1
fi

# Function to process a single folder
process_folder() {
  local folder="$1"
  local output_file="$2"

  echo "Processing folder: $folder" >&2

  # Inject namespace into kustomization.yaml
  if [ -f "$folder/kustomization.yaml" ]; then
  yq eval ".namespace = \"${ARGOCD_APP_NAMESPACE}\"" -i "$folder/kustomization.yaml"
  elif [ -f "$folder/kustomization.yml" ]; then
  yq eval ".namespace = \"${ARGOCD_APP_NAMESPACE}\"" -i "$folder/kustomization.yml"
  fi

  # Generate manifests for this folder
  echo "---" >> "$output_file"
  SOPS_AGE_KEY="$SOPS_AGE_KEY" kustomize build --enable-alpha-plugins --enable-exec "$folder" >> "$output_file"
}

# Determine folders to process
FOLDERS=()
case "$KUSTOMIZE_FOLDERS" in
  "current")
    FOLDERS=(".")
    ;;
  "subfolders")
    # Find all subfolders with kustomization files (recursive)
    while IFS= read -r -d '' dir; do
      FOLDERS+=("$dir")
    done < <(find . -mindepth 2 -type f \( -name "kustomization.yaml" -o -name "kustomization.yml" \) -exec dirname {} \; | sort -u | tr '\n' '\0')
    ;;
  "all")
    # Current folder first
    FOLDERS=(".")
    # Then subfolders
    for dir in */; do
      if [ -d "$dir" ] && ([ -f "$dir/kustomization.yaml" ] || [ -f "$dir/kustomization.yml" ]); then
        FOLDERS+=("${dir%/}")
      fi
    done
    ;;
  *)
    echo "ERROR: Invalid KUSTOMIZE_FOLDERS value: $KUSTOMIZE_FOLDERS. Use 'current', 'subfolders', or 'all'" >&2
    exit 1
    ;;
esac

# Create unique temporary file
TEMP_OUTPUT=$(mktemp /tmp/kustomize_output.XXXXXX)
trap "rm -f $TEMP_OUTPUT" EXIT

# Process all folders
> "$TEMP_OUTPUT"  # Clear output file
PROCESSED_FOLDERS=""

for folder in "${FOLDERS[@]}"; do
  echo "DEBUG: Checking folder: '$folder'" >&2
  if [ -f "$folder/kustomization.yaml" ] || [ -f "$folder/kustomization.yml" ]; then
    echo "DEBUG: Found kustomization file in folder: '$folder'" >&2
    echo "DEBUG: Calling process_folder with folder: '$folder'" >&2
    process_folder "$folder" "$TEMP_OUTPUT"
    PROCESSED_FOLDERS="$PROCESSED_FOLDERS $folder"
  else
    echo "WARNING: No kustomization file found in folder: $folder" >&2
  fi
done

# If no folders were processed, set to current directory to indicate we checked
if [ -z "$PROCESSED_FOLDERS" ]; then
  PROCESSED_FOLDERS="."
fi

# Output the kustomize manifests
cat "$TEMP_OUTPUT"
