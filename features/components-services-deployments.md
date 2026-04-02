# How ZAD Projects Work

## Four Concepts

| Term | What it is | In the YAML |
|------|-----------|-------------|
| **Project** | Your team's boundary — groups everything together | top-level |
| **Service** | A platform-managed feature: database, SSO, web access, storage | `services[]` |
| **Component** | A slot that describes a deployable part of your app — port, path, resources. No image. | `components[]` |
| **Deployment** | A named instance that runs your components with specific images. Could be "production", "pr-844", or "janes-dev". | `deployments[]` |

A **binding** is when a component says `uses-services: [publish-on-web]` — it connects a component to a service.

---

## The Big Picture

```mermaid
graph TD
    subgraph "Project: my-app"

        subgraph "Services — managed by the platform"
            S1["publish-on-web"]
            S2["keycloak"]
            S3["postgresql-database"]
        end

        subgraph "Components — the slots you define"
            Frontend["frontend\nport 8080 · path /"]
            API["api\nport 3000 · path /api"]
        end

        Frontend -. "uses" .-> S1
        Frontend -. "uses" .-> S2
        API -. "uses" .-> S1
        API -. "uses" .-> S3

        subgraph "Deployment: production"
            D1["frontend\nmyteam/frontend:2.1.0"]
            D2["api\nmyteam/api:1.4.3"]
        end

        subgraph "Deployment: staging"
            D3["frontend\nmyteam/frontend:2.2.0-rc1"]
            D4["api\nmyteam/api:1.5.0-beta"]
        end

        Frontend --> D1
        Frontend --> D3
        API --> D2
        API --> D4
    end
```

---

## Step by Step

### 1. Create a Project

A project is your team's space. It holds everything: components, services,
deployments, and team members.

```yaml
name: my-app
description: Our awesome app
clusters:
  - odcn-production
users:
  - email: jane@rijksoverheid.nl
    role: admin
```

---

### 2. Pick Services

Services are platform features that ZAD provisions and manages for you.
You just say *"I need this"* — the platform does the rest.

```yaml
services:
  - publish-on-web              # makes your app reachable on the internet
  - keycloak                    # gives you SSO authentication
  - postgresql-database         # gives you a managed database
```

Available services:

```mermaid
graph LR
    subgraph "Platform Services"
        A["publish-on-web\nIngress + TLS"]
        B["keycloak\nSSO realm + client"]
        C["postgresql-database\nManaged PostgreSQL"]
        D["persistent-storage\nPersistent volumes"]
    end
```

---

### 3. Define Components

A component is a **slot** — it describes the shape of a deployable part of your app.

It answers:
- What **port** does it listen on?
- What URL **path** does it serve?
- How much **CPU and memory** does it need?
- Which **services** does it use? (bindings)

```yaml
components:
  - name: frontend
    type: single
    ports:
      inbound: [8080]
    path: /
    resources:
      cpu: 100m
      memory: 256Mi
    uses-services:              # bindings
      - publish-on-web
      - keycloak

  - name: api
    type: single
    ports:
      inbound: [3000]
    path: /api
    resources:
      cpu: 200m
      memory: 512Mi
    uses-services:
      - publish-on-web
      - postgresql-database
```

**There is no Docker image here.** A component describes what the slot looks like,
not what fills it. That happens next.

```mermaid
graph LR
    subgraph "Component: frontend"
        F["port 8080 · path / · 256Mi"]
    end
    subgraph "Component: api"
        A["port 3000 · path /api · 512Mi"]
    end

    F -. "uses" .-> PW["publish-on-web"]
    F -. "uses" .-> KC["keycloak"]
    A -. "uses" .-> PW
    A -. "uses" .-> PG["postgresql-database"]
```

---

### 4. Create Deployments

A deployment fills the component slots with actual Docker images and puts them
on a cluster with a domain name.

You can have as many deployments as you want — "production", "staging", "pr-844",
"demo-for-client", "janes-dev" — whatever you need.

```yaml
deployments:
  - name: production
    cluster: odcn-production
    subdomain: myapp
    base-domain: rijksapps.nl
    components:
      - reference: frontend                   # ← fills the frontend slot
        image: ghcr.io/myteam/frontend:2.1.0  # ← with this image

      - reference: api                        # ← fills the api slot
        image: ghcr.io/myteam/api:1.4.3       # ← with this image

  - name: staging
    cluster: odcn-production
    subdomain: myapp-staging
    base-domain: rijksapps.nl
    components:
      - reference: frontend
        image: ghcr.io/myteam/frontend:2.2.0-rc1   # ← different version

      - reference: api
        image: ghcr.io/myteam/api:1.5.0-beta
```

---

## Why the Image Lives on the Deployment

The same component runs **different versions** in different deployments.
The slot stays the same — the image that fills it changes:

```mermaid
graph TD
    Comp["Component: frontend\nport 8080 · path /\n— the slot —"]

    Comp --> Prod["production\nfrontend:2.1.0\nstable release"]
    Comp --> Stag["staging\nfrontend:2.2.0-rc1\nrelease candidate"]
    Comp --> PR["pr-42\nfrontend:pr-42-abc\nwork in progress"]

    style Comp fill:#e0e7ff,stroke:#4f46e5,color:#1e1b4b
    style Prod fill:#d1fae5,stroke:#059669,color:#064e3b
    style Stag fill:#fef3c7,stroke:#d97706,color:#78350f
    style PR fill:#fce7f3,stroke:#db2777,color:#831843
```

---

## Real Example: algoritmes

Two components, two deployments. Production runs both components.
The PR preview runs only one, with a different image:

```mermaid
graph TD
    subgraph "Project: algoritmes"
        subgraph "Components"
            C1["component-1\nport 8080 · /beslishulp-ai-verordening"]
            C2["component-2\nport 8080 · /kader"]
        end

        subgraph "Deployment: productie"
            D1["ai-verordening-beslishulp:1.2.23"]
            D2["algoritmekader/preview:pr-844-20d1b89"]
        end

        subgraph "Deployment: pr844"
            D3["algoritmekader/preview:pr-844-df9ad50"]
        end

        C1 --> D1
        C2 --> D2
        C2 --> D3
    end
```

- **pr844** only runs component-2 — you don't have to deploy everything
- **component-2** runs a different image version in each deployment
- The component slots don't change — only what fills them

---

## Quick Reference

| Question                                    | Answer                                                          |
|---------------------------------------------|-----------------------------------------------------------------|
| Where do I define ports, paths, resources?  | In the **component**                                            |
| Where do I pick a database or SSO?          | In **services**, then bind them to a component via uses-services |
| Where do I set the Docker image?            | In the **deployment**, under `components → image`               |
| Can I have multiple deployments?            | Yes — production, staging, PR preview, anything                 |
| Can a deployment skip a component?          | Yes — only include the ones you need                            |
| Can two deployments use different images?   | Yes — that's the whole point                                    |
