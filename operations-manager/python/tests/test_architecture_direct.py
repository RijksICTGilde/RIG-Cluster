#!/usr/bin/env python3
"""
Test the architecture template directly without the server.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components import setup_components_dom

# Setup templates
template_dir = Path("/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates")
env = Environment(loader=FileSystemLoader(str(template_dir)))
setup_components_dom(env)

print("Testing architecture template processing...")
print("=" * 60)

# Load the template
try:
    template = env.get_template("architecture-overview.html.j2")

    # Render it
    output = template.render(request={})

    print("✓ Template rendered successfully")
    print(f"  Output length: {len(output)} characters")

    # Check for unprocessed components
    import re

    unprocessed = re.findall(r"<c-[a-z-]+[^>]*>", output)

    if unprocessed:
        print(f"\n✗ Found {len(unprocessed)} unprocessed components:")
        for i, comp in enumerate(unprocessed[:10]):  # Show first 10
            print(f"  {i+1}. {comp}")
    else:
        print("\n✓ All components processed successfully!")

    # Check for placeholders
    if "JINJA2_PLACEHOLDER" in output:
        print("\n✗ Found unreplaced placeholders")
        placeholders = re.findall(r"JINJA2_PLACEHOLDER_\w+", output)
        print(f"  Count: {len(placeholders)}")
    else:
        print("✓ No unreplaced placeholders")

    # Check structure
    print("\n✓ Checking content structure:")
    if "The Big Picture" in output:
        print("  ✓ 'The Big Picture' section found")
    else:
        print("  ✗ 'The Big Picture' section NOT found")

    if "Built for Developers" in output:
        print("  ✓ 'Built for Developers' hero found")
    else:
        print("  ✗ 'Built for Developers' hero NOT found")

    # Save output for inspection
    output_file = Path("architecture_output.html")
    output_file.write_text(output)
    print(f"\n✓ Output saved to {output_file}")

except Exception as e:
    print(f"\n✗ Error rendering template: {e}")
    import traceback

    traceback.print_exc()
