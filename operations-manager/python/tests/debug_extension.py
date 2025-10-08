#!/usr/bin/env python3
"""
Debug the extension to see why placeholders aren't replaced.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension_dom import ComponentExtensionDOM

# Read the template source
template_path = Path(
    "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates/architecture-overview.html.j2"
)
source = template_path.read_text()

# Create extension
env = Environment(loader=FileSystemLoader(str(template_path.parent)))
ext = ComponentExtensionDOM(env)

print("Processing template...")
print("=" * 60)

# Process
result = ext.preprocess(source, "architecture-overview.html.j2")

print(f"Placeholders stored: {len(ext._jinja_placeholders)}")
if ext._jinja_placeholders:
    print("\nPlaceholder mappings:")
    for key, value in list(ext._jinja_placeholders.items())[:5]:  # Show first 5
        print(f"  {key}:")
        print(f"    {value[:100]}...")

print(f"\nResult contains placeholders: {'JINJA2_PLACEHOLDER' in result}")

# Count placeholders in result
import re

placeholders = re.findall(r"JINJA2_PLACEHOLDER_\w+", result)
print(f"Placeholders in result: {len(placeholders)}")
if placeholders:
    print("  IDs:", placeholders[:5])

# Check if these specific placeholders are in the mappings
test_ids = ["bajalzpd", "cwkemwri"]
for test_id in test_ids:
    full_id = f"JINJA2_PLACEHOLDER_{test_id}"
    if full_id in ext._jinja_placeholders:
        print(f"\n{full_id} IS in mappings")
    else:
        print(f"\n{full_id} NOT in mappings")
