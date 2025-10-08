#!/usr/bin/env python3
"""Debug layout component processing."""

import sys
from pathlib import Path

# Add jinja-roos-components to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "jinja-roos-components"))

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import ComponentExtension

# Setup paths
component_templates = (
    Path(__file__).parent.parent.parent / "jinja-roos-components" / "jinja_roos_components" / "templates"
)

# Simple test - just layout components
test_template = """
<c-layout-row gap="md">
    Simple content
</c-layout-row>
"""

# Create environment with debug mode
env = Environment(loader=FileSystemLoader([str(component_templates)]), extensions=[ComponentExtension], autoescape=True)

print("Testing simple layout-row component...")
print("Input template:")
print(test_template)
print("\n" + "=" * 50)

try:
    template = env.from_string(test_template)
    html_output = template.render()
    print("Rendered output:")
    print(html_output)

    if "rvo-layout-row" in html_output:
        print("\n✅ SUCCESS: Layout component was processed!")
    else:
        print("\n❌ FAILED: Layout component was NOT processed")
        print("Raw component tag is still present in output")

except Exception as e:
    print(f"\n❌ ERROR: {e}")
    import traceback

    traceback.print_exc()
