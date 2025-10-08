#!/usr/bin/env python3
"""Debug placeholder replacement."""

from jinja2 import DictLoader, Environment
from jinja_roos_components import setup_components_dom

# Simple test
template_str = """
<c-heading type="h2" textContent="Test Heading"/>
<c-layout-flow gap="lg">
    <p>Content here</p>
</c-layout-flow>
"""

templates = {"test.html.j2": template_str}
env = Environment(loader=DictLoader(templates))
setup_components_dom(env)

# Get the extension
from jinja_roos_components.extension_dom import ComponentExtensionDOM

ext = None
for ext_class, ext_instance in env.extensions.items():
    if isinstance(ext_instance, ComponentExtensionDOM):
        ext = ext_instance
        break

if not ext:
    print("Extension not found!")
    import sys

    sys.exit(1)

# Process
result = ext.preprocess(template_str, "test.html.j2")

print("Placeholders stored:", len(ext._jinja_placeholders))
for key, value in ext._jinja_placeholders.items():
    print(f"  {key}: {value[:50]}...")

print("\nResult contains placeholders:", "JINJA2_PLACEHOLDER" in result)
print("\nFirst 500 chars of result:")
print(result[:500])
