#!/usr/bin/env python3
"""
Test just the preprocessing step.
"""

from jinja2 import DictLoader, Environment
from jinja_roos_components.extension import ComponentExtension

# Simple test template
template_str = """
<c-layout-flow gap="lg">
    <c-heading type="h2" textContent="Test"/>
</c-layout-flow>
"""

# Create environment
env = Environment(loader=DictLoader({"test.html.j2": template_str}))
ext = ComponentExtension(env)

print("Testing preprocessing...")
print("=" * 60)
print("Input:")
print(template_str)
print("\n" + "=" * 60)

# Preprocess
result = ext.preprocess(template_str, "test.html.j2")

print("Output:")
print(result)
print("\n" + "=" * 60)

# Check for issues
if "<c-" in result:
    print("✗ Still has unprocessed components")
else:
    print("✓ All components processed")

# Check structure
if "{% set" in result and "{% include" in result:
    print("✓ Has Jinja2 includes")
else:
    print("✗ Missing Jinja2 includes")
