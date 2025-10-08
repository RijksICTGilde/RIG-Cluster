#!/usr/bin/env python3
"""
Test the new size attribute in layout-flow component
"""

import sys

sys.path.append("/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/jinja-roos-components")

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import setup_components


def test_layout_flow_size():
    """Test the layout-flow size attribute"""

    # Test template with different size options
    test_template_content = """
<h1>Layout Flow Size Test</h1>

<h2>Default (lg):</h2>
<c-layout-flow gap="md">
    <div>Content A</div>
    <div>Content B</div>
</c-layout-flow>

<h2>Small size:</h2>
<c-layout-flow gap="md" size="sm">
    <div>Content A</div>
    <div>Content B</div>
</c-layout-flow>

<h2>Medium size:</h2>
<c-layout-flow gap="md" size="md">
    <div>Content A</div>
    <div>Content B</div>
</c-layout-flow>

<h2>Large size (explicit):</h2>
<c-layout-flow gap="md" size="lg">
    <div>Content A</div>
    <div>Content B</div>
</c-layout-flow>

<h2>Uncentered:</h2>
<c-layout-flow gap="md" size="uncentered">
    <div>Content A</div>
    <div>Content B</div>
</c-layout-flow>
"""

    with open(
        "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates/test-layout-size.html",
        "w",
    ) as f:
        f.write(test_template_content)

    template_dir = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates"
    env = Environment(loader=FileSystemLoader(template_dir))
    setup_components(env)

    template = env.get_template("test-layout-size.html")
    result = template.render()

    print("LAYOUT FLOW SIZE TEST RESULTS:")
    print("=" * 60)

    # Check for expected classes
    checks = [
        ("Default lg classes", "rvo-max-width-layout--lg" in result),
        ("Small classes", "rvo-max-width-layout--sm" in result),
        ("Medium classes", "rvo-max-width-layout--md" in result),
        ("Uncentered classes", "rvo-max-width-layout--uncentered" in result),
        ("Base max-width class", "rvo-max-width-layout" in result),
        ("Layout column class", "rvo-layout-column" in result),
        ("Gap classes", "rvo-layout-gap--md" in result),
        ("Data attributes", "data-roos-size=" in result),
    ]

    for check_name, passed in checks:
        status = "✅" if passed else "❌"
        print(f"{status} {check_name}")

    # Count occurrences
    print("\n📊 COUNTS:")
    print(f"- rvo-max-width-layout: {result.count('rvo-max-width-layout')}")
    print(f"- rvo-max-width-layout--lg: {result.count('rvo-max-width-layout--lg')}")
    print(f"- rvo-max-width-layout--md: {result.count('rvo-max-width-layout--md')}")
    print(f"- rvo-max-width-layout--sm: {result.count('rvo-max-width-layout--sm')}")
    print(f"- rvo-max-width-layout--uncentered: {result.count('rvo-max-width-layout--uncentered')}")

    # Show a sample of the generated HTML
    print("\n🔍 SAMPLE HTML (first layout-flow):")
    lines = result.split("\n")
    for i, line in enumerate(lines):
        if 'data-roos-component="layout-flow"' in line:
            # Show this line and the previous one (which should have the class)
            if i > 0:
                print(f"  {lines[i-1].strip()}")
            print(f"  {line.strip()}")
            break

    success = all(
        [
            "rvo-max-width-layout--lg" in result,
            "rvo-max-width-layout--sm" in result,
            "rvo-max-width-layout--md" in result,
            "rvo-max-width-layout--uncentered" in result,
            result.count("rvo-max-width-layout--lg") >= 2,  # Default + explicit
        ]
    )

    print(
        f"\n{'🎉 SUCCESS' if success else '❌ FAILURE'}: Size attribute is {'working correctly' if success else 'not working'}"
    )

    return success


if __name__ == "__main__":
    test_layout_flow_size()
