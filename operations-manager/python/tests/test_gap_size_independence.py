#!/usr/bin/env python3
"""
Test that gap and size are independent in layout-flow
"""

import sys

sys.path.append("/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/jinja-roos-components")

from jinja2 import Environment, FileSystemLoader
from jinja_roos_components.extension import setup_components


def test_gap_size_independence():
    """Test that gap and size don't interfere with each other"""

    test_template = """
<c-layout-flow gap="xs" size="sm">Small gap, small size</c-layout-flow>
<c-layout-flow gap="xl" size="sm">Large gap, small size</c-layout-flow>
<c-layout-flow gap="xs" size="lg">Small gap, large size</c-layout-flow>
<c-layout-flow gap="xl" size="lg">Large gap, large size</c-layout-flow>
"""

    with open(
        "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates/test-gap-size.html", "w"
    ) as f:
        f.write(test_template)

    template_dir = "/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/templates"
    env = Environment(loader=FileSystemLoader(template_dir))
    setup_components(env)

    template = env.get_template("test-gap-size.html")
    result = template.render()

    print("GAP AND SIZE INDEPENDENCE TEST:")
    print("=" * 50)

    # Parse each div to check classes
    lines = result.split("\n")
    test_cases = []

    for line in lines:
        if 'data-roos-component="layout-flow"' in line:
            # Find the class line above it
            class_line = ""
            for prev_line in lines:
                if "class=" in prev_line and prev_line in result[: result.find(line)]:
                    class_line = prev_line

            # Extract gap and size from data attributes
            gap = line.split('data-roos-gap="')[1].split('"')[0] if "data-roos-gap=" in line else "unknown"
            size = line.split('data-roos-size="')[1].split('"')[0] if "data-roos-size=" in line else "unknown"

            # Check expected classes
            expected_gap_class = f"rvo-layout-gap--{gap}"
            expected_size_class = f"rvo-max-width-layout--{size}"

            has_gap_class = expected_gap_class in class_line
            has_size_class = expected_size_class in class_line

            test_cases.append(
                {
                    "gap": gap,
                    "size": size,
                    "has_correct_gap": has_gap_class,
                    "has_correct_size": has_size_class,
                    "class_line": class_line.strip(),
                }
            )

    print("TEST RESULTS:")
    for i, case in enumerate(test_cases, 1):
        gap_status = "✅" if case["has_correct_gap"] else "❌"
        size_status = "✅" if case["has_correct_size"] else "❌"
        print(f"\nTest {i}: gap={case['gap']}, size={case['size']}")
        print(f"  {gap_status} Gap class (rvo-layout-gap--{case['gap']})")
        print(f"  {size_status} Size class (rvo-max-width-layout--{case['size']})")
        print(f"  Classes: {case['class_line']}")

    # Verify combinations work independently
    all_passed = all(case["has_correct_gap"] and case["has_correct_size"] for case in test_cases)

    print(
        f"\n{'🎉 SUCCESS' if all_passed else '❌ FAILURE'}: Gap and size are {'independent' if all_passed else 'interfering with each other'}"
    )

    # Show specific evidence
    print("\n📊 EVIDENCE:")
    print(
        f"- Small gap + small size: {'✅' if any(c['gap']=='xs' and c['size']=='sm' and c['has_correct_gap'] and c['has_correct_size'] for c in test_cases) else '❌'}"
    )
    print(
        f"- Large gap + small size: {'✅' if any(c['gap']=='xl' and c['size']=='sm' and c['has_correct_gap'] and c['has_correct_size'] for c in test_cases) else '❌'}"
    )
    print(
        f"- Small gap + large size: {'✅' if any(c['gap']=='xs' and c['size']=='lg' and c['has_correct_gap'] and c['has_correct_size'] for c in test_cases) else '❌'}"
    )
    print(
        f"- Large gap + large size: {'✅' if any(c['gap']=='xl' and c['size']=='lg' and c['has_correct_gap'] and c['has_correct_size'] for c in test_cases) else '❌'}"
    )


if __name__ == "__main__":
    test_gap_size_independence()
