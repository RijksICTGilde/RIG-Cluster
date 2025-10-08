#!/usr/bin/env python3
"""Test the DOM parser with complex attributes."""

from bs4 import BeautifulSoup

# Test HTML with binding attribute
html = """
<c-card padding="xl" background="color" backgroundColor="wit" :outline="true">
    <div>Content</div>
</c-card>
"""

soup = BeautifulSoup(html, "html.parser")
tag = soup.find("c-card")

print("Tag found:", tag.name)
print("Tag attributes:", tag.attrs)
print("Attribute types and values:")
for name, value in tag.attrs.items():
    print(f"  {name}: {type(value)} = {value!r}")

    # Test if we can call replace on value
    try:
        if hasattr(value, "replace"):
            result = value.replace('"', '\\"')
            print(f"    Can replace: {result}")
        else:
            print(f"    Cannot call replace on {type(value)}")
    except Exception as e:
        print(f"    Error: {e}")
