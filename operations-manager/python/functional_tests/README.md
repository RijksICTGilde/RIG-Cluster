# Functional Tests

This directory contains functional tests that validate the OPI components against real infrastructure (Git repositories, Kubernetes clusters, etc.).

## Purpose

Unlike unit tests that test individual components in isolation, functional tests validate:
- End-to-end workflows
- Real Git operations (clone, commit, push)
- Actual repository connectivity
- Integration between components

## Tests

### `test_argocd_application_creation.py`

Tests the complete ArgoCD application creation workflow:

1. **Configuration Validation**: Checks that all required settings and files are available
2. **Project File Parsing**: Validates parsing of `projects/simple-example.yaml`
3. **Git Connectivity**: Tests basic Git operations (clone, commit, push)
4. **ArgoCD Application Creation**: Full workflow including:
   - Manifest generation
   - GitOps repository cloning
   - File creation and commit
   - Push to remote repository

## Prerequisites

Before running functional tests, ensure:

1. **Git Server Running**: Local Git server on `localhost:2222`
2. **SSH Key Available**: SSH key at configured path with proper permissions
3. **Repository Access**: Required repositories exist and are accessible
4. **Configuration**: Settings in `config.py` point to correct repositories

### Quick Setup

Run the setup script to automatically create required repositories and validate connectivity:

```bash
# From the operations-manager/python directory
python functional_tests/setup_test_infrastructure.py
```

This script will:
- ✅ Check if Git server is running on localhost:2222
- ✅ Validate SSH connectivity and credentials  
- ✅ Create the ArgoCD applications repository
- ✅ Create the project repository from simple-example.yaml
- ✅ Provide detailed feedback on any issues

## Running Tests

### Run All Functional Tests
```bash
# From the operations-manager/python directory
python -m functional_tests.run_all
```

### Run Individual Test
```bash
# Test ArgoCD application creation
python functional_tests/test_argocd_application_creation.py
```

### Run with Verbose Logging
```bash
# Set logging level for detailed output
PYTHONPATH=. python functional_tests/test_argocd_application_creation.py
```

## Expected Output

### Successful Test Run
```
=== Git Connectivity Test ===

Testing Git connectivity...
  URL: ssh://git@localhost:2222/srv/git/argo-applications.git
  → Attempting to clone repository...
  ✓ Successfully cloned GitOps repository
  ✓ Successfully committed test file
  ✓ Successfully pushed to remote
  ✓ Git connectivity test completed

=== Functional Test: ArgoCD Application Creation ===

Step 1: Validating configuration...
  Git ArgoCD Applications URL: ssh://git@localhost:2222/srv/git/argo-applications.git
  SSH Key Path: /path/to/keys/git-server-key
  Branch: main
  ✓ SSH key found
  ✓ Project file found: /path/to/projects/simple-example.yaml

Step 2: Parsing project file...
  ✓ Successfully parsed project: example-project
  ✓ Found 1 deployment(s)
  ✓ Found 1 repository(ies)

Step 3: Testing ArgoCD application creation...
  → Starting ArgoCD application creation...
  ✓ ArgoCD application creation completed successfully

=== Test Results ===
✅ ArgoCD application creation test PASSED
   - Successfully connected to Git repository
   - Successfully generated ArgoCD manifest
   - Successfully committed and pushed to GitOps repo

🎉 All functional tests PASSED!
```

## Troubleshooting

### Common Issues

1. **SSH Key Permission Issues**
   ```bash
   chmod 600 /path/to/your/ssh-key
   ```

2. **Git Server Not Running**
   - Ensure your local Git server is running on port 2222
   - Check that repositories exist

3. **Repository Access Issues**
   - Verify SSH key is added to Git server
   - Test manual Git operations first

4. **Configuration Issues**
   - Check `config.py` settings
   - Verify file paths are correct

### Debug Mode
For detailed debugging, modify the logging level in the test file:
```python
logging.basicConfig(level=logging.DEBUG)
```

## Integration with CI/CD

These tests can be integrated into CI/CD pipelines to validate:
- Infrastructure changes
- Configuration updates
- Git connectivity
- End-to-end workflows

Example GitHub Actions integration:
```yaml
- name: Run Functional Tests
  run: |
    cd operations-manager/python
    python -m functional_tests.run_all
```