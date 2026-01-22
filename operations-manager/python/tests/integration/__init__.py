"""
Integration tests for operations-manager.

This package contains integration tests that require external dependencies
like databases, Kubernetes clusters, or other services.

Test categories:
- Mock-based integration tests (fast, no infrastructure required)
- Docker-based tests (require postgres, marked with @pytest.mark.docker)
- Kind cluster tests (require Kind, marked with @pytest.mark.slow)
"""
