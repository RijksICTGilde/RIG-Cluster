"""
Integration tests for operations-manager.

This package contains integration tests that require external dependencies
like databases, Kubernetes clusters, or other services.

Test categories:
- Mock-based integration tests (fast, no infrastructure required)
- Docker-based tests (require postgres)
- De live groepen per afhankelijkheid, elk met zijn eigen marker en zijn eigen task;
  zie docs/development/toetsen.md
"""
