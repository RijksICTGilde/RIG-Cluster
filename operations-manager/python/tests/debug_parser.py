#!/usr/bin/env python3
"""
Debug the parser to see what's happening with the c-heading component.
"""

import logging

from jinja2 import DictLoader, Environment

# Enable debug logging
logging.basicConfig(level=logging.DEBUG)

# Simple test case
test_template = """
<c-layout-flow gap="lg">
    <c-heading type="h2" textContent="This is a test"/>
</c-layout-flow>
"""

# Create environment
templates = {"test.html.j2": test_template}

env = Environment(loader=DictLoader(templates))

# Get the extension
from jinja_roos_components.extension import ComponentExtension

ext = ComponentExtension(env)

print("Testing component parsing:")
print("=" * 60)
print("Input template:")
print(test_template)
print("\n" + "=" * 60)

# Try to preprocess
try:
    result = ext.preprocess(test_template, "test.html.j2")
    print("Preprocessed result:")
    print(result)
except Exception as e:
    print(f"Error during preprocessing: {e}")
    import traceback

    traceback.print_exc()
