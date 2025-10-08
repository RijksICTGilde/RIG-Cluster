# Operations Manager Architecture

## Overview

The Operations Manager (OPI) consists of a **frontend** web interface and a **backend** FastAPI service deployed in each cluster. The backend processes project files to create complete "deployments" - combinations of Kubernetes manifests and required services like databases, storage, and authentication.

## System Architecture

```mermaid
graph TB
    subgraph "Central Management"
        UIServer[User Interface / API Server<br/>Manages Project Files]
        ProjectFiles[Project Files<br/>YAML Configurations]
    end
    
    subgraph "Cluster A"
        BackendA[Backend<br/>Reads project files]
        ManifestsA[Generated Manifests<br/>& Services]
    end
    
    subgraph "Cluster B"
        BackendB[Backend<br/>Reads project files]
        ManifestsB[Generated Manifests<br/>& Services]
    end
    
    UIServer --> ProjectFiles
    UIServer <--> BackendA
    UIServer <--> BackendB
    
    BackendA --> ProjectFiles
    BackendB --> ProjectFiles
    
    BackendA --> ManifestsA
    BackendB --> ManifestsB
```

## Backend Processing Detail

```mermaid
graph TB
    subgraph Input ["Input"]
        ProjectFile[Project File<br/>YAML Configuration]
    end
    
    subgraph BackendProc ["Backend Processing"]
        Backend[Backend Processor<br/>Reads & Processes]
    end
    
    subgraph ServiceProv ["Service Provisioning"]
        DB[Database<br/>Schema & Connections]
        Auth[Keycloak<br/>Realm & Client Setup]
        Storage[MinIO<br/>Bucket & Access Setup]
        More[...]
    end
    
    subgraph ServiceInfo ["Service Information"]
        Credentials[Connection Info<br/>URLs, Credentials, Configs]
    end
    
    subgraph GeneratedMan ["Generated Manifests"]
        Deployment[Deployment<br/>+ Service Connection Info]
        Service[Service<br/>Network Configuration]
        Ingress[Ingress<br/>External Access]
    end
    
    ProjectFile --> Backend
    Backend -->|"Configure services"| ServiceProv
    Backend -->|"Generate manifests"| GeneratedMan
    Backend -->|"Collect service info"| ServiceInfo
    Credentials -->|"Injects connection<br/>information"| GeneratedMan
```

## Key Concepts

**Frontend**: Web interface where project teams configure their deployments through forms

**Backend**: FastAPI application deployed in each cluster that wants to be managed

**Project File**: YAML configuration defining what the project needs (app specs, database, storage, etc.)

**Deployment**: Complete environment created from a project file, including:
- Kubernetes manifests (deployments, services, ingress)
- Required services (PostgreSQL database, MinIO storage, Keycloak authentication)
- Networking and security policies

**Processing Flow**: 
1. Project File → Backend Processing 
2. Backend sets up required services (DB, Auth, Storage)
3. Service connection information is automatically injected into Kubernetes manifests
4. Generated manifests deployed to cluster with all necessary service connectivity