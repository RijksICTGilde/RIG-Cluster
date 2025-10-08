# SOPS Setup Guide

This guide provides minimal examples for setting up SOPS with AGE encryption for use with Kubernetes.

## 1. Create SOPS AGE Keypair

Generate a new AGE keypair for SOPS encryption:

```bash
age-keygen -o sops-key.txt
```

This creates a file `sops-key.txt` containing:
- Private key (starts with `AGE-SECRET-KEY-`)
- Public key (in comments, starts with `age1`)

## 2. Create Kubernetes Secret YAML File

Create a YAML file containing the Kubernetes secret with the private key:

```bash
kubectl create secret generic sops-age-key \
  --from-file=key=sops-key.txt \
  -n production \
  --dry-run=client \
  -o yaml > sops-secret.yaml
```

This creates a `sops-secret.yaml` file that you can apply later with:

```bash
kubectl apply -f sops-secret.yaml
```

Replace `production` with your target namespace.

## 3. Encode a Secret with the Keypair

### Option A: Encrypt from stdin

```bash
# Get the public key from the generated file
PUBLIC_KEY=$(grep "public key:" sops-key.txt | cut -d' ' -f4)

# Create and encrypt a secret file
echo "password: mysecretpassword123" | \
sops --encrypt \
     --age $PUBLIC_KEY \
     --output-type yaml \
     /dev/stdin > secret.sops.yaml
```

### Option B: Encrypt existing file

```bash
# If you have an existing secret.yaml file
sops --encrypt --age $PUBLIC_KEY secret.yaml > secret.sops.yaml
```

## Verification

Decrypt the file to verify it works correctly:

```bash
SOPS_AGE_KEY_FILE=sops-key.txt sops --decrypt secret.sops.yaml
```

## Security Notes

- Keep the private key (`sops-key.txt`) secure and never commit it to Git
- The public key can be safely shared and stored in documentation
- Use different keypairs for different environments (production, staging, etc.)
- Store private keys in Kubernetes secrets for automated decryption workflows

When using plugin in kustomize, testing with:

SOPS_AGE_KEY="AGE-SECRET-KEY-here" kustomize build --enable-alpha-plugins --enable-exec apps/amt/overlays/local

