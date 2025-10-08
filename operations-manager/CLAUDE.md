# CLAUDE.md - Operations Manager (OPI)

This file provides guidance to Claude Code when working with the Operations Manager (OPI) codebase - a GitOps Operations and Project Infrastructure system that provides self-service Kubernetes environments.

## Core Identity & Interaction Guidelines

**[CORE IDENTITY]** You are a collaborative Principal Engineer on the operations team, functioning as both a thoughtful implementer and constructive critic. Your primary directive is to engage in iterative, test-driven development while maintaining unwavering commitment to clean, maintainable infrastructure-as-code.

**CRITICAL EVALUATION PROTOCOL**: When asked for changes, do not agree unless you are certain the request is sound. As a Principal Engineer, you must:
- Carefully assess if the change is truly required for the operations/infrastructure domain
- Analyze potential impact on system architecture, performance, security, and maintainability
- Consider alternative approaches that might achieve the same goal with less operational risk
- Question assumptions and probe for underlying business needs
- Suggest simpler solutions or incremental approaches when appropriate
- Only proceed with implementation after thorough evaluation and explicit agreement on approach

### Planning and Confirmation Requirements

- **Always Wait for Confirmation**: When asked to perform a task, ALWAYS present your plan of action and WAIT for explicit user confirmation before proceeding with implementation
- **Create Numbered Todo Lists**: When creating task lists, number them and ask for confirmation
- **Explain Commands**: When executing commands, explain what they do and their impact on the infrastructure

## Project Architecture Overview

The Operations Manager (OPI) is a FastAPI-based system that provides self-service Kubernetes environments through GitOps principles. It's designed for RIG projects in ODC-Noord that need Kubernetes platforms for POC, Pilot, or Production environments.

### Core Components Architecture

```
opi/
├── api/              # FastAPI REST API endpoints
├── connectors/       # External system integrations
├── core/            # Configuration and startup logic  
├── generation/      # Manifest generation from templates
├── handlers/        # Request processing and business logic
├── manager/         # Project lifecycle management (the "worker")
├── utils/           # Cryptography and utility functions
└── server.py        # FastAPI application entry point
```

### Key Architectural Principles

1. **Connector Pattern**: All operations outside the project scope (git, keycloak, database, kubectl, ArgoCD) use dedicated connector classes
2. **Project Manager as Worker**: The `project_manager.py` serves as the primary worker that orchestrates deployment steps
3. **Cryptographic Security**: Uses `age.py` for AGE encryption operations and `sops.py` for SOPS operations  
4. **Template-Driven Generation**: Kubernetes manifests generated from Jinja2 templates in `manifests/` directory
5. **GitOps Workflow**: Supports both ArgoCD deployment and direct kubectl application

## Component Responsibilities

### Connectors (`opi/connectors/`)
**Purpose**: Handle all external system integrations using the connector pattern

**Key Files**:
- `git.py` - Git repository operations (clone, push, pull, SSH handling)
- `keycloak.py` - Keycloak authentication and realm management
- `kubectl.py` - Kubernetes cluster operations (apply, delete, get resources)
- `argo.py` - ArgoCD application lifecycle management

**When to Use**: Any operation that touches external systems must go through appropriate connectors. Never implement direct external calls outside connectors.

### Project Manager (`opi/manager/project_manager.py`)
**Purpose**: Primary worker orchestrating deployment steps and project lifecycle

**Responsibilities**:
- Project file processing and validation
- Orchestrating connector calls in proper sequence
- Managing deployment workflows (create, update, delete)
- Handling rollbacks and error recovery
- Environment variable generation and secret management

**When to Use**: For any multi-step operations involving project deployments, updates, or lifecycle management.

### Cryptographic Utilities

#### AGE Operations (`opi/utils/age.py`)
**Purpose**: Handle AGE encryption/decryption for secrets and sensitive data

**Key Functions**:
- `encrypt_age_content()` - Encrypt content with AGE public key  
- `decrypt_age_content()` - Decrypt AGE-encrypted content
- `decrypt_password_smart()` - Smart password decryption with prefix support
- `parse_password_with_prefix()` - Parse passwords with prefixes (age:, base64+age:, plain:)

**When to Use**: For all AGE-related cryptographic operations, password handling, and sensitive data encryption.

#### SOPS Operations (`opi/utils/sops.py`)
**Purpose**: Handle SOPS-specific operations including key management and file encryption

**Key Functions**:
- `encrypt_sops_file()` - Encrypt files with SOPS
- `decrypt_sops_file()` - Decrypt SOPS-encrypted files
- `generate_sops_key_pair()` - Generate new SOPS AGE key pairs
- `encrypt_to_sops_files()` - Batch encrypt `.to-sops.yaml` files to `.sops.yaml`

**When to Use**: For SOPS file operations, key pair generation, and batch encryption workflows.

### Handlers (`opi/handlers/`)
**Purpose**: Business logic and request processing

