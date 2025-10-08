#!/usr/bin/env python3
"""
Test with a fresh environment.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

# Create a completely fresh environment
template_dir = Path("/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates")
env = Environment(
    loader=FileSystemLoader(str(template_dir)),
    cache_size=0,  # Disable cache
)

# Now setup components
from jinja_roos_components import setup_components

setup_components(env)

print("Testing with fresh environment...")
print("=" * 60)

try:
    # Get template
    template = env.get_template("architecture-overview.html.j2")

    # Render
    output = template.render(request={})

    print("✓ Template rendered successfully")
    print(f"  Length: {len(output)} characters")

    # Check for unprocessed components
    import re

    unprocessed = re.findall(r"<c-[a-z-]+", output)
    print(f"\n  Unprocessed components: {len(unprocessed)}")
    if unprocessed:
        for comp in unprocessed[:5]:
            print(f"    - {comp}")

except Exception as e:
    print(f"✗ Error: {e}")
    import traceback

    traceback.print_exc()
