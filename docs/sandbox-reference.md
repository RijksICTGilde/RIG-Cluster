=========================================
 ZAD Sandbox - Reference Card
=========================================

SERVICE URLS AND CREDENTIALS

  Service              URL                                              Username     Password
  -------------------  -----------------------------------------------  -----------  -----------
  ArgoCD               https://argo.sandbox.rijksapp.dev                admin        admin1234
  Forgejo              https://forgejo.sandbox.rijksapp.dev             rig-admin    admin1234
  Keycloak             https://keycloak.sandbox.rijksapp.dev            admin        admin1234
  MinIO                https://minio.sandbox.rijksapp.dev               admin        admin1234
  Operations Manager   https://operations-manager.sandbox.rijksapp.dev  (via Keycloak SSO)

COMMON TASK COMMANDS

  Sync infrastructure changes to Forgejo:
    task sandbox:sync

  Rebuild and deploy Operations Manager:
    task sandbox:update-operations-manager

  Start hot-reload development with Skaffold:
    task sandbox:skaffold-dev

  Destroy the cluster:
    task sandbox:destroy

USEFUL KUBECTL COMMANDS

  View all pods:
    kubectl get pods -n rig-system

  Check ArgoCD applications:
    kubectl get applications -n rig-system

  View Operations Manager logs:
    kubectl logs -n rig-system -l app=operations-manager -f

  View Forgejo logs:
    kubectl logs -n rig-system -l app=forgejo -f

HOW TO UNINSTALL

  Run:
    task sandbox:destroy

  This removes:
  - The Kind cluster (rig-sandbox)
  - Generated SOPS-encrypted secrets
  - The sandbox AGE key (security/sandbox-key.txt)
  - Local secrets file (.env.sandboxed-local.secrets)

  It does NOT remove:
  - Installed CLI tools (kind, kubectl, etc.)
  - Docker images
  - The TLS certificate files

=========================================