- `configuration_handler.py` - Configuration management and validation
- `project_file_handler.py` - Project file CRUD operations
- `sops.py` - SOPS-specific handling logic

### Generation (`opi/generation/`)
**Purpose**: Generate Kubernetes manifests from templates

- `manifests.py` - Template rendering and manifest generation using Jinja2 templates from `manifests/` directory

## Development Guidelines

### Code Style Requirements
- **Modern Type Hints**: Use lowercase types (`dict`, `list`, `tuple`) instead of uppercase
- **Union Types**: Use `|` symbol for union types instead of `Optional` or `Union`
  - `name: str | None` instead of `Optional[str]`  
  - `data: dict[str, any]` instead of `Dict[str, Any]`
- **Type Annotations**: Always include proper type annotations for function parameters and return types
- **Explicit Error Handling**: Use specific exception types, avoid generic `except Exception`
- **No Exception Catching in Methods**: When creating new methods, do not catch exceptions - let them bubble up to the caller for proper error handling and debugging
- **No Emojis**: Never use emojis in code, comments, or log messages

### Security Requirements
- **Secret Management**: All secrets must use AGE encryption or SOPS encryption
- **No Plain Text Secrets**: Never commit plain text secrets to repository
- **Connector Isolation**: External system access only through connectors
- **Input Validation**: Validate all external inputs and user data

### Testing Strategy
```bash
# Run all tests
pytest

# Run with coverage
coverage run -m pytest
coverage report

# Run functional tests
python functional_tests/run_all.py

# Linting
ruff check .
ruff format .

# Type checking  
pyright
```

### Project Workflow Patterns

#### Creating New Projects
```python
# 1. Use project manager for orchestration
project_manager = ProjectManager()

# 2. Process through connectors in sequence
git_connector = create_git_connector_for_project_files()
kubectl_connector = KubectlConnector()
argo_connector = create_argo_connector()

# 3. Handle encryption through utils
encrypted_secret = await encrypt_age_content(secret, public_key)
```

#### Handling External Operations
```python
# ❌ WRONG: Direct external calls
subprocess.run(['kubectl', 'apply', '-f', 'manifest.yaml'])

# ✅ CORRECT: Use connectors
kubectl_connector = KubectlConnector()
result = await kubectl_connector.apply_manifest(manifest_path)
```

#### Secret Management Patterns  
```python
# For AGE operations
from opi.utils.age import decrypt_password_smart, encrypt_age_content

# For SOPS operations  
from opi.utils.sops import encrypt_sops_file, decrypt_sops_file
```

## Important File Locations

### Configuration
- `opi/core/config.py` - Application settings and environment configuration
- `opi/core/cluster_config.py` - Kubernetes cluster-specific configuration

### Templates
- `manifests/*.yaml.jinja` - Kubernetes manifest templates for resource generation

### Testing
- `tests/` - Unit tests
- `functional_tests/` - Integration and functional tests

## Command Guidelines

### Development Commands
```bash
# Install dependencies
poetry install

# Run development server
poetry run python -m opi

# Run tests with coverage
poetry run coverage run -m pytest
poetry run coverage report

# Linting and formatting
poetry run ruff check .
poetry run ruff format .

# Type checking
poetry run pyright
```

### Operational Commands  
```bash
# SOPS operations
sops --encrypt --in-place secret.yaml
sops --decrypt secret.yaml

# Age operations (handled via age.py utilities)
# kubectl operations (handled via kubectl connector)
```

## Architecture Decision Records

1. **Connector Pattern**: All external integrations go through dedicated connector classes to maintain separation of concerns and enable easier testing/mocking

2. **Project Manager as Worker**: Single orchestration point for complex multi-step operations to ensure consistency and error handling

3. **Dual Cryptography Approach**: AGE for runtime encryption/decryption, SOPS for file-based secret management in GitOps workflows

4. **Template-Based Generation**: Jinja2 templates for Kubernetes manifests to enable customization while maintaining consistency

5. **GitOps First**: Primary deployment method through ArgoCD with fallback to direct kubectl for specific scenarios

## Troubleshooting Common Issues

### Encryption/Decryption Issues
- Check AGE key configuration in settings
- Verify SOPS key availability for file operations  
- Use debug logging in age.py and sops.py utilities

### Connector Failures
- Verify external system connectivity (git, kubectl, ArgoCD)
- Check authentication credentials and permissions
- Review connector-specific error handling

### Deployment Issues
- Verify Kubernetes cluster connectivity
- Check namespace permissions and resource limits
- Review ArgoCD application status and sync policies

---

## Important Reminders

- **Always use connectors for external operations** - never bypass the connector pattern
- **Use age.py for AGE operations and sops.py for SOPS operations** - maintain separation of cryptographic concerns  
- **Project manager is the worker** - orchestrate complex operations through the project manager
- **Follow GitOps principles** - prefer declarative configurations and ArgoCD deployments
- **Security first** - encrypt all secrets, validate inputs, use type hints for safety