#!/usr/bin/env python3
"""Test the DOM parser to debug the issue."""

from bs4 import BeautifulSoup

# Test HTML with component
html = """
<c-layout-flow gap="lg">
    <c-heading type="h2" textContent="Test"/>
</c-layout-flow>
"""

soup = BeautifulSoup(html, "html.parser")
tag = soup.find("c-layout-flow")

print("Tag found:", tag.name)
print("Tag attributes:", tag.attrs)
print("Attribute types:")
for name, value in tag.attrs.items():
    print(f"  {name}: {type(value)} = {value}")

# Test value handling
for name, value in tag.attrs.items():
    if isinstance(value, list):
        print(f"  {name} is a list: {value}")
        value = " ".join(value)
    print(f"  Final {name}: {value}")
