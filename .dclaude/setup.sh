#!/bin/bash
# Project setup for RIG-Cluster dclaude sessions
# Based on workflow/prepare.md

cd /workspace/operations-manager/python

# Install all dependencies (main + test + dev)
uv sync --all-groups 2>&1 || uv sync --all-groups --no-frozen 2>&1

# Install Playwright browsers for E2E tests
uv run playwright install chromium 2>&1 || true
