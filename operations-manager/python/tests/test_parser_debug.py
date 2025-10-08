#!/usr/bin/env python3
"""
Debug the parser step by step.
"""

html = """<c-action-group
    :actions="[
        {
            'type': 'button',
            'kind': 'tertiary',
            'size': 'md',
            'label': 'Annuleren',
            'onclick': 'window.location.href="/dashboard"'
        }
    ]" />"""

print("HTML to parse:")
print(html)
print("\n" + "=" * 60)

# Find the tag manually
tag_start = html.find("<c-action-group")
print(f"Tag starts at position: {tag_start}")

# Find the end of the tag
pos = tag_start + len("<c-action-group")
in_quote = None
bracket_depth = 0

while pos < len(html):
    char = html[pos]

    # Track quotes
    if char in ('"', "'"):
        if in_quote is None:
            in_quote = char
            print(f"  Pos {pos}: Entering {char} quote")
        elif in_quote == char:
            # Check if we're in a bracket structure
            if bracket_depth == 0:
                in_quote = None
                print(f"  Pos {pos}: Exiting {char} quote")

    # Track brackets inside quotes
    if in_quote:
        if char == "[":
            bracket_depth += 1
            print(f"  Pos {pos}: Opening bracket, depth={bracket_depth}")
        elif char == "]":
            bracket_depth -= 1
            print(f"  Pos {pos}: Closing bracket, depth={bracket_depth}")

    # Check for end of tag
    if char == ">" and in_quote is None:
        print(f"  Pos {pos}: Found end of tag")
        tag_end = pos + 1
        break

    pos += 1

print(f"\nTag ends at position: {tag_end}")
print(f"Full tag: {html[tag_start:tag_end]}")

# Extract attributes
attrs_start = tag_start + len("<c-action-group")
attrs_end = tag_end - 2  # Account for />
attrs_str = html[attrs_start:attrs_end].strip()

print(f"\nAttributes string ({len(attrs_str)} chars):")
print(repr(attrs_str))
