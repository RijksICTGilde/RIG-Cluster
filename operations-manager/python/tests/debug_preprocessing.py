"""
Debug the actual preprocessing with the operations-manager templates
"""

from jinja_roos_components.extension import ComponentExtension
from opi.core.templates import templates

# Get the extension
ext = None
for extension in templates.env.extensions.values():
    if isinstance(extension, ComponentExtension):
        ext = extension
        break

if not ext:
    print("ERROR: ComponentExtension not found!")
    exit(1)

# Override the preprocess method to add debugging
original_preprocess = ext.preprocess


def debug_preprocess(source, name, filename=None):
    print("\n=== PREPROCESS CALLED ===")
    print(f"Name: {name}")
    print(f"Filename: {filename}")
    print(f"Source length: {len(source)}")
    print(f"Source preview: {source[:100]}...")

    # Count component tags
    import re

    c_tags = re.findall(r"<c-[\w-]+", source)
    print(f"Component tags found: {len(c_tags)} - {set(c_tags)}")

    # Call original
    result = original_preprocess(source, name, filename)

    # Check result
    c_tags_after = re.findall(r"<c-[\w-]+", result)
    print(f"Component tags after: {len(c_tags_after)} - {set(c_tags_after)}")
    print(f"Result length: {len(result)}")

    return result


# Patch the method
ext.preprocess = debug_preprocess

# Now try to load and render the template
print("\n=== LOADING TEMPLATE ===")
template = templates.env.get_template("roos-form.html.j2")

print("\n=== RENDERING TEMPLATE ===")
result = template.render({"request": {"url": "test"}, "title": "Test", "clusters": ["test1", "test2"]})

print(f"\nFinal render contains <c- tags: {'<c-' in result}")
