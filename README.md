# RIG-Cluster

Het RIG cluster is een Kubernetes platform in het ODC-Noord.

Het is bedoeld voor RIG projecten die in POC of Pilot fase zitten, maar biedt ook mogelijkheden voor productie.

Het uitgangspunt is dat je snel en flexibel een plek kunt opzetten voor jouw project.

Dit gaan we doen via een 'self service portal', waarbij je een project kunt aanmaken en kunt aangeven welke services je nodig hebt,
welke namespaces en wie rechten nodig heeft op dat project.

Services willen we centraal aanbieden. Mogelijke service zijn vooralsnog een Minio storage, PostgresSQL database, koppeling met de Keycloak. Dit kan aangevuld worden.

## Getting Started

To bootstrap a new cluster:

1. Clone this repository
2. Install the required tools:
   - kubectl
   - kustomize
   - task (from taskfile.dev)

3. Bootstrap the minimal setup (creates namespace and deploys ArgoCD):
   ```bash
   task bootstrap-minimal
   ```

4. For local development with Kind:
   ```bash
   # Create a Kind cluster
   task create-k8s-cluster
   
   # Apply bootstrap
   task bootstrap-minimal SOURCE_TYPE=local-filesystem
   ```

5. For production with GitHub repository:
   ```bash
   task bootstrap-minimal SOURCE_TYPE=github
   ```

6. Access ArgoCD UI:
   ```bash
   kubectl port-forward svc/argocd-server -n rig-system 8080:80
   ```
   Then open http://localhost:8080 in your browser (username: admin, password: admin)

Het beoogde voordeel van centraal aanbieden is dat we de inrichting maar eenmalig hoeven te doen, inclusief backup mogelijkheden etc. Daarnaast is de verwachting dat dit ook resources scheelt. Bovendien kunnen we 'configuration as code' toepassen, waarbij alle
benodigde informatie in een VCS is vastgelegd. Dit maakt migratie of (disaster)-recovery mogelijk.

Vooralsnog beginnen we met de volgende services:
- Flux
- Vault
- Keycloak
- PostgresSQL
- PGAdmin
- Bitnami Sealed Secrets (of SOPS)
- Minio
- Prometheus
- Grafana

Eventueel op aanvraag of later of wanneer nodig:
- Redis
- RabbitMQ
- Kafka
- Harbor

Node setup:

Indien we een leeg cluster hebben en zelf alles moeten inrichten, moet we ook kijken naar logscraping etc.
* Fluent Bit/Fluentd-based of Beats
* Loki with Promtail