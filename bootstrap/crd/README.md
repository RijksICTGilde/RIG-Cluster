# ArgoCD Operator Customization

This directory contains documentation on the customizations required for the ArgoCD operator.

## Customizations

The ArgoCD operator v0.14.0 requires the following modifications to work properly in our environment:

1. **Disable Webhooks**
   - In `config/default/kustomization.yaml`: Comment out the line `- ../webhook`
   - In `config/default/kustomization.yaml`: Comment out the line `- path: manager_webhook_patch.yaml`

2. **Use Latest Image**
   - In `config/manager/kustomization.yaml`: Change `newTag: v0.14.0` to `newTag: latest`

3. ** Using a fake prometheus **
   - Currently we do not have prometheus in the cluster, so we create a fake definition for it for Argo to work

## Installation Process

To install the customized ArgoCD operator:

1. Download the ArgoCD operator source code:
   ```bash
   # Download source code
   task prepare-argocd-operator
   ```

2. Apply the operator installation:
   ```bash
   kubectl apply -f bootstrap/kustomize/operator/argocd-operator-install.yaml
   ```

3. After the operator is running, apply the ArgoCD instance:
   ```bash
   kustomize build bootstrap/kustomize/overlays/local-filesystem | kubectl apply -f -
   ```

## Reference

For more details on the ArgoCD operator, see:
- [ArgoCD Operator GitHub Repository](https://github.com/argoproj-labs/argocd-operator)
- [ArgoCD Operator Documentation](https://argocd-operator.readthedocs.io/en/latest/)