#!/usr/bin/env python3
"""
Test rendering a preprocessed template.
"""

from jinja2 import Environment, FileSystemLoader

# The preprocessed template string
preprocessed = """
{% set _captured_content_geteggnw %}
    {% set _component_context = {"type": "h2", "textContent": "Test"} %}{% include "components/heading.html.j2" with context %}
{% endset %}{% set _component_context = {"gap": "lg", "content": _captured_content_geteggnw} %}{% include "components/layout-flow.html.j2" with context %}
"""

# Create environment with component templates
import sys

sys.path.insert(0, "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster")
from jinja_roos_components import get_templates_path

template_paths = [
    get_templates_path(),  # Component templates
]

env = Environment(loader=FileSystemLoader(template_paths))

print("Testing preprocessed template rendering...")
print("=" * 60)

try:
    # Create template from string
    template = env.from_string(preprocessed)

    # Render
    output = template.render()

    print("✓ Rendered successfully")
    print(f"  Length: {len(output)} characters")
    print("\nFirst 200 chars of output:")
    print(output[:200])

except Exception as e:
    print(f"✗ Error: {e}")
    import traceback

    traceback.print_exc()
