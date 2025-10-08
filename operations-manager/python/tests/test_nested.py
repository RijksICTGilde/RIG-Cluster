"""
Test nested component processing to find the exact issue
"""

from jinja2 import Environment
from jinja_roos_components import setup_components
from jinja_roos_components.extension import ComponentExtension

# Create a fresh environment
env = Environment()
setup_components(env)

# Get the extension
ext = None
for extension in env.extensions.values():
    if isinstance(extension, ComponentExtension):
        ext = extension
        break

# Test cases with increasing complexity
test_cases = [
    # Simple nested
    ("<c-page><c-card>Content</c-card></c-page>", "Simple nested"),
    # Double nested
    ("<c-page><c-layout-flow><c-card>Content</c-card></c-layout-flow></c-page>", "Double nested"),
    # With attributes
    ('<c-page title="Test"><c-card outline="true">Content</c-card></c-page>', "With attributes"),
    # Multiple siblings
    ("<c-page><c-card>Card 1</c-card><c-card>Card 2</c-card></c-page>", "Multiple siblings"),
    # Mixed content
    ("<c-page><h1>Title</h1><c-card>Content</c-card></c-page>", "Mixed content"),
    # Real example structure
    (
        """<c-page title="Test">
    <c-layout-flow gap="xl">
        <c-card outline="true">
            <c-layout-flow gap="md">
                <p>Content</p>
            </c-layout-flow>
        </c-card>
    </c-layout-flow>
</c-page>""",
        "Real structure",
    ),
]

for test_html, description in test_cases:
    print(f"\n=== {description} ===")
    print(f"Input: {test_html[:50]}...")

    # Preprocess
    result = ext.preprocess(test_html, "test", None)

    # Check for remaining components
    import re

    remaining = re.findall(r"<c-[\w-]+", result)

    if remaining:
        print(f"❌ FAILED - Remaining components: {remaining}")
        print(f"Result preview: {result[:200]}...")
    else:
        print("✅ PASSED - All components processed")

# Now test what the actual preprocessing does step by step
print("\n\n=== STEP BY STEP ANALYSIS ===")
test = '<c-page title="Test"><c-card>Content</c-card></c-page>'

# Manually trace through the preprocessing
import re

pattern = ext.component_pattern

# First match
match = pattern.search(test)
if match:
    print(f"First match: {match.group(0)}")
    print(f"Component: {match.group(1)}")
    print(f"Content: {match.group(3)!r}")

    # Check if content has components
    content = match.group(3)
    inner_matches = list(pattern.finditer(content))
    print(f"Inner matches: {len(inner_matches)}")

    if inner_matches:
        for im in inner_matches:
            print(f"  - {im.group(1)}: {im.group(0)[:30]}...")
