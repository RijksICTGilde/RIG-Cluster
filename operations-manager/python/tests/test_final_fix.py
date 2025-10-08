"""
Final test to verify the component parsing fix works in operations-manager
"""

from opi.core.templates import templates

# Test that the form template now processes correctly
print("=== Testing operations-manager form template ===")

try:
    template = templates.env.get_template("roos-form.html.j2")
    result = template.render({"request": {"url": "test"}, "title": "Test", "clusters": ["test1", "test2"]})

    # Check for raw component tags
    import re

    raw_tags = re.findall(r"<c-[\w-]+", result)

    if raw_tags:
        print(f"❌ FAILED - Found {len(raw_tags)} raw component tags: {set(raw_tags)}")

        # Show first occurrence
        first_tag = raw_tags[0]
        pos = result.find(first_tag)
        print(f"\nFirst occurrence at position {pos}:")
        print(f"Context: ...{result[max(0, pos-50):pos+100]}...")
    else:
        print("✅ SUCCESS - All component tags have been processed!")
        print(f"Generated HTML length: {len(result)} characters")

        # Verify expected HTML elements are present
        checks = [
            ('<form method="POST"', "Form element"),
            ("utrecht-form-label", "Form labels"),
            ("<input", "Input elements"),
            ("<select", "Select elements"),
            ("<button", "Button elements"),
        ]

        print("\nVerifying expected HTML elements:")
        for pattern, description in checks:
            if pattern in result:
                print(f"  ✅ {description} found")
            else:
                print(f"  ❌ {description} NOT found")

except Exception as e:
    print(f"ERROR: {e}")
    import traceback

    traceback.print_exc()
