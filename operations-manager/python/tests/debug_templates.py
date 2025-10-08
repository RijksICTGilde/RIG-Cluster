"""
Debug template processing in operations-manager
"""

from jinja_roos_components.extension import ComponentExtension
from opi.core.templates import templates

print("=== Debugging Operations Manager Templates ===")

# Check if extension is loaded
print(f"\n1. Extensions in templates.env: {list(templates.env.extensions.keys())}")
has_component_ext = any(isinstance(ext, ComponentExtension) for ext in templates.env.extensions.values())
print(f"   Has ComponentExtension: {has_component_ext}")

# Get the extension
ext = None
for extension in templates.env.extensions.values():
    if isinstance(extension, ComponentExtension):
        ext = extension
        break

if ext:
    print(f"\n2. Component pattern: {ext.component_pattern.pattern}")
    print(f"   Registry has 'card': {ext.registry.has_component('card')}")
    print(f"   Registry has 'layout-flow': {ext.registry.has_component('layout-flow')}")
    print(f"   Total components: {len(ext.registry.list_components())}")

# Test preprocessing directly
test_html = '<c-card title="Test"><c-layout-flow gap="md">Content</c-layout-flow></c-card>'
print(f"\n3. Testing preprocessing on: {test_html[:50]}...")

# Method 1: Direct preprocessing
if ext:
    preprocessed = ext.preprocess(test_html, "test", None)
    print(f"   Direct preprocess: Contains <c- tags: {'<c-' in preprocessed}")

# Method 2: Through env.from_string
from_string_result = templates.env.from_string(test_html).render()
print(f"   env.from_string: Contains <c- tags: {'<c-' in from_string_result}")

# Method 3: Load actual template
print("\n4. Loading actual roos-form.html.j2...")
try:
    template = templates.env.get_template("roos-form.html.j2")

    # Check template source through module
    source, filename, uptodate = templates.env.loader.get_source(templates.env, "roos-form.html.j2")
    print(f"   Template source length: {len(source)}")
    print(f"   Source contains <c-page: {'<c-page' in source}")
    print(f"   Source contains <c-card: {'<c-card' in source}")
    print(f"   Source contains <c-layout-flow: {'<c-layout-flow' in source}")

    # Try to render
    result = template.render({"request": {"url": "test"}, "title": "Test", "clusters": ["test1", "test2"]})

    print(f"\n   Rendered length: {len(result)}")
    print(f"   Rendered contains <c-page: {'<c-page' in result}")
    print(f"   Rendered contains <c-card: {'<c-card' in result}")
    print(f"   Rendered contains <c-layout-flow: {'<c-layout-flow' in result}")

    # Show where the first unprocessed tag appears
    if "<c-" in result:
        pos = result.find("<c-")
        print(f"\n   First <c- tag at position {pos}:")
        print(f"   Context: ...{result[max(0,pos-50):pos+100]}...")

except Exception as e:
    print(f"   ERROR: {e}")
    import traceback

    traceback.print_exc()

# Check if there's something special about FastAPI's template handling
print("\n5. Checking FastAPI template configuration...")
print(f"   Template env class: {type(templates.env)}")
print(f"   Loader type: {type(templates.env.loader)}")
print(f"   Loader search paths: {getattr(templates.env.loader, 'searchpath', 'N/A')}")
